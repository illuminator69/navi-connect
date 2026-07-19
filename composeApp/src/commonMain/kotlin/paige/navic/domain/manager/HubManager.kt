package paige.navic.domain.manager

import io.ktor.client.HttpClient
import io.ktor.client.plugins.websocket.DefaultClientWebSocketSession
import io.ktor.client.plugins.websocket.WebSockets
import io.ktor.client.plugins.websocket.webSocket
import io.ktor.websocket.Frame
import io.ktor.websocket.readText
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import paige.navic.data.database.dao.SongDao
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.models.DomainExplicitStatus
import paige.navic.domain.models.DomainSong
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.shared.RemotePlaybackRouter
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
import kotlin.time.Clock
import kotlin.time.Duration.Companion.milliseconds

data class HubDevice(
	val id: String,
	val name: String,
	val platform: String,
	val online: Boolean,
	val isActive: Boolean,
	val volume: Int
)

data class RemoteTrack(
	val id: String,
	val title: String,
	val artist: String,
	val album: String?,
	val durationMs: Long,
	val imageUrl: String?
)

/**
 * Mirror of the hub session for the remote-control UI: what's playing on the
 * ACTIVE device, with a wall-clock snapshot so the UI can tick the position
 * locally between the hub's ~1 Hz progress frames.
 */
data class RemoteSessionState(
	val tracks: List<RemoteTrack> = emptyList(),
	val index: Int = 0,
	val isPlaying: Boolean = false,
	val positionMs: Long = 0,
	val positionAtMs: Long = 0,
	val repeat: String = "none",
	val shuffle: Boolean = false
) {
	val nowPlaying: RemoteTrack? get() = tracks.getOrNull(index)
}

/**
 * navi-connect hub client: makes Navic a Spotify-Connect-style device against
 * the relay hub (see navi-connect PROTOCOL.md). Navic is both a receiver
 * (obeys `do` directives, reports position ~1 Hz while it is the active
 * device) and a controller (can transfer playback to other devices).
 *
 * Frames are plain JSON objects with a `t` discriminator, handled with the
 * dynamic JsonObject API rather than typed DTOs so protocol additions never
 * break deserialization.
 */
