package paige.navic.domain.manager.cast

import io.ktor.client.HttpClient
import io.ktor.client.plugins.websocket.DefaultClientWebSocketSession
import io.ktor.client.plugins.websocket.WebSockets
import io.ktor.client.plugins.websocket.webSocket
import io.ktor.websocket.Frame
import io.ktor.websocket.close
import io.ktor.websocket.readText
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import paige.navic.util.core.Logger
import java.net.InetSocketAddress
import java.net.Socket
import kotlin.random.Random
import kotlin.time.Clock
import kotlin.time.Duration.Companion.milliseconds
import kotlin.time.Duration.Companion.seconds

private const val TAG = "CastBridge"

private const val INITIAL_BACKOFF_MS = 1_000L
private const val MAX_BACKOFF_MS = 30_000L

/** Position reporting cadence while playing, matching Feishin's ticker. */
private val TICK_INTERVAL = 1.seconds

/**
 * Liveness probe budget. A receiver app torn down after idling does not *reject* a request, it
 * simply never answers — so the timeout is the answer, and it needs to be short enough that a
 * play press doesn't visibly hang on it.
 */
private val ALIVE_PROBE_TIMEOUT = 3.seconds

/** How long to wait for a re-query to produce a new address before redialling a moved speaker. */
private val ADDRESS_REFRESH_WAIT = 1_200.milliseconds

/**
 * Hard ceiling on building one cast session: TCP + TLS + receiver launch.
 *
 * Insurance, not a timeout anyone should hit. A single unbounded call anywhere in that sequence
 * strands the bridge behind its own mutex with nothing in the log — which is exactly how the
 * undispatched reader bug hid.
 */
private val SESSION_SETUP_TIMEOUT = 25.seconds

/** Hub close code for "another socket registered this device id" (hub.py `_close(..., 4003)`). */
const val CLOSE_SUPERSEDED = 4003

/**
 * Ceiling on how stale an orphaned hub session may be and still be worth asking the speaker about.
 *
 * Only reached by a `repeat: all` session, whose remaining playtime is unbounded by definition;
 * every other session is bounded by the arithmetic in [CastDeviceBridge.couldStillBePlaying].
 */
private const val ADOPTION_MAX_AGE_MS = 6 * 60 * 60 * 1_000L

/**
 * Added to a session's remaining playtime before calling it definitely over.
 *
 * The estimate assumes uninterrupted playback, and a session paused from the Google Home app for a
 * few minutes would otherwise be written off while it is still sitting there ready to resume.
 */
private const val ADOPTION_SLACK_MS = 10 * 60 * 1_000L

/** Plain TCP reachability probe budget — a speaker on the LAN answers in milliseconds. */
private const val REACHABILITY_TIMEOUT_MS = 2_000

/** How often the bridge re-asserts its verdict on the speaker (PROTOCOL §3.2). */
private const val DEVICE_STATE_INTERVAL_MS = 30_000L

/**
 * Consecutive failed probes before calling a speaker down.
 *
 * One miss is a dropped multicast-adjacent packet on a phone that just turned its screen off; a run
 * of them spread over a minute is a speaker that has genuinely gone. A phantom receiver for a
 * minute is much cheaper than dropping a live one.
 */
private const val REACHABILITY_FAILS_BEFORE_DOWN = 2

/** One track as it arrives over the hub wire. */
private data class BridgeTrack(
	val id: String,
	val title: String?,
	val artist: String?,
	val album: String?,
	val streamUrl: String?,
	val mime: String?,
	val imageUrl: String?,
	/** Only used to estimate how long an orphaned session could still be running. 0 = unknown. */
	val durationMs: Long,
	val raw: JsonObject
)

/**
 * A Chromecast registered with the hub as its own virtual receiver.
 *
 * This is a port of Feishin's `CastDeviceBridge` (`feishin/src/main/features/core/cast/index.ts`),
 * and intentionally so: the safeguards in here are not speculative hardening, they are the residue
 * of bugs already found in the field and written up in `SESSION-2026-08-08-v1.15.1-merge.md`.
 * Where this diverges from that file, it is a bug.
 *
 * The model: the hub does not speak Google Cast. This class holds its OWN WebSocket to the hub —
 * separate from [paige.navic.domain.manager.HubManager]'s — registering as `cast-<id>` with
 * `platform: "chromecast"` and `caps: ["receiver"]`. The hub needs no special support; a virtual
 * receiver is just another client. Audio never passes through the phone: the speaker fetches the
 * Navidrome `streamUrl` directly.
 */
