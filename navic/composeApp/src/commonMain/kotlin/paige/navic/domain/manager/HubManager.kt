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
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.models.DomainExplicitStatus
import paige.navic.domain.models.DomainSong
import paige.navic.domain.repositories.SavedQueueRepository
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
	/**
	 * A socket claiming this id is attached to the hub.
	 *
	 * For a bridged Chromecast that is the *bridging client's* socket, which lives on some other
	 * phone or desktop — it says nothing about whether the speaker is powered on or even on the
	 * same continent. See [reachable]; PROTOCOL §3.2.
	 */
	val online: Boolean,
	val isActive: Boolean,
	val volume: Int,
	/**
	 * The bridge's verdict on the hardware: true reached recently, false connected-but-silent,
	 * null unknown or not applicable — which is every ordinary client, since a client is its own
	 * hardware and its socket already proves it.
	 */
	val reachable: Boolean? = null,
	/** For a virtual device, the hub id of the client bridging it. */
	val bridgedBy: String? = null
) {
	/** Can the session be handed here right now? Unknown reachability is permissive. */
	val transferable: Boolean get() = online && reachable != false
}

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
	val shuffle: Boolean = false,
	/** Saved-queue history id of the current session (the "Now Playing" record), or null. */
	val savedQueueId: String? = null
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
	private val mediaPlayer: MediaPlayerViewModel,
	private val savedQueueRepository: SavedQueueRepository,
	private val lbBotManager: LbBotManager
) : RemotePlaybackRouter {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
	private val client = HttpClient {
		install(WebSockets) {
			// Protocol-level WS pings so a half-open socket (Wi-Fi→cellular, NAT
			// timeout) is detected and torn down instead of blocking `incoming`
			// forever — the app-level `ping`/`pong` only refreshes the hub's
			// last-seen and can't detect a dead link on its own.
			pingIntervalMillis = 10_000
		}
	}

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

	/**
	 * Last hub-side error the user should know about (bad token, target offline, …).
	 * The UI (device picker / NaviConnect settings) can surface it as a snackbar —
	 * without this a wrong token just looked like "never connects". Cleared on a
	 * successful `welcome`.
	 */
	private val _connectionError = MutableStateFlow<String?>(null)
	val connectionError: StateFlow<String?> = _connectionError.asStateFlow()

	/** Set when the hub rejects our token, so the reconnect loop stops tight-looping. */
	private var authFailed = false

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
	// Have we ever put a non-empty queue on the wire? Gates the clear-propagation below, so the empty
	// queue that exists for a moment during startup hydration can't wipe another device's session.
	private var publishedNonEmptyQueue = false
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

	override val isHubConnected: Boolean
		get() = _connected.value

	override fun setQueue(
		songs: List<DomainSong>,
		startIndex: Int,
		sourceKind: String,
		sourceName: String?
	) = loadSessionQueue(songs, startIndex, sourceKind = sourceKind, sourceName = sourceName)

	override fun enqueue(songs: List<DomainSong>, playNext: Boolean) =
		enqueueSessionQueue(songs, playNext)

	override fun restoreQueue(
		songs: List<DomainSong>,
		index: Int,
		positionMs: Long,
		play: Boolean,
		savedQueueId: String?,
		sourceKind: String,
		sourceName: String?
	) = loadSessionQueue(
		songs, index, positionMs, play,
		savedQueueId = savedQueueId, sourceKind = sourceKind, sourceName = sourceName
	)

	override fun seek(positionMs: Long) = actSeek(positionMs)

	/**
	 * Force the local queue back onto the hub even though its contents are unchanged. Clearing the
	 * dedupe signature is the whole trick: the publish path exists to send one frame per queue change,
	 * which is exactly wrong when what changed is the queue's *identity* (its record was deleted).
	 */
	override fun republishQueue() {
		lastQueueSig = ""
		scope.launch {
			try {
				val state = mediaPlayer.localUiState.value
				if (state.queue.isEmpty()) return@launch
				publishQueueIfOurs(state, positionMs = 0, now = nowMs())
			} catch (e: Exception) {
				Logger.e("HubManager", "queue republish failed", e)
			}
		}
	}

	/**
	 * Tell the hub the queue was emptied here. `publishQueueIfOurs` can't: it returns early on an empty
	 * queue (and on a paused one), so a local clear never left the device and the hub went on serving
	 * a session the user had thrown away. Guarded on having actually published something, so the
	 * momentarily-empty queue during startup can't wipe a session another device is playing.
	 */
	override fun clearSessionQueue() {
		if (!_connected.value || !publishedNonEmptyQueue) return
		publishedNonEmptyQueue = false
		lastQueueSig = ""
		actClearQueue()
	}

	override suspend fun resolveLibrarySongs(songs: List<DomainSong>): List<DomainSong> {
		if (songs.isEmpty()) return songs
		val byId = songDao.getSongsByIds(songs.map { it.id }).associateBy { it.songId }
		// 1:1 and in order — a missing song keeps its placeholder rather than being dropped, or every
		// index after it (including the one we're about to resume at) would shift.
		return songs.map { byId[it.id]?.toDomainModel() ?: it }
	}

	private fun nowMs(): Long = Clock.System.now().toEpochMilliseconds()

	/** Clear the reporter/publish dedupe state (new socket, or we just handed off). */
	private fun resetReporterGuards() {
		lastQueueSig = ""
		lastReportedIndex = -2
		lastReportedPaused = null
		lastTickMs = 0L
		lastClaimPublishAtMs = 0L
	}

	private fun isActiveDevice(): Boolean {
		val me = _myDeviceId.value
		return me != null && me == _activeDeviceId.value
	}

	fun start() = restart()

	fun restart() {
		// Join the previous connect job before starting a new one — cancelling
		// without joining let the old coroutine race the new one over the shared
		// `wsSession` (duplicate/stale sockets). restart() runs on every settings
		// change, so this race was easy to hit.
		val old = connectJob
		_connected.value = false
		authFailed = false
		_connectionError.value = null
		connectJob = scope.launch {
			old?.cancelAndJoin()
			wsSession = null
			if (!preferenceManager.hubEnabled) return@launch
			runLoop()
		}
	}

	fun stop() {
		connectJob?.cancel()
		wsSession = null
		_connected.value = false
	}

	/** Controller action: hand playback to another device (state preserved). */
	fun transfer(targetDeviceId: String) {
		// Transferring to the device that's already active is a no-op. (The hub guards
		// this too; not sending at all also keeps the picker from flickering.)
		if (targetDeviceId == _activeDeviceId.value) return
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
		play: Boolean = true,
		savedQueueId: String? = null,
		sourceKind: String = "manual",
		sourceName: String? = null
	) {
		if (songs.isEmpty()) return
		// Pre-set the publish signature so our own reporter doesn't re-publish this.
		lastQueueSig = queueSig(songs)
		publishedNonEmptyQueue = true
		sendAsync(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", startIndex.coerceIn(0, songs.lastIndex))
			put("positionMs", positionMs.coerceAtLeast(0))
			put("play", play)
			// Identity comes from the CALLER (the album/playlist/mix being played), not from
			// local player state — that describes the queue we're replacing, so reading it here
			// stamped every remote-routed queue with the previous one's name (usually none).
			// A genuinely new queue mints an id; a restore/undo passes the record's own id.
			put("savedQueueId", savedQueueId ?: mediaPlayer.newQueueSessionId(songs))
			put("sourceKind", sourceKind)
			sourceName?.let { put("sourceName", it) }
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
		var backoffMs = INITIAL_BACKOFF_MS
		while (currentCoroutineContext().isActive) {
			try {
				connectOnce()
			} catch (e: Exception) {
				Logger.e("HubManager", "hub connection failed: ${e.message}", e)
			}
			val wasConnected = _connected.value
			wsSession = null
			if (wasConnected) {
				disconnectedAtMs = nowMs()
				backoffMs = INITIAL_BACKOFF_MS  // the link worked; reset the backoff
			}
			_connected.value = false
			evaluateRemoteActive()
			// The hub rejected our token — retrying with the same bad token would
			// tight-loop forever and never surface the error. Stop; a settings
			// change (which calls restart()) clears the flag and tries again.
			if (authFailed) {
				Logger.e("HubManager", "hub auth rejected — stopping reconnect until settings change")
				return
			}
			delay(backoffMs)
			backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
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
						// PROTOCOL §7.1: we answer every transfer's do:load with a `loaded` frame,
						// so a transfer here that can't start hands the session back instead of
						// reading as playing on every device.
						add("loadAck")
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

	// Frames sent fire-and-forget still have to reach the hub IN ORDER: each sendAsync is its own
	// coroutine, so without this a burst (say a batch of deletes racing a setQueue) could arrive
	// reordered and the hub would apply them in the wrong sequence.
	private val sendMutex = Mutex()

	private fun sendAsync(obj: JsonObject) {
		scope.launch {
			try {
				sendMutex.withLock { sendFrame(obj) }
			} catch (e: Exception) {
				Logger.e("HubManager", "hub send failed", e)
			}
		}
	}

	// ------------------------------------------------------------------ //
	// Saved-queue history (Continue Listening) — hub-owned + shared.
	// ------------------------------------------------------------------ //

	/** Rename a shared saved-queue record (propagates to every client). */
	fun actRenameSavedQueue(id: String, name: String?) {
		sendAsync(buildJsonObject {
			put("t", "act"); put("action", "renameSavedQueue"); put("id", id)
			name?.let { put("name", it) }
		})
	}

	/** Delete a shared saved-queue record (propagates to every client). */
	fun actDeleteSavedQueue(id: String) {
		sendAsync(buildJsonObject {
			put("t", "act"); put("action", "deleteSavedQueue"); put("id", id)
		})
	}

	/**
	 * Delete several records in one act. Clear-all and delete-others used to send one frame per row,
	 * and the hub answers each with a full history broadcast plus a state write — twenty deletions
	 * meant twenty of both, all of which every client then re-applied to its local table.
	 */
	fun actDeleteSavedQueues(ids: List<String>) {
		if (ids.isEmpty()) return
		sendAsync(buildJsonObject {
			put("t", "act"); put("action", "deleteSavedQueues")
			putJsonArray("ids") { ids.forEach { add(it) } }
		})
	}

	private fun intToRepeat(mode: Int): String = when (mode) {
		1 -> "one"; 2 -> "all"; else -> "none"
	}

	/**
	 * What to call this queue in the shared history. [PlayerUiState.savedQueueName] is stamped by
	 * whoever replaced the queue and is therefore always right; `currentCollection` is the older,
	 * weaker guess (it resolves asynchronously from the playing song, and is null for every generated
	 * mix) and stays only as a fallback for queues built before the stamp existed.
	 */
	private fun savedQueueNameFor(state: PlayerUiState): String? =
		state.savedQueueName ?: state.currentCollection?.name

	// NOTE: we deliberately do NOT publish `coverImageUrl`. Card art is now derived by each client
	// from the record's resume track, rendered by id with that client's own credentials — a URL
	// carrying OUR server address and OUR auth token is not something the other client can display.

	/**
	 * Adopt the hub's authoritative saved-queue history into the local Room store (a REPLACE — that's
	 * what lets a delete on one client propagate here). Hub tracks are minimal, so they're resolved
	 * 1:1 to DomainSong placeholders, same as the live mirror.
	 */
	private suspend fun applySavedQueues(records: List<JsonObject>) {
		// One library lookup for the WHOLE broadcast. Resolving per record meant twenty queries every
		// time any client edited any queue, since the hub rebroadcasts the entire list each time.
		val allTracks = records.flatMap { it["songs"]?.asObjectList() ?: emptyList() }
		val byId = songDao
			.getSongsByIds(allTracks.mapNotNull { it["id"]?.jsonPrimitive?.content }.distinct())
			.associateBy { it.songId }
		val out = records.mapNotNull { rec ->
			val id = rec["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
			// A broadcast built before our sync-up frame landed still carries rows we deleted while
			// offline; adopting them would put the deleted card back for a render.
			if (savedQueueRepository.isDeleted(id)) return@mapNotNull null
			val tracks = rec["songs"]?.asObjectList() ?: emptyList()
			if (tracks.isEmpty()) return@mapNotNull null
			val songs = tracks.mapNotNull { track ->
				val songId = track["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
				byId[songId]?.toDomainModel() ?: remoteTrackToDomainSong(track)
			}
			if (songs.isEmpty()) return@mapNotNull null
			val idx = (rec["currentIndex"]?.jsonPrimitive?.intOrNull ?: 0)
				.coerceIn(0, (songs.size - 1).coerceAtLeast(0))
			SavedQueueRepository.RemoteSavedQueue(
				id = id,
				name = rec["name"]?.jsonPrimitive?.contentOrNullSafe(),
				sourceName = rec["sourceName"]?.jsonPrimitive?.contentOrNullSafe(),
				songs = songs,
				currentIndex = idx,
				currentSongId = songs.getOrNull(idx)?.id,
				currentSongName = songs.getOrNull(idx)?.title,
				// Art comes from the queue's FIRST track — its birth — not from the resume cursor:
				// the hub and Feishin freeze `coverImageUrl` at birth (PROTOCOL.md §8.3), so deriving
				// it from the cursor here made one shared record look different on each client. The
				// record's own `coverImageUrl` stays ignored: it's the other client's authed URL
				// against its own server, which is exactly why those covers wouldn't load.
				coverArtId = songs.firstOrNull()?.coverArtId,
				positionMs = rec["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L,
				shuffle = rec["shuffle"]?.jsonPrimitive?.booleanOrNull ?: false,
				repeatMode = repeatToInt(rec["repeat"]?.jsonPrimitive?.contentOrNullSafe() ?: "none"),
				sourceKind = rec["sourceKind"]?.jsonPrimitive?.contentOrNullSafe() ?: "manual",
				createdAt = rec["createdAt"]?.jsonPrimitive?.longOrNull ?: nowMs(),
				updatedAt = rec["updatedAt"]?.jsonPrimitive?.longOrNull ?: nowMs(),
				songCount = rec["songCount"]?.jsonPrimitive?.intOrNull ?: songs.size
			)
		}
		savedQueueRepository.replaceFromHub(out)
	}

	/**
	 * Push our local (possibly offline-accumulated) history up so it survives + propagates — including
	 * deletions, which the hub tombstones. Without those a delete made while offline was silently
	 * undone: the row went from Room, then this sync pushed the survivors up and the hub's reply
	 * (which still had the deleted record) put it straight back.
	 */
	private suspend fun syncLocalSavedQueuesUp() {
		// Decoded up front so a row whose blob is unreadable is dropped here (decodeQueue logs it)
		// rather than uploaded with an empty `songs`, which the hub silently rejects — the record
		// would then look synced while never actually reaching the shared history.
		val rows = savedQueueRepository.allForSync()
			.map { it to savedQueueRepository.decodeQueue(it) }
			.filter { (_, songs) -> songs.isNotEmpty() }
		val deleted = savedQueueRepository.pendingTombstoneIds()
		if (rows.isEmpty() && deleted.isEmpty()) return
		sendFrame(buildJsonObject {
			put("t", "act"); put("action", "syncSavedQueues")
			if (deleted.isNotEmpty()) {
				putJsonArray("deleted") { deleted.forEach { add(it) } }
			}
			putJsonArray("queues") {
				rows.forEach { (row, songs) ->
					addJsonObject {
						put("id", row.id)
						row.name?.let { put("name", it) }
						row.sourceName?.let { put("sourceName", it) }
						put("sourceKind", row.sourceKind)
						put("currentIndex", row.currentIndex)
						put("positionMs", row.positionMs)
						put("shuffle", row.shuffle)
						put("repeat", intToRepeat(row.repeatMode))
						put("songCount", row.songCount)
						put("createdAt", row.createdAt)
						put("updatedAt", row.updatedAt)
						putJsonArray("songs") {
							songs.forEach { song ->
								addJsonObject {
									put("id", song.id)
									put("title", song.title)
									put("artist", song.artistName)
									song.albumTitle?.let { put("album", it) }
									put("durationMs", song.duration.inWholeMilliseconds)
									song.coverArtId?.let {
										put("imageUrl", sessionManager.getCoverArtUrl(it))
									}
								}
							}
						}
					}
				}
			}
		})
	}

	/**
	 * The saved-queue id to publish for [state]'s queue: reuse the local session id when present,
	 * else the hub's current record id when it's substantially the same queue (adopt/transfer-in),
	 * else the id of a record we already hold for this queue, else a fresh one.
	 *
	 * The hub-adopt test is deliberately **set overlap**, not the ordered prefix it used to be: a
	 * reorder, a removal, a play-next or a shuffle all fail a prefix test, so every one of them forked
	 * a brand-new record for a queue the user never stopped playing. Half the queue in common is
	 * enough — the same threshold Feishin uses.
	 */
	private fun hubSavedQueueIdFor(state: PlayerUiState): String {
		state.savedQueueId?.let { return it }
		val ids = state.queue.map { it.id }
		val hub = _remoteSession.value
		val hubIds = hub.tracks.map { it.id }.toSet()
		val hubId = hub.savedQueueId
		if (hubId != null && hubIds.isNotEmpty() && ids.isNotEmpty()) {
			val shared = ids.toSet().count { it in hubIds }
			if (shared.toDouble() / hubIds.size >= HUB_QUEUE_ADOPT_OVERLAP) return hubId
		}
		return mediaPlayer.newQueueSessionId(state.queue)
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
				_connectionError.value = null
				// A new socket means the reporter's dedupe state describes a session
				// that no longer exists — clear it so the first tick after reconnect
				// actually reports instead of being swallowed as "unchanged".
				resetReporterGuards()
				// Reconcile saved-queue history: push OUR local (possibly offline)
				// rows up first, then adopt the hub's authoritative list. The hub
				// union-merges our rows and rebroadcasts the complete set.
				// Isolated: a Room/serialization failure here must not skip the adopt
				// below, which is what actually re-syncs playback.
				try {
					syncLocalSavedQueuesUp()
				} catch (e: Exception) {
					Logger.e("HubManager", "saved-queue sync-up failed", e)
				}
				// Isolated for the same reason: a failure adopting history must not take
				// down the reconnect handshake.
				try {
					applySavedQueues(msg["savedQueues"]?.asObjectList() ?: emptyList())
				} catch (e: Exception) {
					Logger.e("HubManager", "saved-queue adopt failed", e)
				}
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

			// A partial progress frame must NOT reset the fields it omits: defaulting
			// a missing isPlaying to false / positionMs to 0 snapped the scrubber and
			// flickered a paused state. Keep the previous mirror value for anything absent.
			"progress" -> {
				val prev = _remoteSession.value
				_remoteSession.value = prev.copy(
					index = msg["index"]?.jsonPrimitive?.intOrNull ?: prev.index,
					isPlaying = msg["isPlaying"]?.jsonPrimitive?.booleanOrNull ?: prev.isPlaying,
					positionMs = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: prev.positionMs,
					positionAtMs = nowMs()
				)
			}

			"devices" -> msg["devices"]?.let { parseDevices(it.asObjectList()) }

			"savedQueues" -> try {
				applySavedQueues(msg["queues"]?.asObjectList() ?: emptyList())
			} catch (e: Exception) {
				Logger.e("HubManager", "saved-queue broadcast adopt failed", e)
			}

			"do" -> handleDo(msg)

			// lb-bot placed an album somewhere in the library — possibly at another
			// client's request. The frame carries no authority: it only says "re-read
			// data you can already read", so it needs no validation beyond the socket's,
			// and missing one costs nothing, because the index flip upstream is durable
			// and the next read is right regardless.
			"library" -> lbBotManager.onLibraryChanged()

			"error" -> {
				val code = msg["code"]?.jsonPrimitive?.content
				val message = msg["message"]?.jsonPrimitive?.content
				Logger.e("HubManager", "hub error: $code $message")
				_connectionError.value = when (code) {
					// Both mean "the transfer you just asked for did not happen" — say which,
					// because the two have different fixes (turn the speaker on vs. try again).
					"target_unreachable" -> message ?: "That device isn't responding"
					"load_failed" -> message ?: "That device could not start playback"
					else -> message ?: code
				}
				// A bad token never resolves by retrying — stop the reconnect loop.
				if (code == "auth") authFailed = true
			}
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
			shuffle = obj["shuffle"]?.jsonPrimitive?.booleanOrNull ?: false,
			savedQueueId = obj["savedQueueId"]?.jsonPrimitive?.contentOrNullSafe()
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
		// We already hold this exact queue, paused, at (about) the hub's cursor — repeated
		// session frames must not reload it. Only reload when the cursor genuinely differs:
		// that's the "the other device was force-stopped, resume where IT left off" case,
		// and it's what makes a takeover continue from the right spot.
		if (hubSig == localSig) {
			val localPositionMs = local.currentSong?.duration?.inWholeMilliseconds
				?.let { (local.progress * it).toLong() } ?: 0L
			val inSync = local.currentIndex == session.index &&
				(localPositionMs - session.positionMs) in -2_000..2_000
			if (inSync) return
		}

		val songs = resolveRemoteTracks(session.tracks)
		if (songs.isEmpty()) return
		hubDrivenUntilMs = nowMs() + 2000
		// Do NOT pin the signature to the hub's: publishQueueIfOurs bails while sig ==
		// lastQueueSig, so pinning it here meant a reopened app could never publish and
		// therefore never be promoted back to active — it sat connected but mute.
		lastQueueSig = ""
		// Carry the hub's record identity into local state so the "Now Playing" highlight
		// survives the adopt (a null id here read as "this queue isn't in history").
		mediaPlayer.loadRemoteQueue(
			songs, session.index, session.positionMs, play = false,
			savedQueueId = session.savedQueueId,
			savedQueueKind = savedQueueKindFor(session.savedQueueId)
		)
	}

	/** The kind stored on the hub record with this id, if we know it (else "manual"). */
	private suspend fun savedQueueKindFor(id: String?): String {
		if (id == null) return "manual"
		return savedQueueRepository.get(id)?.sourceKind ?: "manual"
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
				volume = d["volume"]?.jsonPrimitive?.intOrNull ?: 100,
				reachable = d["reachable"]?.jsonPrimitive?.booleanOrNull,
				bridgedBy = d["bridgedBy"]?.jsonPrimitive?.contentOrNullSafe()
			)
		}
	}

	/** Acknowledge a transfer's `do:load` — see PROTOCOL §7.1. */
	private suspend fun sendLoaded(ok: Boolean, error: String?) {
		sendAsync(buildJsonObject {
			put("t", "loaded")
			put("ok", ok)
			if (error != null) put("error", error)
		})
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
					// PROTOCOL §7.1: the hub commits the active slot before sending this load, so
					// staying silent would leave the whole stack believing this device took a
					// session it cannot play a note of.
					sendLoaded(false, "nothing in the queue resolved")
					return
				}
				val index = msg["index"]?.jsonPrimitive?.intOrNull ?: 0
				val positionMs = msg["positionMs"]?.jsonPrimitive?.longOrNull ?: 0L
				val play = msg["play"]?.jsonPrimitive?.booleanOrNull ?: true
				lastQueueSig = songs.joinToString(",") { it.id }
				mediaPlayer.loadRemoteQueue(songs, index, positionMs, play)
				sendLoaded(true, null)
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
				// We're no longer the active device; the next time we claim it, the
				// guards must not still describe this handed-off session.
				resetReporterGuards()
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
	private var lastClaimPublishAtMs = 0L

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
		publishedNonEmptyQueue = true
		val savedQueueId = hubSavedQueueIdFor(state)
		mediaPlayer.adoptQueueSessionId(savedQueueId)
		sendFrame(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", state.currentIndex.coerceAtLeast(0))
			put("positionMs", 0)
			put("play", true)
			put("savedQueueId", savedQueueId)
			put("sourceKind", state.savedQueueKind)
			savedQueueNameFor(state)?.let { put("sourceName", it) }
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
		// Normally one publish per queue change. But when NOBODY is active and we're
		// playing, publishing is also how we CLAIM the session — and after adopting the
		// hub's queue our signature already equals it, so the dedupe would block the
		// claim forever (connected, playing locally, but never promoted → no reports).
		// Re-publish in that state, throttled so the ~5 Hz state flow doesn't spam.
		val unclaimed = active == null
		if (sig == lastQueueSig && !(unclaimed && now - lastClaimPublishAtMs >= CLAIM_REPUBLISH_MS)) return
		if (unclaimed) lastClaimPublishAtMs = now
		lastQueueSig = sig
		publishedNonEmptyQueue = true

		// Remembered on the player, or the claim re-publish loop above would re-derive it every
		// pass and — before the hub's broadcast round-trips back into the local index — could mint
		// a different id each time, forking one history card per republish.
		val savedQueueId = hubSavedQueueIdFor(state)
		mediaPlayer.adoptQueueSessionId(savedQueueId)
		sendFrame(buildJsonObject {
			put("t", "act")
			put("action", "setQueue")
			put("index", state.currentIndex.coerceAtLeast(0))
			put("positionMs", positionMs)
			put("play", true)
			put("savedQueueId", savedQueueId)
			put("sourceKind", state.savedQueueKind)
			savedQueueNameFor(state)?.let { put("sourceName", it) }
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

		/** Reconnect backoff: the protocol mandates exponential backoff, not a fixed retry. */
		const val INITIAL_BACKOFF_MS = 1_000L
		const val MAX_BACKOFF_MS = 30_000L

		/**
		 * How long the remote view survives a lost socket before falling back to local. Comfortably
		 * longer than the 3s reconnect backoff, short enough that a genuinely dead hub hands control
		 * back rather than stranding the user in a session they can't drive.
		 */
		const val REMOTE_HOLD_MS = 30_000L

		/** Throttle for the unclaimed-session re-publish (the "claim active" path). */
		const val CLAIM_REPUBLISH_MS = 2_000L

		/**
		 * Share of the hub session's tracks our queue must contain for it to count as the same
		 * listening session (so we refresh the hub's record instead of forking one). Half, matching
		 * Feishin — a shuffle or a heavy edit still leaves that much in common.
		 */
		const val HUB_QUEUE_ADOPT_OVERLAP = 0.5
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