class HubManager(
	private val preferenceManager: PreferenceManager,
	private val sessionManager: SessionManager,
	private val songDao: SongDao,
	private val mediaPlayer: MediaPlayerViewModel
) : RemotePlaybackRouter {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
	private val client = HttpClient { install(WebSockets) }

	private val _connected = MutableStateFlow(false)
	val connected: StateFlow<Boolean> = _connected.asStateFlow()

	private val _devices = MutableStateFlow<List<HubDevice>>(emptyList())
	val devices: StateFlow<List<HubDevice>> = _devices.asStateFlow()

	private val _myDeviceId = MutableStateFlow<String?>(null)
	val myDeviceId: StateFlow<String?> = _myDeviceId.asStateFlow()

	private val _activeDeviceId = MutableStateFlow<String?>(null)
	val activeDeviceId: StateFlow<String?> = _activeDeviceId.asStateFlow()

	private val _remoteSession = MutableStateFlow(RemoteSessionState())
	val remoteSession: StateFlow<RemoteSessionState> = _remoteSession.asStateFlow()

	private val _isRemoteActive = MutableStateFlow(false)

	/** True when playback is live on ANOTHER device — show/route the remote view. */
	val isRemoteActive: StateFlow<Boolean> = _isRemoteActive.asStateFlow()

	/** When our socket dropped, so a brief reconnect doesn't tear the remote view down. */
	private var disconnectedAtMs = 0L

	/**
	 * Losing the socket does NOT move playback — it only blinds us. Tearing the remote view down
	 * on a blip made the UI fall back to the LOCAL player, which (untouched, at its defaults:
	 * isPaused=false, progress=0) renders as "playing, at 0:00" while the remote device carries on
	 * — the phantom unpause + progress-bar reset. So hold the remote view across a reconnect, and
	 * only release it back to local if the hub stays gone (otherwise an actually-dead hub would
	 * strand the user in a session they can't control).
	 */
	private fun evaluateRemoteActive() {
		val active = _activeDeviceId.value
		val me = _myDeviceId.value
		val remote = active != null && me != null && active != me
		_isRemoteActive.value = remote && (
			_connected.value || nowMs() - disconnectedAtMs < REMOTE_HOLD_MS
		)
	}

	private var connectJob: Job? = null
	private var wsSession: DefaultClientWebSocketSession? = null

	// Mirrors of the Feishin client's guards:
	private var lastQueueSig = ""        // dedupe for publishing our queue
	private var hubDrivenUntilMs = 0L    // player events caused by `do`, not the user
	private var lastReportedIndex = -2
	private var lastReportedPaused: Boolean? = null
	private var lastTickMs = 0L

	init {
		startRemoteMirror()
		// Queue-building from ANY screen (tap a song, shuffle an album, add to queue) now reaches
		// the active device through here instead of dead-ending in the local player.
		mediaPlayer.remotePlaybackRouter = this
	}

	// --- RemotePlaybackRouter ----------------------------------------------------------------

	override val isRemoteSessionActive: Boolean
		get() = isRemoteActive.value

	override fun setQueue(songs: List<DomainSong>, startIndex: Int) =
		loadSessionQueue(songs, startIndex)

	override fun enqueue(songs: List<DomainSong>, playNext: Boolean) =
		enqueueSessionQueue(songs, playNext)

	override fun restoreQueue(
		songs: List<DomainSong>,
		index: Int,
		positionMs: Long,
		play: Boolean
	) = loadSessionQueue(songs, index, positionMs, play)

	private fun nowMs(): Long = Clock.System.now().toEpochMilliseconds()

	private fun isActiveDevice(): Boolean {
		val me = _myDeviceId.value
		return me != null && me == _activeDeviceId.value
	}

	fun start() = restart()

	fun restart() {
		connectJob?.cancel()
		wsSession = null
		_connected.value = false
		if (!preferenceManager.hubEnabled) return
		connectJob = scope.launch { runLoop() }
	}

	fun stop() {
		connectJob?.cancel()
		wsSession = null
		_connected.value = false
	}

	/** Controller action: hand playback to another device (state preserved). */
	fun transfer(targetDeviceId: String) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "transfer")
			put("target", targetDeviceId)
		})
	}

	/**
	 * Append [songs] to the END of the session queue (plays on the active device).
	 * Used for "add to queue" from a controller while another device is active.
	 */
	fun enqueueSessionQueue(songs: List<DomainSong>, playNext: Boolean = false) {
		if (songs.isEmpty()) return
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "enqueue")
			put("at", if (playNext) "next" else "end")
			putJsonArray("tracks") {
				songs.forEach { song ->
					addJsonObject {
						put("id", song.id)
						put("title", song.title)
						put("artist", song.artistName)
						song.albumTitle?.let { put("album", it) }
						put("durationMs", song.duration.inWholeMilliseconds)
						song.coverArtId?.let { put("imageUrl", sessionManager.getCoverArtUrl(it)) }
						put("streamUrl", sessionManager.api.getStreamUrl(song.id))
						put("mime", song.mimeType)
					}
				}
			}
		})
	}

	/**
	 * Load [songs] as the session queue and play them on the ACTIVE device. Used
	 * when the user starts a generated mix (mood search / radio) from this device
	 * while ANOTHER device is the active receiver: the hub forwards a do:load to
	 * the active device, so the mix plays where playback currently is instead of
	 * starting a conflicting local playback on this device. No-op on empty queue.
	 */
	fun loadSessionQueue(
		songs: List<DomainSong>,
		startIndex: Int = 0,
		positionMs: Long = 0,
		play: Boolean = true
	) {
		if (songs.isEmpty()) return
		// Pre-set the publish signature so our own reporter doesn't re-publish this.
		lastQueueSig = queueSig(songs)
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", startIndex.coerceIn(0, songs.lastIndex))
			put("positionMs", positionMs.coerceAtLeast(0))
			put("play", play)
			putJsonArray("tracks") {
				songs.forEach { song ->
					addJsonObject {
						put("id", song.id)
						put("title", song.title)
						put("artist", song.artistName)
						song.albumTitle?.let { put("album", it) }
						put("durationMs", song.duration.inWholeMilliseconds)
						song.coverArtId?.let { put("imageUrl", sessionManager.getCoverArtUrl(it)) }
						put("streamUrl", sessionManager.api.getStreamUrl(song.id))
						put("mime", song.mimeType)
					}
				}
			}
		})
	}

	// Controller actions for the remote-control UI (drive the ACTIVE device).
	fun actPlayPause() = sendAct("playpause")
	fun actPlay() = sendAct("play")
	fun actPause() = sendAct("pause")
	fun actNext() = sendAct("next")
	fun actPrevious() = sendAct("previous")

	fun actSeek(positionMs: Long) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "seek")
			put("positionMs", positionMs)
		})
	}

	fun actJump(index: Int) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "jump")
			put("index", index)
		})
	}

	/** Set the ACTIVE remote device's volume (0..100). */
	fun actSetVolume(level: Int) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "volume")
			put("level", level.coerceIn(0, 100))
		})
	}

	/** Reorder the session queue on the active device (move [from] → [to]). */
	fun actMoveQueueItem(from: Int, to: Int) {
		if (from == to) return
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "move")
			put("from", from)
			put("to", to)
		})
	}

	/** Drop one item from the session queue on the active device. */
	fun actRemoveQueueItem(index: Int) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "remove")
			put("index", index)
		})
	}

	/** Empty the session queue on the active device (also stops playback). */
	fun actClearQueue() {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "clear")
		})
	}

	fun actToggleShuffle() {
		val on = !_remoteSession.value.shuffle
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "shuffle")
			put("on", on)
		})
	}

	fun actToggleRepeat() {
		val next = when (_remoteSession.value.repeat) {
			"none" -> "all"
			"all" -> "one"
			else -> "none"
		}
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "repeat")
			put("mode", next)
		})
	}

	private fun sendAct(action: String) {
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", action)
		})
	}

	fun currentTimeMs(): Long = nowMs()

	// ------------------------------------------------------------------ //
	// Remote-session mirror: while another device is active, push a resolved
	// snapshot of the hub session into the local player VM as a display override
	// so the entire player UI (mini player, now-playing, queue) reflects what's
	// actually playing — no per-component branching for DISPLAY (transport is
	// routed separately by the UI when isRemoteActive).
	// ------------------------------------------------------------------ //

	private var remoteUiBase: PlayerUiState? = null
	private var remoteSig = ""

	private fun repeatToInt(mode: String): Int = when (mode) {
		"one" -> 1
		"all" -> 2
		else -> 0
	}

	private fun startRemoteMirror() {
		scope.launch {
			combine(
				_connected, _activeDeviceId, _myDeviceId, _remoteSession
			) { _, _, _, session -> session }.collect { session ->
				evaluateRemoteActive()
				refreshMirror(session)
			}
		}
		// Ticks progress between the hub's ~1 Hz frames so the slider moves smoothly, and gives
		// the hold window above something to expire against while the session sits idle.
		scope.launch {
			while (currentCoroutineContext().isActive) {
				delay(250)
				evaluateRemoteActive()
				refreshMirror(_remoteSession.value)
			}
		}
	}

	/**
	 * Rebuilds the resolved queue only when the track set / index changes; otherwise just refreshes
	 * live progress (cheap). Re-pushing an unchanged snapshot is free — [PlayerUiState] is a data
	 * class, so the StateFlow dedupes it and nothing recomposes.
	 */
	private suspend fun refreshMirror(session: RemoteSessionState) {
		if (!_isRemoteActive.value) {
			remoteUiBase = null
			remoteSig = ""
			mediaPlayer.setRemoteState(null)
			return
		}
		val ids = session.tracks.map { it.id }
		val sig = ids.joinToString(",") + "|" + session.index
		if (sig != remoteSig) {
			remoteSig = sig
			// Resolve 1:1 with the session (placeholders for songs not in the
			// local DB) so the mirrored queue length + order MATCH the hub's —
			// otherwise dropping un-synced songs shifts the index, so the shown
			// "current song" is wrong and a jump sends the wrong index (which
			// made Feishin restart the queue).
			val resolved = resolveRemoteTracks(session.tracks)
			val idx = session.index.coerceIn(0, (resolved.size - 1).coerceAtLeast(0))
			remoteUiBase = PlayerUiState(
				queue = resolved,
				currentSong = resolved.getOrNull(idx),
				currentIndex = idx,
				isShuffleEnabled = session.shuffle,
				repeatMode = repeatToInt(session.repeat)
			)
		}
		pushRemoteProgress(session)
	}

	private fun pushRemoteProgress(session: RemoteSessionState) {
		val base = remoteUiBase ?: return
		val durationMs = session.nowPlaying?.durationMs ?: 0L
		val elapsed =
			if (session.isPlaying) (nowMs() - session.positionAtMs).coerceAtLeast(0) else 0L
		val positionMs = session.positionMs + elapsed
		val progress =
			if (durationMs > 0) (positionMs.toFloat() / durationMs).coerceIn(0f, 1f) else 0f
		mediaPlayer.setRemoteState(base.copy(isPaused = !session.isPlaying, progress = progress))
	}

	// ------------------------------------------------------------------ //
	// Connection
	// ------------------------------------------------------------------ //

	private suspend fun runLoop() {
		while (currentCoroutineContext().isActive) {
			try {
				connectOnce()
			} catch (e: Exception) {
				Logger.e("HubManager", "hub connection failed: ${e.message}", e)
			}
			wsSession = null
			if (_connected.value) disconnectedAtMs = nowMs()
			_connected.value = false
			evaluateRemoteActive()
			delay(3000)
		}
	}

	private suspend fun connectOnce() {
		val url = preferenceManager.hubUrl.trim()
		if (url.isEmpty()) {
			delay(10_000)
			return
		}
		client.webSocket(url) {
			wsSession = this
			sendFrame(buildJsonObject {
				put("t", "hello")
				put("token", preferenceManager.hubToken)
				putJsonObject("device") {
					val savedId = preferenceManager.hubDeviceId
					if (savedId.isNotEmpty()) put("id", savedId) else put("id", JsonNull)
					put("name", preferenceManager.hubDeviceName.ifBlank { "Navic" })
					put("platform", "android")
					putJsonArray("caps") {
						add("receiver")
						add("controller")
					}
				}
			})

			val reporter = launch { reporterLoop() }
			// Keepalive. A paused remote session is completely silent in BOTH directions (we only
			// report while we're the active device, and the hub only fans out progress off those
			// reports), so without this the socket idles out and reconnects — which is what made
			// the player blink back to the local state mid-pause.
			val pinger = launch { pingLoop() }
			try {
				for (frame in incoming) {
					if (frame is Frame.Text) {
						try {
							handleFrame(Json.parseToJsonElement(frame.readText()).jsonObject)
						} catch (e: Exception) {
							Logger.e("HubManager", "bad hub frame", e)
						}
					}
				}
			} finally {
				reporter.cancel()
				pinger.cancel()
			}
		}
	}

	/** The hub answers `ping` with `pong` and refreshes our last-seen. */
	private suspend fun pingLoop() {
		while (currentCoroutineContext().isActive) {
			delay(PING_INTERVAL_MS)
			try {
				sendFrame(buildJsonObject { put("t", "ping") })
			} catch (e: Exception) {
				Logger.e("HubManager", "hub ping failed", e)
				return
			}
		}
	}

	private suspend fun sendFrame(obj: JsonObject) {
		wsSession?.send(Frame.Text(obj.toString()))
	}

	private fun sendAsync(obj: JsonObject) {
		scope.launch {
			try {
				sendFrame(obj)
			} catch (e: Exception) {
				Logger.e("HubManager", "hub send failed", e)
			}
		}
	}

	// ------------------------------------------------------------------ //
	// Inbound frames
	// ------------------------------------------------------------------ //

	private suspend fun handleFrame(msg: JsonObject) {
		when (msg["t"]?.jsonPrimitive?.content) {
			"welcome" -> {
				val id = msg["deviceId"]?.jsonPrimitive?.content
				if (id != null) {
					_myDeviceId.value = id
					preferenceManager.hubDeviceId = id
				}
				msg["session"]?.let { applySession(it.jsonObject) }
				msg["devices"]?.let { parseDevices(it.asObjectList()) }
				_connected.value = true
				// Hub is authoritative: adopt its session rather than re-publishing ours.
				adoptIfNoLiveReceiver()
				Logger.i("HubManager", "connected to hub as $id")
			}

			// A session frame is the snapshot fields at the top level.
			"session" -> {
				applySession(msg)
				// Active device may have just dropped (activeId → null): adopt the
				// last-known queue locally, paused, so we're not stranded mirroring it.
				adoptIfNoLiveReceiver()
			}

			"progress" -> _remoteSession.value = _remoteSession.value.copy(
				index = msg["index"]?.jsonPrimitive?.intOrNull
					?: _remoteSession.value.index,
				isPlaying = msg["isPlaying"]?.jsonPrimitive?.booleanOrNull ?: false,
				positionMs = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L,
				positionAtMs = nowMs()
			)

			"devices" -> msg["devices"]?.let { parseDevices(it.asObjectList()) }

			"do" -> handleDo(msg)

			"error" -> Logger.e(
				"HubManager",
				"hub error: ${msg["code"]?.jsonPrimitive?.content} ${msg["message"]?.jsonPrimitive?.content}"
			)
		}
	}

	private fun applySession(obj: JsonObject) {
		_activeDeviceId.value =
			obj["activeDeviceId"]?.jsonPrimitive?.contentOrNullSafe()
		_remoteSession.value = RemoteSessionState(
			tracks = obj["queue"]?.asObjectList()?.mapNotNull { t ->
				val id = t["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
				RemoteTrack(
					id = id,
					title = t["title"]?.jsonPrimitive?.content ?: "",
					artist = t["artist"]?.jsonPrimitive?.content ?: "",
					album = t["album"]?.jsonPrimitive?.contentOrNullSafe(),
					durationMs = t["durationMs"]?.jsonPrimitive?.longOrNull ?: 0L,
					imageUrl = t["imageUrl"]?.jsonPrimitive?.contentOrNullSafe()
				)
			} ?: emptyList(),
			index = obj["index"]?.jsonPrimitive?.intOrNull ?: 0,
			isPlaying = obj["isPlaying"]?.jsonPrimitive?.booleanOrNull ?: false,
			positionMs = obj["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L,
			positionAtMs = nowMs(),
			repeat = obj["repeat"]?.jsonPrimitive?.contentOrNullSafe() ?: "none",
			shuffle = obj["shuffle"]?.jsonPrimitive?.booleanOrNull ?: false
		)
	}

	/**
	 * Hub-authoritative startup/reconnect + offline takeover: adopt the hub's session
	 * as our LOCAL queue (paused) instead of publishing our own (possibly stale) queue
	 * over it. Runs on `welcome` and every `session` frame.
	 *
	 * Only adopts when there is NO live receiver (activeId == null): another active
	 * device is handled by the display mirror ([refreshMirror]); when we're active the
	 * reporter owns publishing. Adopting loads PAUSED — a launching / taking-over client
	 * stays a controller until the user presses play (which claims active).
	 */
	private suspend fun adoptIfNoLiveReceiver() {
		if (_activeDeviceId.value != null) return
		val session = _remoteSession.value
		val hubSig = session.tracks.joinToString(",") { it.id }
		val local = mediaPlayer.localUiState.value
		val localSig = local.queue.joinToString(",") { it.id }
		val localPlaying = !local.isPaused && local.queue.isNotEmpty()

		// Live player of this exact queue (our socket blipped and reconnected while we
		// kept playing locally): the hub cleared active on our drop, so RE-CLAIM it.
		// Reset the publish signature so the reporter's next tick republishes our queue
		// (play=true) and the hub promotes us back to active — no reload, no pause.
		if (hubSig.isNotEmpty() && hubSig == localSig && localPlaying) {
			lastQueueSig = ""
			return
		}
		// Empty hub session: keep our local queue as the offline fallback; let the first
		// real user play publish it.
		if (hubSig.isEmpty()) {
			lastQueueSig = ""
			return
		}
		// Already adopted this exact queue — nothing to do (repeated session frames).
		if (hubSig == lastQueueSig && hubSig == localSig) return

		val songs = resolveRemoteTracks(session.tracks)
		if (songs.isEmpty()) return
		hubDrivenUntilMs = nowMs() + 2000
		lastQueueSig = hubSig
		mediaPlayer.loadRemoteQueue(songs, session.index, session.positionMs, play = false)
	}

	private fun parseDevices(arr: List<JsonObject>) {
		_devices.value = arr.mapNotNull { d ->
			val id = d["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
			HubDevice(
				id = id,
				name = d["name"]?.jsonPrimitive?.content ?: "Unknown",
				platform = d["platform"]?.jsonPrimitive?.content ?: "unknown",
				online = d["online"]?.jsonPrimitive?.booleanOrNull ?: false,
				isActive = d["isActive"]?.jsonPrimitive?.booleanOrNull ?: false,
				volume = d["volume"]?.jsonPrimitive?.intOrNull ?: 100
			)
		}
	}

	private suspend fun handleDo(msg: JsonObject) {
		hubDrivenUntilMs = nowMs() + 2000
		when (msg["cmd"]?.jsonPrimitive?.content) {
			"load" -> {
				val tracks = msg["tracks"]?.jsonArray?.map { it.jsonObject } ?: emptyList()
				// Resolve 1:1 (placeholders for un-synced songs) so the hub index
				// stays aligned — dropping a song would shift the index and play
				// the wrong track.
				val songs = resolveQueue(tracks)
				if (songs.isEmpty()) {
					Logger.e("HubManager", "do:load — empty track list")
					return
				}
				val index = msg["index"]?.jsonPrimitive?.intOrNull ?: 0
				val positionMs = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
				val play = msg["play"]?.jsonPrimitive?.booleanOrNull ?: true
				lastQueueSig = songs.joinToString(",") { it.id }
				mediaPlayer.loadRemoteQueue(songs, index, positionMs, play)
			}

			"play" -> mediaPlayer.resume()
			"pause" -> mediaPlayer.pause()
			"jump" -> mediaPlayer.playAt(msg["index"]?.jsonPrimitive?.intOrNull ?: 0)

			"seek" -> {
				val positionMs = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
				val durationMs = mediaPlayer.localUiState.value.currentSong
					?.duration?.inWholeMilliseconds ?: 0L
				if (durationMs > 0) {
					mediaPlayer.seek((positionMs.toFloat() / durationMs).coerceIn(0f, 1f))
				}
			}

			"queueChanged" -> {
				val tracks = msg["tracks"]?.jsonArray?.map { it.jsonObject } ?: emptyList()
				val songs = resolveQueue(tracks)
				if (songs.isEmpty()) return
				val index = msg["index"]?.jsonPrimitive?.intOrNull ?: 0
				lastQueueSig = songs.joinToString(",") { it.id }
				// Edits the queue around the current track without restarting
				// it (falls back to a reload when the current track changed).
				mediaPlayer.reconcileRemoteQueue(songs, index)
			}

			// Emptying the queue can't ride on `queueChanged` — it carries a track
			// list, and an empty one is indistinguishable from "nothing to apply".
			"clear" -> {
				lastQueueSig = ""
				mediaPlayer.clearQueue()
			}

			"setVolume" -> {
				val level = msg["level"]?.jsonPrimitive?.intOrNull ?: 100
				mediaPlayer.setPlayerVolume(level / 100f)
			}

			"setRepeat" -> {
				// media3 constants: 0 = off, 1 = one, 2 = all
				val mode = when (msg["mode"]?.jsonPrimitive?.content) {
					"one" -> 1
					"all" -> 2
					else -> 0
				}
				mediaPlayer.applyRemoteRepeat(mode)
			}

			"setShuffle" -> mediaPlayer.applyRemoteShuffle(
				msg["on"]?.jsonPrimitive?.booleanOrNull ?: false
			)

			"release" -> {
				// Final position report, THEN released — the hub uses that
				// report to resume the next device at our exact spot.
				mediaPlayer.pause()
				val state = mediaPlayer.localUiState.value
				val positionMs = state.currentSong?.duration
					?.inWholeMilliseconds?.let { (state.progress * it).toLong() } ?: 0L
				sendFrame(buildJsonObject {
					put("t", "report")
					put("positionMs", positionMs)
					put("index", state.currentIndex.coerceAtLeast(0))
					put("isPlaying", false)
				})
				sendFrame(buildJsonObject {
					put("t", "released")
					put("positionMs", positionMs)
					put("index", state.currentIndex.coerceAtLeast(0))
				})
			}
		}
	}

	private suspend fun resolveSongs(ids: List<String>): List<DomainSong> {
		if (ids.isEmpty()) return emptyList()
		// IN(...) queries don't preserve order — restore the hub's queue order.
		val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
		return ids.mapNotNull { byId[it]?.toDomainModel() }
	}

	/**
	 * Resolve a hub queue (full track objects) to DomainSongs, preserving the
	 * hub's order AND length 1:1 — songs missing from the local DB become
	 * placeholders synthesized from the hub track metadata. This is critical for
	 * transfer/queueChanged: dropping a missing song would shift every later
	 * index, so the hub's index would point to the wrong track (and Navic would
	 * play one song off). Placeholders still play + show art because Navidrome
	 * serves both stream and cover by song id (same server).
	 */
	private suspend fun resolveQueue(tracks: List<JsonObject>): List<DomainSong> {
		if (tracks.isEmpty()) return emptyList()
		val ids = tracks.mapNotNull { it["id"]?.jsonPrimitive?.content }
		val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
		return tracks.mapNotNull { track ->
			val id = track["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
			byId[id]?.toDomainModel() ?: remoteTrackToDomainSong(track)
		}
	}

	/** Minimal DomainSong for a hub track that isn't in the local library. */
	private fun remoteTrackToDomainSong(track: JsonObject): DomainSong {
		val id = track["id"]?.jsonPrimitive?.content ?: ""
		return DomainSong(
			id = id,
			title = track["title"]?.jsonPrimitive?.content ?: "",
			artistName = track["artist"]?.jsonPrimitive?.content ?: "",
			artistId = "",
			albumTitle = track["album"]?.jsonPrimitive?.contentOrNullSafe(),
			albumId = null,
			parentId = null,
			comment = null,
			trackNumber = null,
			discNumber = null,
			isrc = emptyList(),
			year = null,
			genre = null,
			genres = emptyList(),
			moods = emptyList(),
			duration = (track["durationMs"]?.jsonPrimitive?.longOrNull ?: 0L).milliseconds,
			bpm = null,
			contributors = emptyList(),
			playCount = 0,
			userRating = null,
			averageRating = null,
			bitRate = null,
			bitDepth = null,
			sampleRate = null,
			audioChannelCount = null,
			replayGain = null,
			fileSize = 0,
			fileExtension = "",
			mimeType = track["mime"]?.jsonPrimitive?.contentOrNullSafe() ?: "",
			filePath = null,
			starredAt = null,
			// Navidrome serves the cover by song id, so this lets art load.
			coverArtId = id,
			musicBrainzId = null,
			explicitStatus = DomainExplicitStatus.Unknown
		)
	}

	/** [resolveQueue] for the mirror's [RemoteTrack] list — 1:1, placeholders. */
	private suspend fun resolveRemoteTracks(tracks: List<RemoteTrack>): List<DomainSong> {
		if (tracks.isEmpty()) return emptyList()
		val byId = songDao.getSongsByIds(tracks.map { it.id }).associateBy { it.songId }
		return tracks.map { track ->
			byId[track.id]?.toDomainModel() ?: remoteTrackToDomainSong(track)
		}
	}

	/** Minimal DomainSong for a mirror [RemoteTrack] not in the local library. */
	private fun remoteTrackToDomainSong(track: RemoteTrack): DomainSong = DomainSong(
		id = track.id,
		title = track.title,
		artistName = track.artist,
		artistId = "",
		albumTitle = track.album,
		albumId = null,
		parentId = null,
		comment = null,
		trackNumber = null,
		discNumber = null,
		isrc = emptyList(),
		year = null,
		genre = null,
		genres = emptyList(),
		moods = emptyList(),
		duration = track.durationMs.milliseconds,
		bpm = null,
		contributors = emptyList(),
		playCount = 0,
		userRating = null,
		averageRating = null,
		bitRate = null,
		bitDepth = null,
		sampleRate = null,
		audioChannelCount = null,
		replayGain = null,
		fileSize = 0,
		fileExtension = "",
		mimeType = "",
		filePath = null,
		starredAt = null,
		// Navidrome serves the cover by song id, so this lets art load.
		coverArtId = track.id,
		musicBrainzId = null,
		explicitStatus = DomainExplicitStatus.Unknown
	)

	// ------------------------------------------------------------------ //
	// Outbound: reports + queue publishing
	// ------------------------------------------------------------------ //

	private suspend fun reporterLoop() {
		mediaPlayer.localUiState.collect { state ->
			try {
				val now = nowMs()
				val positionMs = state.currentSong?.duration
					?.inWholeMilliseconds?.let { (state.progress * it).toLong() } ?: 0L

				if (routeLocalPlayIfRemote(state, now)) return@collect
				publishQueueIfOurs(state, positionMs, now)

				if (!isActiveDevice()) return@collect

				val stateChanged = state.currentIndex != lastReportedIndex ||
					state.isPaused != lastReportedPaused
				val tickDue = !state.isPaused && now - lastTickMs >= 1000

				if (stateChanged || tickDue) {
					lastReportedIndex = state.currentIndex
					lastReportedPaused = state.isPaused
					lastTickMs = now
					sendFrame(buildJsonObject {
						put("t", "report")
						put("positionMs", positionMs)
						put("index", state.currentIndex.coerceAtLeast(0))
						put("isPlaying", !state.isPaused)
					})
				}
			} catch (e: Exception) {
				Logger.e("HubManager", "report failed", e)
			}
		}
	}

	private var lastRoutedAtMs = 0L

	// uiState emits every ~200ms while playing (progress ticks) but keeps the
	// SAME queue list instance — cache the joined-ids signature by reference so
	// we don't rebuild a potentially huge string on every tick (this caused
	// visible jank with large queues).
	private var sigCacheQueue: List<DomainSong>? = null
	private var sigCacheValue = ""

	private fun queueSig(queue: List<DomainSong>): String {
		if (queue !== sigCacheQueue) {
			sigCacheQueue = queue
			sigCacheValue = queue.joinToString(",") { it.id }
		}
		return sigCacheValue
	}

	/**
	 * Spotify semantics: if the user starts playback on THIS device while
	 * ANOTHER device is active, the music belongs to the session — send the
	 * local queue to the hub (which loads it on the active remote device) and
	 * silence the local player. Hub-driven events are exempt. Returns true
	 * when the play was routed (callers should skip publishing/reporting).
	 */
	private suspend fun routeLocalPlayIfRemote(
		state: paige.navic.ui.core.PlayerUiState,
		now: Long
	): Boolean {
		if (!_connected.value) return false
		// While another device is active, the system MediaSession is driven by
		// RemoteSessionPlayer (Android) which mirrors the remote into the local
		// controller's state. Re-routing that mirror back to the hub would echo
		// the remote queue / pause the remote, so don't. (New local playback
		// while remote is blocked by the facade — transfer here first.)
		if (isRemoteActive.value) return false
		if (state.isPaused || state.queue.isEmpty()) return false
		if (now < hubDrivenUntilMs) return false
		if (now - lastRoutedAtMs < 1000) return false
		val active = _activeDeviceId.value ?: return false
		val me = _myDeviceId.value ?: return false
		if (active == me) return false

		lastRoutedAtMs = now
		lastQueueSig = queueSig(state.queue)
		sendFrame(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", state.currentIndex.coerceAtLeast(0))
			put("positionMs", 0)
			put("play", true)
			putJsonArray("tracks") {
				state.queue.forEach { song ->
					addJsonObject {
						put("id", song.id)
						put("title", song.title)
						put("artist", song.artistName)
						song.albumTitle?.let { put("album", it) }
						put("durationMs", song.duration.inWholeMilliseconds)
						song.coverArtId?.let { put("imageUrl", sessionManager.getCoverArtUrl(it)) }
						// streamUrl/mime let URL-based receivers (Chromecast
						// bridge) play without speaking Subsonic.
						put("streamUrl", sessionManager.api.getStreamUrl(song.id))
						put("mime", song.mimeType)
					}
				}
			}
		})
		// The session plays it remotely — silence the local player.
		mediaPlayer.pause()
		return true
	}

	/**
	 * Publish our local queue as session intent when the user plays music on
	 * this device — this is how the hub learns the queue and how this device
	 * claims the active role. Never hijacks: only when no device is active or
	 * we already are, and never for hub-driven changes (do:load echoes).
	 */
	private suspend fun publishQueueIfOurs(
		state: paige.navic.ui.core.PlayerUiState,
		positionMs: Long,
		now: Long
	) {
		if (!_connected.value) return
		if (state.isPaused || state.queue.isEmpty()) return
		if (now < hubDrivenUntilMs) return
		val active = _activeDeviceId.value
		val me = _myDeviceId.value
		if (!(active == null || (me != null && active == me))) return

		val sig = queueSig(state.queue)
		if (sig == lastQueueSig) return
		lastQueueSig = sig

		sendFrame(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", state.currentIndex.coerceAtLeast(0))
			put("positionMs", positionMs)
			put("play", true)
			putJsonArray("tracks") {
				state.queue.forEach { song ->
					addJsonObject {
						put("id", song.id)
						put("title", song.title)
						put("artist", song.artistName)
						song.albumTitle?.let { put("album", it) }
						put("durationMs", song.duration.inWholeMilliseconds)
						song.coverArtId?.let { put("imageUrl", sessionManager.getCoverArtUrl(it)) }
						// streamUrl/mime let URL-based receivers (Chromecast
						// bridge) play without speaking Subsonic.
						put("streamUrl", sessionManager.api.getStreamUrl(song.id))
						put("mime", song.mimeType)
					}
				}
			}
		})
	}

	private companion object {
		/** Matches the hub's PING_INTERVAL, so a silent (paused) session still holds the socket. */
		const val PING_INTERVAL_MS = 10_000L

		/**
		 * How long the remote view survives a lost socket before falling back to local. Comfortably
		 * longer than the 3s reconnect backoff, short enough that a genuinely dead hub hands control
		 * back rather than stranding the user in a session they can't drive.
		 */
		const val REMOTE_HOLD_MS = 30_000L
	}
}

// Small helpers for tolerant JSON access ------------------------------------

private fun kotlinx.serialization.json.JsonPrimitive.contentOrNullSafe(): String? =
	if (this is JsonNull) null else content

private fun kotlinx.serialization.json.JsonElement.asObjectList(): List<JsonObject> =
	// tolerate non-array payloads

	try {
		jsonArray.map { it.jsonObject }
	} catch (_: Exception) {
		emptyList()
	}
