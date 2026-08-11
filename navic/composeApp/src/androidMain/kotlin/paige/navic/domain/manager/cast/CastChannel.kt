package paige.navic.domain.manager.cast

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import paige.navic.util.core.Logger
import java.net.InetSocketAddress
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager
import kotlin.time.Duration.Companion.seconds

private const val TAG = "CastChannel"

private const val CONNECT_TIMEOUT_MS = 8_000

/**
 * Read timeout applied ONLY across the TLS handshake.
 *
 * `startHandshake()` is otherwise unbounded: if the speaker accepts the TCP connection and then
 * never sends a ServerHello, the call blocks forever, holding the bridge's cast mutex and
 * swallowing every later command with no log line to show for it.
 */
private const val HANDSHAKE_TIMEOUT_MS = 8_000
private val REQUEST_TIMEOUT = 8.seconds
private val HEARTBEAT_INTERVAL = 5.seconds

/**
 * A live castv2 connection to one Chromecast.
 *
 * Owns the TLS socket, the reader loop, heartbeat, and `requestId` correlation. Deliberately
 * knows nothing about the hub — [CastDeviceBridge] is what maps hub `do` commands onto this.
 *
 * Lifecycle is single-use: once [close] runs (or the socket dies), build a new instance. That is
 * what makes "the receiver app idled out, rebuild it" a trivially correct recovery rather than a
 * state-machine reset.
 */