internal class CastDeviceBridge(
	private val deviceId: String,
	@Volatile var friendlyName: String,
	host: String,
	private val hubUrl: String,
	private val token: String,
	private val discovery: CastDiscovery,
	/** This client's own hub device id, stamped as `bridgedBy` so pickers can name the holder. */
	private val ownerDeviceId: () -> String?,
	/** Told when the hub supersedes us, so the manager can stand down instead of fighting back. */
	private val onSuperseded: () -> Unit
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
	private val client = HttpClient {
		install(WebSockets) { pingIntervalMillis = 10_000 }
	}

	val hubDeviceId: String get() = "cast-$deviceId"

	/**
	 * True only once the hub has answered `welcome`.
	 *
	 * Not "the socket opened" and not "we exist": the manager drives the picker off this, and a
	 * bridge that is looping on a refused connection must not be advertised as ready to receive a
	 * transfer — that reads as a working cast device that silently does nothing.
	 */
	private val _connected = MutableStateFlow(false)
	val connected: StateFlow<Boolean> = _connected.asStateFlow()

	@Volatile
	private var host: String = host

	private var ws: DefaultClientWebSocketSession? = null
	private val sendMutex = Mutex()
	private var connectJob: Job? = null
	private var tickerJob: Job? = null

	private var channel: CastChannel? = null
	private val castMutex = Mutex()
	private var deviceStateJob: Job? = null
	@Volatile
	private var lastReportedReachable: Boolean? = null

	private var tracks: List<BridgeTrack> = emptyList()
	private var index = 0

	@Volatile
	private var lastPositionMs = 0L

	@Volatile
	private var playing = false

	/**
	 * Freezes all reporting during a handover.
	 *
	 * `stop()` makes the device emit IDLE/CANCELLED with currentTime 0, and a late report of 0
	 * racing the hub's device switch is what reset transfers to the beginning of the track.
	 */
	@Volatile
	private var releasing = false

	/** Distinguishes our own teardown from the device hanging up, so we don't "recover" from it. */
	@Volatile
	private var tearingDown = false

	@Volatile
	private var destroyed = false

	/** One adoption attempt per hub connection; reset on each fresh `welcome`. */
	private var adopted = false

	/** Saved-queue identity of an adopted session, so re-claiming doesn't fork a history record. */
	private var savedQueueId: String? = null
	private var sourceKind: String? = null
	private var sourceName: String? = null

	fun start() {
		if (connectJob != null) return
		connectJob = scope.launch { runLoop() }
	}

	fun updateHost(newHost: String) {
		if (newHost == host) return
		Logger.i(TAG, "$friendlyName: address changed ${host} → $newHost")
		host = newHost
		// Drop the channel rather than repointing it — a socket to the old address is dead by
		// definition, and rebuilding is the recovery path we already trust everywhere else.
		scope.launch { teardownCast() }
	}

	/**
	 * Is the speaker actually there, regardless of what mDNS currently believes?
	 *
	 * A plain TCP connect — no TLS, no cast traffic, nothing the speaker surfaces to the user. This
	 * exists because multicast is the first thing a phone stops listening to when the screen goes
	 * off, so "the speaker vanished from discovery" and "the speaker was unplugged" are the same
	 * event as far as [CastDiscovery] can tell. They are not the same event to a bridge that is
	 * mid-session, and tearing one down on the strength of a missing announcement is how a speaker
	 * that was still playing lost its receiver a few minutes after the phone was locked.
	 */
	suspend fun speakerReachable(): Boolean = withContext(Dispatchers.IO) {
		runCatching {
			Socket().use { it.connect(InetSocketAddress(host, CastProtocol.CAST_PORT), REACHABILITY_TIMEOUT_MS) }
			true
		}.getOrDefault(false)
	}

	fun destroy() {
		destroyed = true
		connectJob?.cancel()
		deviceStateJob?.cancel()
		tickerJob?.cancel()
		scope.launch { teardownCast() }
		runCatching { client.close() }
		scope.cancel()
	}

	// ------------------------------------------------------------------ hub socket

	private suspend fun runLoop() {
		var backoffMs = INITIAL_BACKOFF_MS
		while (currentCoroutineContext().isActive && !destroyed) {
			var superseded = false
			try {
				connectOnce()
			} catch (e: CancellationException) {
				throw e
			} catch (e: Exception) {
				Logger.w(TAG, "$friendlyName: hub connection error: ${e.message}")
			}
			superseded = lastCloseCode?.toInt() == CLOSE_SUPERSEDED
			ws = null
			_connected.value = false
			if (destroyed) return
			if (superseded) {
				// Someone else (almost certainly Feishin's bridge) owns this speaker. Reconnecting
				// would kick them off, they would kick us back, and the two clients would flap
				// forever. Stand down and let the manager decide when to try again.
				Logger.i(TAG, "$friendlyName: superseded by another bridge — standing down")
				onSuperseded()
				return
			}
			// Jitter matters here in a way it doesn't for HubManager's single socket: every
			// speaker on the LAN has its own bridge, and they'd otherwise all retry in lockstep.
			val jitter = (Random.nextDouble() * 0.3 * backoffMs).toLong()
			delay(backoffMs + jitter)
			backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
		}
	}

	@Volatile
	private var lastCloseCode: Short? = null

	private suspend fun connectOnce() {
		lastCloseCode = null
		client.webSocket(hubUrl) {
			ws = this
			adopted = false
			Logger.i(TAG, "$friendlyName: hub socket open, saying hello as $hubDeviceId")
			sendFrame(buildJsonObject {
				put("t", "hello")
				put("token", token)
				put("device", buildJsonObject {
					put("id", hubDeviceId)
					// The 📺 prefix matches Feishin's naming so the same speaker reads
					// identically whichever client happens to be bridging it.
					put("name", "📺 $friendlyName")
					put("platform", "chromecast")
					// `loadAck` (PROTOCOL §7.1) makes a failed cast load visible immediately instead
					// of leaving the hub convinced this speaker took the session for ~20 seconds.
					putJsonArray("caps") { add("receiver"); add("loadAck") }
					ownerDeviceId()?.let { put("bridgedBy", it) }
				})
			})

			try {
				for (frame in incoming) {
					if (frame !is Frame.Text) continue
					val msg = runCatching {
						Json.parseToJsonElement(frame.readText()).jsonObject
					}.getOrNull() ?: continue
					handleHubFrame(msg)
				}
			} finally {
				_connected.value = false
				lastCloseCode = runCatching { closeReason.await()?.code }.getOrNull()
			}
		}
	}

	/**
	 * Tell the hub whether the speaker itself is there — PROTOCOL §3.2.
	 *
	 * The hub sees this bridge's socket, which lives on a phone, and calls the row `online` on the
	 * strength of it. That is a much weaker claim than "the speaker is on": a Chromecast that is
	 * unplugged, asleep, or in a house nobody here is in leaves the socket perfectly healthy. Only
	 * the bridge can tell the difference, so the bridge has to say.
	 *
	 * Reported on a timer rather than only on change, because the hub expires a verdict whose
	 * bridge has gone quiet instead of continuing to speak for it.
	 */
	private fun startDeviceStateLoop() {
		deviceStateJob?.cancel()
		deviceStateJob = scope.launch {
			var failures = 0
			while (isActive) {
				val probed = speakerReachable()
				failures = if (probed) 0 else failures + 1
				// An open cast channel is proof on its own — a probe that lost a packet must not
				// contradict a speaker we are actively talking to. And one miss is a dropped
				// packet; only a run of them is a speaker that has gone away.
				// Read once: ensureCast() can hold this mutex for the whole session-setup
				// budget, and taking it twice per tick doubles the wait for no benefit.
				val channelOpen = castMutex.withLock { channel?.isOpen == true }
				val reachable = probed || channelOpen ||
					failures < REACHABILITY_FAILS_BEFORE_DOWN
				if (reachable != lastReportedReachable) {
					Logger.i(TAG, "$friendlyName: reachable -> $reachable")
					lastReportedReachable = reachable
				}
				sendFrame(buildJsonObject {
					put("t", "deviceState")
					put("reachable", reachable)
					put("appRunning", channelOpen)
				})
				delay(DEVICE_STATE_INTERVAL_MS)
			}
		}
	}

	private suspend fun handleHubFrame(msg: JsonObject) {
		when (msg["t"]?.jsonPrimitive?.content) {
			"welcome" -> {
				_connected.value = true
				Logger.i(TAG, "$friendlyName: registered with hub as $hubDeviceId")
				adopted = false
				startDeviceStateLoop()
				msg["session"]?.jsonObject?.let { maybeAdopt(it) }
			}

			"session" -> maybeAdopt(msg)
			"do" -> handleDo(msg)
			else -> Unit
		}
	}

	private suspend fun sendFrame(obj: JsonObject) {
		val session = ws ?: return
		// Ordering matters: `released` must never overtake the final `report` it follows.
		sendMutex.withLock {
			runCatching { session.send(Frame.Text(obj.toString())) }
		}
	}

	private fun report(ended: Boolean = false) {
		if (releasing) return
		scope.launch {
			sendFrame(buildJsonObject {
				put("t", "report")
				put("index", index)
				put("isPlaying", playing)
				put("positionMs", lastPositionMs)
				if (ended) put("ended", true)
			})
		}
	}

	// ------------------------------------------------------------------ hub commands

	private suspend fun handleDo(msg: JsonObject) {
		val cmd = msg["cmd"]?.jsonPrimitive?.content ?: return
		// Logged unconditionally: when a transfer produces silence, the first thing worth knowing
		// is whether the hub asked us to do anything at all. Without this, "the hub never sent
		// load" and "load ran and failed quietly" look identical in a capture.
		Logger.i(TAG, "$friendlyName: do $cmd")
		try {
			when (cmd) {
				"load" -> {
					releasing = false
					tracks = msg["tracks"]?.jsonArray?.toTracks() ?: emptyList()
					index = msg["index"]?.jsonPrimitive?.int ?: 0
					// Honour `play`. Never load-playing-then-pause: in Feishin the async play
					// could land after the pause, leaving audio running under a store that read
					// PAUSED — and since engines act on status *changes*, every later pause,
					// ours or the user's, became a no-op.
					val play = msg["play"]?.jsonPrimitive?.content != "false"
					val ok = loadCurrent(msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L, play)
					// PROTOCOL §7.1. Without this the hub commits the active slot and never learns
					// the speaker didn't start, so casting to a TV that has been off for days reads
					// as playing on every device until somebody notices the silence.
					sendFrame(buildJsonObject {
						put("t", "loaded")
						put("ok", ok)
						if (!ok) put("error", "the speaker did not start playback")
					})
				}

				"jump" -> {
					index = msg["index"]?.jsonPrimitive?.int ?: 0
					loadCurrent(0L, true)
				}

				"play" -> {
					// Prove the session before using it. A Chromecast tears its receiver app
					// down after a few idle minutes while our object survives, so play() would
					// go into the void and we'd report audio that isn't happening. A false
					// negative is harmless — reloading is the right recovery anyway.
					if (castSessionAlive()) {
						channel?.play()
						playing = true
						report()
						startTicker()
					} else if (tracks.isNotEmpty()) {
						loadCurrent(lastPositionMs, true)
					}
				}

				"pause" -> {
					// A dropped PAUSE used to be reported as a pause anyway: the commands return
					// null when there is no transport or media session, and the null was discarded.
					// The hub then held a state the speaker had never been in.
					if (channel?.pause() == null) {
						Logger.w(TAG, "$friendlyName: pause never reached the device")
						teardownCast()
					}
					playing = false
					report()
				}

				"seek" -> {
					val pos = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
					if (channel?.seek(pos) != null) {
						lastPositionMs = pos
					} else {
						// Recording the position regardless meant the hub could hand the next device
						// a spot the speaker had never reached. Reload there instead — that IS where
						// the user asked to be.
						Logger.w(TAG, "$friendlyName: seek never reached the device — reloading")
						teardownCast()
						loadCurrent(pos, playing)
					}
				}

				"queueChanged" -> {
					// A queue edit must not start playback, and must not move the playhead:
					// re-locate the current track by id rather than trusting the new index.
					val currentId = tracks.getOrNull(index)?.id
					tracks = msg["tracks"]?.jsonArray?.toTracks() ?: emptyList()
					val relocated = tracks.indexOfFirst { it.id == currentId }
					index = if (relocated >= 0) relocated else msg["index"]?.jsonPrimitive?.int ?: 0
				}

				"setVolume" -> channel?.setVolume(msg["level"]?.jsonPrimitive?.int ?: 100)

				"release" -> handleRelease()

				else -> Unit
			}
		} catch (e: CancellationException) {
			throw e
		} catch (e: Exception) {
			Logger.e(TAG, "$friendlyName: $cmd failed", e)
		}
	}

	private suspend fun handleRelease() {
		// Freeze reporting FIRST — see the `releasing` field doc.
		releasing = true
		stopTicker()
		capturePosition()
		playing = false

		// Sent while `releasing` suppresses the ticker, so these two frames are the last word.
		val finalIndex = index
		val finalPosition = lastPositionMs
		sendFrame(buildJsonObject {
			put("t", "report")
			put("index", finalIndex)
			put("isPlaying", false)
			put("positionMs", finalPosition)
		})
		sendFrame(buildJsonObject {
			put("t", "released")
			put("index", finalIndex)
			put("positionMs", finalPosition)
		})

		runCatching { channel?.stop() }
		releasing = false
	}

	// ------------------------------------------------------------------ cast session

	/**
	 * Live channel, building one if needed. Returns null if the speaker can't be reached.
	 *
	 * On a failed connect this asks discovery to re-query and waits briefly for an answer before
	 * the second attempt: the usual cause of a failure here is a new DHCP lease, and dialling the
	 * old address again would just fail the same way.
	 */
	private suspend fun ensureCast(allowLaunch: Boolean = true): CastChannel? = castMutex.withLock {
		channel?.takeIf { it.isOpen }?.let { return@withLock it }
		channel?.let { runCatching { it.close() } }
		channel = null

		// A passive probe gets one attempt and no re-query. Its whole point is to be cheap and to
		// leave no trace when the answer is "nothing here"; retrying a moved speaker is worth doing
		// for a load the user asked for, not for a guess.
		val attempts = if (allowLaunch) 2 else 1
		repeat(attempts) { attempt ->
			if (attempt > 0) {
				discovery.requery()
				delay(ADDRESS_REFRESH_WAIT)
			}
			val candidate = CastChannel(host)
			val ok = withTimeoutOrNull(SESSION_SETUP_TIMEOUT) {
				runCatching {
					candidate.connect()
					// Distinguished from a thrown failure on purpose: a null here means the
					// socket and TLS were fine but the receiver app would not start, which is a
					// different problem from an unreachable speaker and logged nothing at all.
					val app = if (allowLaunch) candidate.launchOrJoin() else candidate.joinRunning()
					if (app == null && allowLaunch) {
						Logger.w(TAG, "$friendlyName: connected to $host but no receiver session")
					}
					app != null
				}.getOrElse { e ->
					Logger.w(TAG, "$friendlyName: cast connect to $host failed: ${e.message}")
					false
				}
			} ?: run {
				Logger.e(TAG, "$friendlyName: cast session setup timed out at $host")
				false
			}
			if (ok) {
				channel = candidate
				observeChannel(candidate)
				return@withLock candidate
			}
			runCatching { candidate.close() }
		}
		null
	}

	private fun observeChannel(ch: CastChannel) {
		scope.launch { ch.status.collect { onCastStatus(it) } }
		scope.launch {
			ch.closedEvents.collect {
				if (tearingDown || destroyed) return@collect
				// The device hung up on us: the receiver app idled out, or someone took it over.
				// Re-joining is also how a resume performed from the Google Home app gets picked
				// back up, so it is worth doing whenever we thought we were playing.
				Logger.i(TAG, "$friendlyName: cast socket closed")
				val wasPlaying = playing
				castMutex.withLock { channel = null }
				if (wasPlaying && !destroyed) {
					runCatching { loadCurrent(lastPositionMs, true) }
				}
			}
		}
	}

	/**
	 * Whether the receiver session is actually there.
	 *
	 * Cannot live in the ticker alone — the ticker returns early while paused, which is exactly
	 * how a speaker that idled out while paused went unnoticed until someone pressed play.
	 */
	private suspend fun castSessionAlive(): Boolean {
		// Read the channel under the mutex teardownCast() nulls it under. Unsynchronised,
		// a teardown racing this returned a stale channel and the caller then drove it.
		val ch = castMutex.withLock { channel } ?: return false
		if (!ch.isOpen) return false
		return ch.mediaStatus(ALIVE_PROBE_TIMEOUT) != null
	}

	/** @return whether the speaker actually started — PROTOCOL §7.1's `loaded.ok`. */
	private suspend fun loadCurrent(positionMs: Long, play: Boolean): Boolean {
		val track = tracks.getOrNull(index) ?: run {
			// Silent before. "Hub sent a load we couldn't act on" and "we loaded and the speaker
			// ignored it" are very different bugs and produced identical (empty) logs.
			Logger.w(
				TAG,
				"$friendlyName: load ignored — index $index of ${tracks.size} track(s)"
			)
			return false
		}
		val url = track.streamUrl
		if (url.isNullOrBlank()) {
			Logger.e(
				TAG,
				"$friendlyName: \"${track.title}\" has no streamUrl — the queue was published " +
					"by an older client; start playback again on the sending device to republish it"
			)
			return false
		}

		Logger.i(TAG, "$friendlyName: loading \"${track.title}\" at ${positionMs}ms (play=$play)")

		val ch = ensureCast() ?: run {
			// Say so. Leaving `playing` true here is what left every client showing a running
			// scrubber for a silent speaker.
			Logger.e(TAG, "$friendlyName: giving up — no cast session")
			playing = false
			report()
			return false
		}

		// The speaker may already be playing this very track — that is the normal state of affairs
		// when a bridge rejoins a session that never stopped. LOADing it again seeks it back to the
		// hub's cursor, and the hub's cursor stopped advancing the moment we lost the socket:
		// audible as playback jumping backwards a second or two after the app reappears. A speaker
		// actively playing our URL is the authority on its own position — nothing else can be.
		// Only `isPlaying` earns that authority: a paused leftover session at the same contentId is
		// a stale cursor of exactly the kind the hub's position is meant to correct.
		if (play) {
			val live = runCatching { ch.mediaStatus(ALIVE_PROBE_TIMEOUT) }.getOrNull()
			if (live != null && live.isPlaying && live.media?.contentId == url) {
				live.positionMs?.let { lastPositionMs = it }
				playing = true
				Logger.i(
					TAG,
					"$friendlyName: already playing \"${track.title}\" at ${lastPositionMs}ms — " +
						"reporting it instead of reloading"
				)
				report()
				startTicker()
				return true
			}
		}

		val status = runCatching {
			ch.load(
				contentId = url,
				contentType = track.mime ?: "audio/mpeg",
				title = track.title,
				artist = track.artist,
				album = track.album,
				imageUrl = track.imageUrl,
				positionMs = positionMs,
				autoplay = play
			)
		}.getOrNull()

		if (status == null) {
			// The previous media session may be dead (typically after a release/stop). One retry
			// on a completely fresh session, then give up honestly.
			Logger.w(TAG, "$friendlyName: load failed, retrying on a fresh session")
			teardownCast()
			val fresh = ensureCast()
			val retried = fresh?.let {
				runCatching {
					it.load(
						contentId = url,
						contentType = track.mime ?: "audio/mpeg",
						title = track.title,
						artist = track.artist,
						album = track.album,
						imageUrl = track.imageUrl,
						positionMs = positionMs,
						autoplay = play
					)
				}.getOrNull()
			}
			if (retried == null) {
				Logger.e(TAG, "$friendlyName: load gave up for \"${track.title}\"")
				playing = false
				report()
				return false
			}
		}

		lastPositionMs = positionMs
		playing = play
		report()
		if (play) startTicker()
		return true
	}

	private suspend fun capturePosition() {
		// Keep the last good value if the device returns 0/nothing mid-transition — a 0 here is
		// exactly what reset transfers back to the start.
		channel?.mediaStatus(ALIVE_PROBE_TIMEOUT)?.positionMs?.let { lastPositionMs = it }
	}

	private suspend fun teardownCast() {
		tearingDown = true
		castMutex.withLock {
			runCatching { channel?.close() }
			channel = null
		}
		stopTicker()
		playing = false
		tearingDown = false
	}

	private fun onCastStatus(status: MediaStatus) {
		if (releasing) return
		if (status.errored) {
			Logger.e(
				TAG,
				"$friendlyName: playback error (unsupported format or unreachable streamUrl)"
			)
		}
		status.positionMs?.let { lastPositionMs = it }

		when {
			status.isPlaying -> {
				playing = true
				report()
				startTicker()
			}

			status.isPaused -> {
				playing = false
				report()
			}

			status.finished -> {
				// The bridge IS the receiver — nothing else advances this queue.
				if (index < tracks.size - 1) {
					index += 1
					scope.launch { loadCurrent(0L, true) }
				} else {
					playing = false
					lastPositionMs = 0L
					report(ended = true)
				}
			}
		}
	}

	// ------------------------------------------------------------------ adoption

	/**
	 * Re-join a session this speaker is still playing, rather than leaving it orphaned.
	 *
	 * Fires in two cases. The obvious one is the hub still naming us active. The important one is
	 * an ORPHANED session — the hub relinquishes the active slot whenever the active device drops,
	 * so after Navic restarts `activeDeviceId` is already null. Gating on "still ours" meant this
	 * never fired in the one case it was written for, and a speaker that was audibly still playing
	 * was treated as stopped by every client.
	 */
	private fun maybeAdopt(session: JsonObject) {
		if (destroyed) return
		val activeDeviceId = session["activeDeviceId"]?.jsonPrimitive?.contentOrNullSafe()
		// Another client owns the session now, so anything we adopted is moot — and if that client
		// later drops, that is a NEW orphaning and deserves its own attempt. Without this reset the
		// one-shot flag below would spend our single chance on the first frame after connecting.
		if (activeDeviceId != null && activeDeviceId != hubDeviceId) adopted = false
		if (adopted) return
		val queue = session["queue"]?.jsonArray?.toTracks() ?: emptyList()
		val stillOurs = activeDeviceId == hubDeviceId
		// Deliberately NOT gated on the session's `isPlaying`. The hub sets is_playing=false and
		// clears the active slot in the same breath whenever the active device disconnects
		// (hub.py `_disconnect`) — which is precisely our case: the phone slept, this bridge's
		// socket died, and the speaker carried on playing to an empty room. The flag therefore
		// reads "stopped" exactly when adoption is most needed, and requiring it made recovery
		// impossible in the only situation it exists for.
		//
		// What keeps this from touching speakers that are none of our business is no longer a
		// guess about the hub's state, but the probe itself: [adoptRunningSession] JOINS an
		// already-running Default Media Receiver and never launches one, so a speaker playing over
		// Bluetooth answers "nothing of yours here" and is left alone. [couldStillBePlaying] is
		// what stops us from even asking when the session cannot possibly still be running.
		val orphaned = activeDeviceId == null && queue.isNotEmpty() && couldStillBePlaying(session, queue)
		if (!stillOurs && !orphaned) return

		adopted = true
		tracks = queue
		index = session["index"]?.jsonPrimitive?.int ?: 0
		lastPositionMs = session["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
		savedQueueId = session["savedQueueId"]?.jsonPrimitive?.contentOrNullSafe()
		sourceKind = session["sourceKind"]?.jsonPrimitive?.contentOrNullSafe()
		sourceName = session["sourceName"]?.jsonPrimitive?.contentOrNullSafe()

		scope.launch { adoptRunningSession(claim = !stillOurs) }
	}

	/**
	 * Could this orphaned session still be coming out of the speaker?
	 *
	 * The hub cannot answer that — it marks the session stopped the instant the active device
	 * drops. But it does record WHEN that happened (`updatedAt`) and how much music was left, and
	 * a queue with twelve minutes remaining that was orphaned two hours ago is definitively over.
	 * That arithmetic is what keeps an ordinary app launch from reaching out to the speaker at all:
	 * the probe is harmless, but "harmless" is a weaker promise than "didn't happen".
	 *
	 * Every uncertainty resolves towards probing — an unknown timestamp, a queue with no durations,
	 * a clock skewed against the hub's. Being wrong here costs one LAN round-trip; being wrong the
	 * other way abandons a speaker that is still playing.
	 */
	private fun couldStillBePlaying(session: JsonObject, queue: List<BridgeTrack>): Boolean {
		val updatedAt = session["updatedAt"]?.jsonPrimitive?.longOrNull ?: return true
		val since = Clock.System.now().toEpochMilliseconds() - updatedAt
		if (since < 0) return true
		if (since > ADOPTION_MAX_AGE_MS) return false
		// `repeat: all` never runs out, so the ceiling above is the only bound that applies.
		if (session["repeat"]?.jsonPrimitive?.contentOrNullSafe() == "all") return true

		val from = (session["index"]?.jsonPrimitive?.int ?: 0).coerceIn(0, queue.size)
		val total = queue.drop(from).sumOf { it.durationMs }
		if (total <= 0) return true
		val positionMs = session["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
		return since < (total - positionMs) + ADOPTION_SLACK_MS
	}

	private suspend fun adoptRunningSession(claim: Boolean) {
		// Join-only. Adoption asks a question — "is this speaker still playing our session?" — and
		// launching the receiver to ask it makes the answer no: LAUNCH seizes the audio output, so
		// a speaker happily playing over Bluetooth fell silent every time Navic started, with
		// nothing taking its place. If the Default Media Receiver isn't already up, there is by
		// definition nothing of ours to adopt.
		val ch = ensureCast(allowLaunch = false) ?: run {
			Logger.i(TAG, "$friendlyName: nothing running on the speaker — not adopting")
			return
		}
		val status = ch.mediaStatus() ?: run {
			Logger.i(TAG, "$friendlyName: no running session to adopt")
			teardownCast()
			return
		}
		val contentId = status.media?.contentId

		// Claiming the idle active slot tells every client this speaker IS the session, so it has
		// to be earned: the device must be playing a track from that very queue. Someone else's
		// Spotify, or a stale receiver session left over from yesterday, matches nothing.
		val matches = contentId != null && tracks.any { it.streamUrl == contentId }
		if (claim && !matches) {
			Logger.i(TAG, "$friendlyName: running session is not ours — leaving it alone")
			// Drop the channel too. Holding a virtual connection into someone else's session is
			// not harmful, but it is a socket we have no business keeping, and the next real load
			// wants a fresh one anyway.
			teardownCast()
			return
		}

		status.positionMs?.let { lastPositionMs = it }
		playing = status.isPlaying
		if (contentId != null) {
			tracks.indexOfFirst { it.streamUrl == contentId }.takeIf { it >= 0 }?.let { index = it }
		}
		Logger.i(
			TAG,
			"$friendlyName: adopted running session (playing=$playing, index=$index, ${lastPositionMs}ms)"
		)

		// Take the active slot back BEFORE reporting: the hub discards a report from a device it
		// doesn't consider active, so the session would stay "stopped" however loudly the speaker
		// is playing. `setQueue` is the one frame that promotes an idle slot.
		if (claim) claimActive()
		report()
		if (playing) startTicker()
	}

	private suspend fun claimActive() {
		sendFrame(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", index)
			put("play", playing)
			put("positionMs", lastPositionMs)
			// Reuse the session's own saved-queue identity, or resuming forks a near-duplicate
			// history card for music that never actually stopped.
			savedQueueId?.let { put("savedQueueId", it) }
			sourceKind?.let { put("sourceKind", it) }
			sourceName?.let { put("sourceName", it) }
			put("tracks", JsonArray(tracks.map { it.raw }))
		})
		Logger.i(TAG, "$friendlyName: claimed the idle active slot (index=$index, ${lastPositionMs}ms)")
	}

	// ------------------------------------------------------------------ ticker

	private fun startTicker() {
		if (tickerJob?.isActive == true) return
		tickerJob = scope.launch {
			while (isActive && !destroyed) {
				delay(TICK_INTERVAL)
				if (!playing || releasing) continue
				val ch = channel ?: continue
				// One poll at a time; overlapping requests pile up on a slow channel.
				val status = runCatching { ch.mediaStatus() }.getOrNull()
				if (status == null) {
					Logger.w(TAG, "$friendlyName: status poll failed, dropping dead cast session")
					val wasPlaying = playing
					teardownCast()
					if (wasPlaying && !destroyed) runCatching { loadCurrent(lastPositionMs, true) }
					return@launch
				}
				status.positionMs?.let {
					lastPositionMs = it
					report()
				}
			}
		}
	}

	private fun stopTicker() {
		tickerJob?.cancel()
		tickerJob = null
	}
}

// ------------------------------------------------------------------ parsing

private fun kotlinx.serialization.json.JsonPrimitive.contentOrNullSafe(): String? =
	if (this is kotlinx.serialization.json.JsonNull) null else content.takeIf { it.isNotBlank() }

private fun JsonArray.toTracks(): List<BridgeTrack> = mapNotNull { element ->
	val obj = element as? JsonObject ?: return@mapNotNull null
	fun str(key: String) = obj[key]?.jsonPrimitive?.contentOrNullSafe()
	val id = str("id") ?: return@mapNotNull null
	BridgeTrack(
		id = id,
		title = str("title"),
		artist = str("artist"),
		album = str("album"),
		streamUrl = str("streamUrl"),
		mime = str("mime"),
		imageUrl = str("imageUrl"),
		durationMs = obj["durationMs"]?.jsonPrimitive?.longOrNull ?: 0L,
		raw = obj
	)
}