internal class CastChannel(
	val host: String,
	private val port: Int = CastProtocol.CAST_PORT
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

	private var socket: SSLSocket? = null
	private var readerJob: Job? = null
	private var heartbeatJob: Job? = null

	private val writeMutex = Mutex()
	private var requestIdCounter = 1L

	/** requestId → waiter, for turning the async frame stream into suspending calls. */
	private val pending = mutableMapOf<Long, (JsonObject) -> Unit>()
	private val pendingMutex = Mutex()

	/** The launched/joined app's transportId — the destination for all media traffic. */
	@Volatile
	var transportId: String? = null
		private set

	@Volatile
	var sessionId: String? = null
		private set

	@Volatile
	var mediaSessionId: Long? = null
		private set

	@Volatile
	private var closed = false

	private val _status = MutableSharedFlow<MediaStatus>(
		replay = 1,
		extraBufferCapacity = 16,
		onBufferOverflow = BufferOverflow.DROP_OLDEST
	)

	/** Spontaneous MEDIA_STATUS pushes from the device (play/pause/finish/seek). */
	val status: SharedFlow<MediaStatus> = _status.asSharedFlow()

	private val _closedEvents = MutableSharedFlow<Unit>(replay = 1, extraBufferCapacity = 1)

	/**
	 * Fires once when the connection drops for ANY reason, ours included.
	 *
	 * The bridge uses this to notice a receiver that tore itself down while paused, and to pick
	 * playback back up when someone resumes from the Google Home app. It distinguishes its own
	 * teardown with a flag; this channel just reports the fact.
	 */
	val closedEvents: SharedFlow<Unit> = _closedEvents.asSharedFlow()

	val isOpen: Boolean get() = !closed && socket?.isClosed == false

	// ------------------------------------------------------------------ connect

	/** Opens the socket and virtual-connects to `receiver-0`. Throws on failure. */
	suspend fun connect() = withContext(Dispatchers.IO) {
		check(!closed) { "castv2: channel already closed" }
		Logger.i(TAG, "connecting to $host:$port")
		val s = trustAllSocketFactory().createSocket() as SSLSocket
		// Connect timeout is separate from the read timeout; without it a speaker that moved
		// to a new IP hangs the caller for the OS default (minutes) instead of failing fast.
		s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
		// Bound the handshake, then hand the socket to the reader loop with blocking reads
		// restored — the loop's whole job is to sit waiting for unsolicited pushes, so a
		// permanent read timeout would tear down a perfectly healthy connection every 8s.
		s.soTimeout = HANDSHAKE_TIMEOUT_MS
		s.startHandshake()
		s.soTimeout = 0
		socket = s

		// NOT UNDISPATCHED. connect() already runs on Dispatchers.IO, and readLoop's first act is
		// withContext(Dispatchers.IO) — same dispatcher, so it does not suspend. Starting
		// undispatched therefore ran the loop inline on this very thread, straight into a blocking
		// readFrame(), and connect() never returned: no "connected" log, castMutex held forever,
		// every later command queued behind it. Let it start on its own thread.
		readerJob = scope.launch { readLoop(s) }
		sendVirtualConnect(CastProtocol.RECEIVER_ID)
		heartbeatJob = scope.launch { heartbeatLoop() }
		Logger.i(TAG, "connected to $host")
	}

	/**
	 * Take over the Default Media Receiver: join it if it is already running, launch it otherwise.
	 *
	 * Joining rather than always launching is what makes adoption possible — relaunching would
	 * stop whatever the speaker is playing, which is the exact opposite of the goal when Navic
	 * restarts while music is still going.
	 *
	 * @return the running application, or null if it could be neither joined nor launched.
	 */
	suspend fun launchOrJoin(): ReceiverApplication? {
		joinRunning()?.let { return it }
		val reply = request(
			CastProtocol.NS_RECEIVER,
			CastProtocol.RECEIVER_ID
		) { id ->
			buildJsonObject {
				put("type", "LAUNCH")
				put("requestId", id)
				put("appId", CastProtocol.DEFAULT_MEDIA_RECEIVER_APP_ID)
			}
		} ?: return null

		val app = reply.toReceiverStatus()?.mediaReceiver() ?: return null
		attachTo(app)
		Logger.i(TAG, "$host: launched receiver session ${app.sessionId}")
		return app
	}

	/**
	 * Join the Default Media Receiver only if it is ALREADY running. Never launches it.
	 *
	 * The difference is audible, which is why this is a separate entry point rather than a flag on
	 * [launchOrJoin]: LAUNCH takes the speaker's audio output away from whoever holds it, so a
	 * speaker playing over Bluetooth goes silent the moment we probe it. Anything speculative — and
	 * adoption on startup is speculative by nature — must be able to answer "nothing of ours is
	 * running here" without making that true.
	 */
	suspend fun joinRunning(): ReceiverApplication? {
		val existing = runningApp() ?: return null
		attachTo(existing)
		Logger.i(TAG, "$host: joined running receiver session ${existing.sessionId}")
		return existing
	}

	/** The Default Media Receiver if it's already running on the device, else null. */
	suspend fun runningApp(): ReceiverApplication? {
		val reply = request(CastProtocol.NS_RECEIVER, CastProtocol.RECEIVER_ID) { id ->
			buildJsonObject {
				put("type", "GET_STATUS")
				put("requestId", id)
			}
		} ?: return null
		return reply.toReceiverStatus()?.mediaReceiver()
	}

	private suspend fun attachTo(app: ReceiverApplication) {
		transportId = app.transportId
		sessionId = app.sessionId
		// Media commands go to the app's transportId, and that destination needs its OWN virtual
		// connection first. Skipping this is the classic castv2 failure: everything "succeeds"
		// and the device does nothing.
		app.transportId?.let { sendVirtualConnect(it) }
		// Adopt whatever it is already doing, so a joined session reports truthfully at once.
		mediaStatus()?.let { st ->
			mediaSessionId = st.mediaSessionId
			_status.tryEmit(st)
		}
	}

	// ------------------------------------------------------------------ media

	suspend fun load(
		contentId: String,
		contentType: String,
		title: String?,
		artist: String?,
		album: String?,
		imageUrl: String?,
		positionMs: Long,
		autoplay: Boolean
	): MediaStatus? {
		val target = transportId ?: return null
		val payload = LoadRequest(
			requestId = nextRequestId(),
			sessionId = sessionId,
			media = MediaInformation(
				contentId = contentId,
				contentType = contentType,
				streamType = "BUFFERED",
				metadata = MediaMetadata(
					title = title,
					artist = artist,
					albumName = album,
					images = imageUrl?.let { listOf(MediaImage(it)) } ?: emptyList()
				)
			),
			autoplay = autoplay,
			currentTime = positionMs / 1000.0
		)
		val encoded = CastProtocol.json.encodeToString(LoadRequest.serializer(), payload)
		// LOAD gets a longer leash than other requests: the device fetches and buffers the URL
		// before answering, and on a slow server 8s is not unusual.
		val reply = requestRaw(
			CastProtocol.NS_MEDIA, target, payload.requestId, encoded, timeout = 15.seconds
		) ?: return null
		return reply.toMediaStatus()?.also {
			mediaSessionId = it.mediaSessionId ?: mediaSessionId
			_status.tryEmit(it)
		}
	}

	suspend fun play() = simpleMediaCommand("PLAY")
	suspend fun pause() = simpleMediaCommand("PAUSE")
	suspend fun stop() = simpleMediaCommand("STOP")

	suspend fun seek(positionMs: Long): MediaStatus? {
		val target = transportId ?: return null
		val session = mediaSessionId ?: return null
		return request(CastProtocol.NS_MEDIA, target) { id ->
			buildJsonObject {
				put("type", "SEEK")
				put("requestId", id)
				put("mediaSessionId", session)
				put("currentTime", positionMs / 1000.0)
			}
		}?.toMediaStatus()
	}

	/**
	 * Current media status, or null if the device didn't answer.
	 *
	 * A null here is the liveness signal the bridge relies on: a receiver app that has been torn
	 * down after idling doesn't reject the request, it simply never replies, so the timeout IS
	 * the answer.
	 */
	suspend fun mediaStatus(timeout: kotlin.time.Duration = REQUEST_TIMEOUT): MediaStatus? {
		val target = transportId ?: return null
		val id = nextRequestId()
		val payload = buildJsonObject {
			put("type", "GET_STATUS")
			put("requestId", id)
		}.toString()
		return requestRaw(CastProtocol.NS_MEDIA, target, id, payload, timeout)?.toMediaStatus()
	}

	/** Device volume, 0..100. Applies to the speaker itself, not our stream. */
	suspend fun setVolume(percent: Int) {
		request(CastProtocol.NS_RECEIVER, CastProtocol.RECEIVER_ID) { id ->
			buildJsonObject {
				put("type", "SET_VOLUME")
				put("requestId", id)
				put("volume", buildJsonObject { put("level", percent.coerceIn(0, 100) / 100.0) })
			}
		}
	}

	private suspend fun simpleMediaCommand(type: String): MediaStatus? {
		val target = transportId ?: return null
		val session = mediaSessionId ?: return null
		return request(CastProtocol.NS_MEDIA, target) { id ->
			buildJsonObject {
				put("type", type)
				put("requestId", id)
				put("mediaSessionId", session)
			}
		}?.toMediaStatus()
	}

	// ------------------------------------------------------------------ plumbing

	private suspend fun nextRequestId(): Long = writeMutex.withLock { ++requestIdCounter }

	private suspend fun request(
		namespace: String,
		destination: String,
		build: (Long) -> JsonObject
	): JsonObject? {
		val id = nextRequestId()
		return requestRaw(namespace, destination, id, build(id).toString(), REQUEST_TIMEOUT)
	}

	private suspend fun requestRaw(
		namespace: String,
		destination: String,
		requestId: Long,
		payload: String,
		timeout: kotlin.time.Duration
	): JsonObject? {
		if (!isOpen) return null
		val waiter = kotlinx.coroutines.CompletableDeferred<JsonObject>()
		pendingMutex.withLock { pending[requestId] = { waiter.complete(it) } }
		try {
			send(CastMessage(CastProtocol.SENDER_ID, destination, namespace, payload))
			return withTimeoutOrNull(timeout) { waiter.await() }
				?: run {
					Logger.w(TAG, "$host: request $requestId ($namespace) timed out")
					null
				}
		} catch (e: CancellationException) {
			throw e
		} catch (e: Exception) {
			Logger.w(TAG, "$host: request $requestId failed: ${e.message}")
			return null
		} finally {
			pendingMutex.withLock { pending.remove(requestId) }
		}
	}

	private suspend fun sendVirtualConnect(destination: String) {
		send(
			CastMessage(
				CastProtocol.SENDER_ID, destination, CastProtocol.NS_CONNECTION,
				buildJsonObject { put("type", "CONNECT") }.toString()
			)
		)
	}

	private suspend fun send(message: CastMessage) = withContext(Dispatchers.IO) {
		val s = socket ?: throw IllegalStateException("castv2: not connected")
		// Frames must not interleave — one writer at a time.
		writeMutex.withLock { s.outputStream.writeFrame(message) }
	}

	private suspend fun readLoop(s: SSLSocket) = withContext(Dispatchers.IO) {
		try {
			val input = s.inputStream
			while (isActive && !s.isClosed) {
				val frame = input.readFrame()
				dispatch(frame)
			}
		} catch (e: CancellationException) {
			throw e
		} catch (e: Exception) {
			if (!closed) Logger.w(TAG, "$host: reader ended: ${e.message}")
		} finally {
			// Whoever ends the loop, the connection is over — tell the bridge exactly once.
			markClosed()
		}
	}

	private suspend fun dispatch(frame: CastMessage) {
		val envelope = runCatching {
			CastProtocol.json.decodeFromString(CastEnvelope.serializer(), frame.payloadUtf8)
		}.getOrNull() ?: return

		when {
			// The receiver pings us too; not answering gets the connection dropped.
			envelope.type == "PING" -> runCatching {
				send(
					CastMessage(
						CastProtocol.SENDER_ID, frame.sourceId, CastProtocol.NS_HEARTBEAT,
						buildJsonObject { put("type", "PONG") }.toString()
					)
				)
			}

			envelope.type == "CLOSE" -> {
				Logger.i(TAG, "$host: receiver closed the virtual connection")
				markClosed()
			}
		}

		val obj = runCatching {
			CastProtocol.json.parseToJsonElement(frame.payloadUtf8) as? JsonObject
		}.getOrNull() ?: return

		// Spontaneous status (no requestId, or requestId 0) — the device telling us it changed
		// state on its own: track finished, someone pressed play on a Google Home speaker, etc.
		if (frame.namespace == CastProtocol.NS_MEDIA && envelope.type == "MEDIA_STATUS") {
			obj.toMediaStatus()?.let { st ->
				mediaSessionId = st.mediaSessionId ?: mediaSessionId
				_status.tryEmit(st)
			}
		}

		val id = envelope.requestId ?: return
		val waiter = pendingMutex.withLock { pending.remove(id) } ?: return
		waiter(obj)
	}

	private suspend fun heartbeatLoop() {
		while (scope.isActive && isOpen) {
			delay(HEARTBEAT_INTERVAL)
			if (!isOpen) return
			runCatching {
				send(
					CastMessage(
						CastProtocol.SENDER_ID, CastProtocol.RECEIVER_ID,
						CastProtocol.NS_HEARTBEAT,
						buildJsonObject { put("type", "PING") }.toString()
					)
				)
			}.onFailure {
				Logger.w(TAG, "$host: heartbeat failed, closing")
				markClosed()
			}
		}
	}

	private fun markClosed() {
		if (closed) return
		closed = true
		_closedEvents.tryEmit(Unit)
		runCatching { socket?.close() }
	}

	fun close() {
		markClosed()
		heartbeatJob?.cancel()
		readerJob?.cancel()
		scope.cancel()
	}
}

// ------------------------------------------------------------------ helpers

private fun JsonObject.toReceiverStatus(): ReceiverStatus? = runCatching {
	CastProtocol.json.decodeFromJsonElement(ReceiverStatusMessage.serializer(), this).status
}.getOrNull()

private fun JsonObject.toMediaStatus(): MediaStatus? = runCatching {
	CastProtocol.json.decodeFromJsonElement(MediaStatusMessage.serializer(), this)
		.status.firstOrNull()
}.getOrNull()

/**
 * The Default Media Receiver among the running applications.
 *
 * Filtered by appId rather than taking the first entry: the idle backdrop/screensaver is also a
 * running application, and treating it as our session means every media command vanishes.
 */
private fun ReceiverStatus.mediaReceiver(): ReceiverApplication? =
	applications.firstOrNull {
		it.appId == CastProtocol.DEFAULT_MEDIA_RECEIVER_APP_ID && it.transportId != null
	}

/**
 * Accepts the Chromecast's self-signed certificate.
 *
 * Cast devices present a per-device cert from Google's own chain that no Android trust store
 * validates against the IP you dial. `castv2-client` does the same thing. The exposure is
 * limited to a LAN socket carrying Navidrome URLs the receiver is about to fetch anyway.
 */
private fun trustAllSocketFactory(): javax.net.ssl.SSLSocketFactory {
	val trustAll = object : X509TrustManager {
		override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) = Unit
		override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) = Unit
		override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
	}
	return SSLContext.getInstance("TLS").apply {
		init(null, arrayOf(trustAll), SecureRandom())
	}.socketFactory
}
