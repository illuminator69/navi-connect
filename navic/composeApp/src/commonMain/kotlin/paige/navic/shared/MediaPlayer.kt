package paige.navic.shared

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.sample
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import paige.navic.domain.models.DomainRadio
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongCollection
import paige.navic.domain.models.toSavedQueueKind
import paige.navic.domain.repositories.PlayerStateRepository
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
import kotlin.random.Random
import kotlin.time.Clock
import kotlin.time.Duration.Companion.seconds
import kotlin.time.ExperimentalTime

/**
 * Sends queue-building commands to the hub session when another device is the active receiver.
 *
 * Implemented by HubManager, which already depends on [MediaPlayerViewModel] — the player holds
 * this as a plain nullable hook rather than injecting the hub back, which would be a dependency
 * cycle.
 */
interface RemotePlaybackRouter {
	val isRemoteSessionActive: Boolean

	/** True whenever the hub socket is up (independent of who's active). When connected, the hub
	 *  owns the saved-queue history, so local capture stands down and mirrors the hub instead. */
	val isHubConnected: Boolean

	/**
	 * Replace the session queue and play [startIndex] on the active device.
	 *
	 * [sourceKind]/[sourceName] describe what is being played (album/playlist/radio + its display
	 * name) and are passed explicitly rather than read back off player state: the state's
	 * `currentCollection` only resolves asynchronously from the *playing song's* album, so at
	 * publish time it is almost always null or the PREVIOUS queue's — which is what left shared
	 * history rows unnamed.
	 */
	fun setQueue(
		songs: List<DomainSong>,
		startIndex: Int,
		sourceKind: String = "manual",
		sourceName: String? = null
	)

	/** Append to the session queue, either next or at the end. */
	fun enqueue(songs: List<DomainSong>, playNext: Boolean)

	/** Route a scrubber seek to the active remote device (absolute position). */
	fun seek(positionMs: Long)

	/**
	 * Undo restore: replace the session queue AND resume at [index]/[positionMs]. Unlike
	 * [setQueue] (which always restarts at position 0), this carries the saved playhead so an
	 * undone clear/remove/move lands the user exactly where they were.
	 */
	fun restoreQueue(
		songs: List<DomainSong>,
		index: Int,
		positionMs: Long,
		play: Boolean,
		savedQueueId: String? = null,
		sourceKind: String = "manual",
		sourceName: String? = null
	)

	/**
	 * Publish the local queue to the hub again even though its contents are unchanged. The publish
	 * path dedupes on the queue signature, so after the live record is deleted out from under us
	 * nothing would re-mint one — see [MediaPlayerViewModel.restartQueueSession].
	 */
	fun republishQueue()

	/** Tell the hub the queue was emptied here, so the shared session doesn't keep serving it. */
	fun clearSessionQueue()

	/**
	 * Swap hub-derived placeholder songs for the library's own rows, 1:1 and in order. Lives here
	 * because the hub client is what already owns that resolution (it does the same for every queue
	 * arriving over the wire).
	 */
	suspend fun resolveLibrarySongs(songs: List<DomainSong>): List<DomainSong>
}

/**
 * A short-lived snapshot of the active session's queue, captured just before a destructive queue
 * edit (clear / remove / move / play-now replace) so it can be undone. Session-scoped and in-memory
 * only — never persisted. [label] is a string-resource-agnostic key the UI maps to a message.
 */
data class QueueUndoSnapshot(
	val id: Long,
	val kind: QueueUndoKind,
	val queue: List<DomainSong>,
	val index: Int,
	val positionMs: Long,
	val wasPaused: Boolean,
	val savedQueueId: String?,
	val savedQueueKind: String
)

enum class QueueUndoKind { CLEAR, REMOVE, MOVE, REPLACE }

abstract class MediaPlayerViewModel(
	private val stateRepository: PlayerStateRepository,
	protected val connectivityManager: ConnectivityManager,
	protected val downloadManager: DownloadManager,
	private val savedQueueRepository: SavedQueueRepository
) : ViewModel() {

	@Suppress("PropertyName")
	protected val _uiState = MutableStateFlow(PlayerUiState())

	/**
	 * The raw LOCAL player state. The hub client reports from / restores into
	 * this — it must never see the remote-session override below (that would feed
	 * a remote device's own state back to it).
	 */
	val localUiState: StateFlow<PlayerUiState> = _uiState.asStateFlow()

	// navi-connect: while another device is the active receiver, HubManager
	// pushes a resolved snapshot of the REMOTE session here so the whole player
	// UI (mini player, now-playing, queue) mirrors what's actually playing
	// without any per-component branching. Null when playback is local.
	private val _remoteState = MutableStateFlow<PlayerUiState?>(null)

	/** Set by HubManager; null restores the local view. */
	fun setRemoteState(state: PlayerUiState?) {
		_remoteState.value = state
	}

	// ------------------------------------------------------------------
	// Queue undo. A single, short-lived (~6 s) snapshot of the active session's
	// queue captured just before a destructive edit, restorable to whichever
	// session (local or remote) is active. In-memory, never persisted.
	// ------------------------------------------------------------------
	private val undoStack = ArrayDeque<QueueUndoSnapshot>()
	private val _queueUndo = MutableStateFlow<QueueUndoSnapshot?>(null)

	/** The undo the queue screen currently offers, or null. Expires itself after [UNDO_TIMEOUT_MS]. */
	val queueUndo: StateFlow<QueueUndoSnapshot?> = _queueUndo.asStateFlow()

	private var undoIdCounter = 0L
	private var undoExpiryJob: Job? = null

	/**
	 * Snapshot the ACTIVE session's queue (local or remote — both surface through [uiState]) before
	 * a destructive edit. No-op on an empty queue: there is nothing to restore to.
	 */
	fun captureQueueUndo(kind: QueueUndoKind) {
		val state = uiState.value
		if (state.queue.isEmpty()) return
		val index = state.currentIndex.coerceIn(0, state.queue.lastIndex)
		val currentSong = state.currentSong ?: state.queue.getOrNull(index)
		val positionMs = (state.progress.coerceIn(0f, 1f) *
			(currentSong?.duration?.inWholeMilliseconds ?: 0L)).toLong()
		val snap = QueueUndoSnapshot(
			id = ++undoIdCounter,
			kind = kind,
			queue = state.queue.toList(),
			index = index,
			positionMs = positionMs,
			wasPaused = state.isPaused,
			savedQueueId = state.savedQueueId,
			savedQueueKind = state.savedQueueKind
		)
		undoStack.addLast(snap)
		while (undoStack.size > MAX_UNDO) undoStack.removeFirst()
		_queueUndo.value = snap
		undoExpiryJob?.cancel()
		undoExpiryJob = viewModelScope.launch {
			delay(UNDO_TIMEOUT_MS)
			if (_queueUndo.value?.id == snap.id) _queueUndo.value = null
		}
	}

	/** Dismiss the current offer without restoring (leaves the snapshot on the stack unused). */
	fun dismissQueueUndo() {
		undoExpiryJob?.cancel()
		_queueUndo.value = null
	}

	/** Restore the most recent snapshot to the active session, resuming at its saved playhead. */
	fun performQueueUndo() {
		undoExpiryJob?.cancel()
		_queueUndo.value = null
		val snap = undoStack.removeLastOrNull() ?: return
		val router = routeRemotely
		if (router != null) {
			// Carry the snapshot's history identity so the undone queue refreshes ITS record
			// instead of the hub minting a duplicate for the same listening session.
			router.restoreQueue(
				snap.queue, snap.index, snap.positionMs, play = !snap.wasPaused,
				savedQueueId = snap.savedQueueId,
				sourceKind = snap.savedQueueKind
			)
		} else {
			// Preserve the queue's saved-queue identity + kind so its history row isn't orphaned.
			loadRemoteQueue(
				snap.queue, snap.index, snap.positionMs, play = !snap.wasPaused,
				savedQueueId = snap.savedQueueId,
				savedQueueKind = snap.savedQueueKind
			)
		}
	}

	/** What the UI observes: the remote session when active, else local. */
	val uiState: StateFlow<PlayerUiState> =
		combine(_uiState, _remoteState) { local, remote -> remote ?: local }
			.stateIn(viewModelScope, SharingStarted.Eagerly, PlayerUiState())

	/**
	 * The playhead on its own — for the handful of things that actually DRAW it.
	 *
	 * [uiState] carries `progress` in the same object as `queue`/`currentSong`, and the playhead is
	 * re-stamped every 200 ms locally (the progress job) / 250 ms remotely (the hub mirror). So
	 * every collector of [uiState] used to recompose ~5x a second for the whole of playback, even
	 * ones that only read the current song's id. Read this instead of `uiState.progress`, and
	 * [steadyState] for everything else.
	 */
	val progress: StateFlow<Float> =
		uiState
			.map { it.progress }
			.distinctUntilChanged()
			.stateIn(viewModelScope, SharingStarted.Eagerly, 0f)

	/**
	 * [uiState] with the playhead zeroed, so a progress tick alone no longer emits.
	 *
	 * Deliberately the SAME type as [uiState] with the same field names: every collector that
	 * doesn't draw the playhead swaps one for the other and needs no other change. `progress` reads
	 * 0f here — that is the point, not an oversight; anything needing the real value takes
	 * [progress].
	 *
	 * The `distinctUntilChanged` walks the queue via structural equality, so it runs on
	 * [Dispatchers.Default] rather than the main thread it would otherwise inherit from
	 * `viewModelScope`.
	 */
	val steadyState: StateFlow<PlayerUiState> =
		uiState
			.map { it.copy(progress = 0f) }
			.distinctUntilChanged()
			.flowOn(Dispatchers.Default)
			.stateIn(viewModelScope, SharingStarted.Eagerly, PlayerUiState())

	protected fun isAvailable(songId: String): Boolean {
		val isOnline = connectivityManager.isOnline.value
		val isDownloaded = downloadManager.downloadedSongs.value.containsKey(songId)
		return isOnline || isDownloaded
	}

	init {
		viewModelScope.launch {
			restoreState()
			// Before any queue can be replaced: sessionIdFor answers from this index, and an empty one
			// would mint a duplicate for the very first queue played after launch.
			savedQueueRepository.primeIndex()
			observeAndSaveState()
		}
	}

	abstract fun removeFromQueue(index: Int)
	abstract fun moveQueueItem(fromIndex: Int, toIndex: Int)
	abstract fun clearQueue()
	abstract fun playAt(index: Int)
	abstract fun playRadio(radio: DomainRadio)
	abstract fun pause()
	abstract fun resume()
	abstract fun seek(normalized: Float)
	abstract fun next()
	abstract fun previous()
	abstract fun toggleShuffle()
	abstract fun toggleRepeat()
	abstract fun setPlaybackSpeed(value: Float)

	// ------------------------------------------------------------------
	// Queue-building commands. Every one of these has to go to the ACTIVE DEVICE, which may not be
	// this one: picking a song from an album while a remote device is playing used to call straight
	// into the local player, so the remote session simply ignored it and "nothing happened".
	//
	// Routed HERE rather than at the ~30 UI call sites, so a new screen can't forget to do it. The
    // platform players implement the *Local variants and stay unaware of the hub entirely.
	// ------------------------------------------------------------------

	/** Set by HubManager. Null (or inactive) means everything below plays locally, as before. */
	var remotePlaybackRouter: RemotePlaybackRouter? = null

	protected val routeRemotely: RemotePlaybackRouter?
		get() = remotePlaybackRouter?.takeIf { it.isRemoteSessionActive }

	protected abstract fun addToQueueSingleLocal(song: DomainSong)
	protected abstract fun addToQueueLocal(collection: DomainSongCollection)
	protected abstract fun playCollectionLocal(
		collection: DomainSongCollection,
		startSong: DomainSong
	)
	protected abstract fun playNextSingleLocal(song: DomainSong)
	protected abstract fun playNextLocal(collection: DomainSongCollection)
	protected abstract fun shufflePlayLocal(collection: DomainSongCollection)

	fun addToQueueSingle(song: DomainSong) {
		routeRemotely?.enqueue(listOf(song), playNext = false) ?: addToQueueSingleLocal(song)
	}

	fun addToQueue(collection: DomainSongCollection) {
		routeRemotely?.enqueue(collection.songs, playNext = false) ?: addToQueueLocal(collection)
	}

	fun playNextSingle(song: DomainSong) {
		routeRemotely?.enqueue(listOf(song), playNext = true) ?: playNextSingleLocal(song)
	}

	fun playNext(collection: DomainSongCollection) {
		routeRemotely?.enqueue(collection.songs, playNext = true) ?: playNextLocal(collection)
	}

	fun playCollection(collection: DomainSongCollection, startSong: DomainSong) {
		// Replacing the queue is undoable: snapshot whatever is playing now before it's discarded.
		captureQueueUndo(QueueUndoKind.REPLACE)
		val router = routeRemotely
		if (router != null) {
			// Send the WHOLE collection and tell the hub where to start, so the rest of the album
			// is queued behind the tapped song exactly as it would be locally.
			val start = collection.songs.indexOfFirst { it.id == startSong.id }.coerceAtLeast(0)
			router.setQueue(
				collection.songs, start,
				sourceKind = collection.toSavedQueueKind(),
				sourceName = collection.name
			)
		} else {
			playCollectionLocal(collection, startSong)
		}
	}

	fun shufflePlay(collection: DomainSongCollection) {
		captureQueueUndo(QueueUndoKind.REPLACE)
		val router = routeRemotely
		if (router != null) {
			// Shuffle here rather than asking the hub to: the receiver gets a plain ordered queue,
			// which keeps the session's own shuffle flag meaning what it already means.
			router.setQueue(
				collection.songs.shuffled(), 0,
				sourceKind = collection.toSavedQueueKind(),
				sourceName = collection.name
			)
		} else {
			shufflePlayLocal(collection)
		}
	}

	fun togglePlay() {
		if (!_uiState.value.isPaused) {
			pause()
		} else {
			resume()
		}
	}

	abstract fun syncPlayerWithState(state: PlayerUiState)

	// ------------------------------------------------------------------
	// navi-connect hub hooks. Unlike syncPlayerWithState (restore-only, bails
	// when the player already has items), loadRemoteQueue unconditionally
	// replaces the queue and seeks — it's the transfer-with-resume primitive.
	// Open (not abstract) so platforms without hub support compile unchanged.
	// ------------------------------------------------------------------
	/**
	 * Replace the local queue and seek. Default (savedQueueId = null) marks the queue TRANSIENT — a
	 * hub-mirror / transfer-adopted queue that must not clobber the user's saved-queue history. Pass
	 * a [savedQueueId] (with a [savedQueueKind]) to make it a persistent local session instead —
	 * used for locally-started generated mixes and undo restores.
	 */
	open fun loadRemoteQueue(
		songs: List<DomainSong>,
		index: Int,
		positionMs: Long,
		play: Boolean,
		savedQueueId: String? = null,
		savedQueueKind: String = "manual",
		savedQueueName: String? = null
	) {}
	open fun setPlayerVolume(volume: Float) {}
	open fun applyRemoteRepeat(mode: Int) {}
	open fun applyRemoteShuffle(enabled: Boolean) {}

	/**
	 * Replace the queue around the currently-playing track WITHOUT restarting
	 * it (used for hub queueChanged when the current track is unchanged).
	 */
	open fun reconcileRemoteQueue(songs: List<DomainSong>, index: Int) {}

	/**
	 * navi-connect autoplay: append songs to the end of the queue without
	 * disturbing the currently-playing track. Open so platforms without it
	 * (iOS) compile unchanged.
	 */
	open fun appendToQueue(songs: List<DomainSong>) {}

	private suspend fun restoreState() {
		val savedJson = stateRepository.loadState()
		if (!savedJson.isNullOrBlank()) {
			try {
				val restoredState = Json.decodeFromJsonElement<PlayerUiState>(
					Json.parseToJsonElement(savedJson)
				)
				val stateToApply = restoredState.copy(isPaused = true, isLoading = false)

				_uiState.value = stateToApply

				syncPlayerWithState(stateToApply)

			} catch (e: Exception) {
				Logger.e("MediaPlayerViewModel", "Failed to restore state!", e)
				_uiState.value = PlayerUiState()
			}
		}
	}

	@OptIn(FlowPreview::class)
	private fun observeAndSaveState() {
		// Single-slot persistence + periodic snapshot refresh, rate-limited so a playing track's
		// progress ticks don't hammer either store.
		//
		// This MUST be sample, not debounce. The progress loop emits every 200 ms while playing, so
		// `debounce(1s)` — which only fires after a gap with no emissions — never fired at all during
		// playback: state was persisted solely when playback stopped for a second. That is why a queue
		// you listened to straight through kept the cursor it was born with (track 1) while one you
		// happened to pause in got saved. sample emits the LATEST value once per interval instead, so
		// the cursor is written while you listen.
		viewModelScope.launch {
			_uiState
				.sample(1.seconds)
				.collect { state ->
					try {
						val jsonString = Json.encodeToString(state)
						stateRepository.saveState(jsonString)
					} catch (e: Exception) {
						Logger.e("MediaPlayerViewModel", "Failed to save state!", e)
					}
					try {
						val id = state.savedQueueId
						if (id != null && state.queue.isNotEmpty() && remotePlaybackRouter?.isHubConnected != true) {
							savedQueueRepository.upsert(id, state)
						}
					} catch (e: Exception) {
						Logger.e("MediaPlayerViewModel", "Failed to save queue snapshot!", e)
					}
				}
		}

		// Queue-session handover. Two jobs, both on the un-sampled state so nothing is missed:
		//
		//  - FLUSH the outgoing record at the cursor it had when you left it. Sampling alone leaves
		//    up to a second unwritten, which at a track boundary is the difference between resuming
		//    where you were and resuming a track early; switching queues is exactly when that matters.
		//  - OPEN the incoming record immediately, so it appears in the history and the active-queue
		//    highlight matches without waiting for the next sample tick.
		//
		// The session id is minted synchronously at the replace points, so it's already correct here.
		viewModelScope.launch {
			var previous: PlayerUiState? = null
			_uiState.collect { state ->
				val outgoing = previous
				previous = state
				if (outgoing != null && outgoing.savedQueueId == state.savedQueueId) return@collect
				if (remotePlaybackRouter?.isHubConnected == true) return@collect

				val outgoingId = outgoing?.savedQueueId
				if (outgoingId != null && outgoing.queue.isNotEmpty()) {
					try {
						savedQueueRepository.upsert(outgoingId, outgoing)
					} catch (e: Exception) {
						Logger.e("MediaPlayerViewModel", "Failed to flush queue snapshot!", e)
					}
				}

				val id = state.savedQueueId ?: return@collect
				if (state.queue.isEmpty()) return@collect
				try {
					savedQueueRepository.upsert(id, state)
				} catch (e: Exception) {
					Logger.e("MediaPlayerViewModel", "Failed to open queue snapshot!", e)
				}
			}
		}
	}

	/**
	 * Restore a saved queue as the live queue. [play] = false (the default) restores it PAUSED at its
	 * saved track/position (same semantics as launch-restore); [play] = true "resumes" it — restores
	 * and starts playing from the saved playhead. Routes to the active remote device when receiving.
	 */
	fun swapToSavedQueue(id: String, play: Boolean = false) {
		viewModelScope.launch {
			try {
				val entity = savedQueueRepository.get(id)
					?: throw IllegalStateException("saved queue $id is gone")
				val stored = savedQueueRepository.decodeQueue(entity)
				if (stored.isEmpty()) throw IllegalStateException("saved queue $id decoded to nothing")
				val songs = resolvePlaceholders(stored)
				val index = entity.currentIndex.coerceIn(0, songs.lastIndex)
				val name = entity.name ?: entity.sourceName

				val router = routeRemotely
				if (router != null) {
					// Reuse THIS record's id/kind/name remotely too, so resuming a saved queue on
					// another device refreshes the same history row instead of forking a new one.
					router.restoreQueue(
						songs, index, entity.positionMs, play,
						savedQueueId = id,
						sourceKind = entity.sourceKind,
						sourceName = name
					)
					_uiState.update {
						it.copy(
							savedQueueId = id,
							savedQueueKind = entity.sourceKind,
							savedQueueName = name
						)
					}
				} else {
					// loadRemoteQueue carries the id + kind through the queue replacement, so the
					// restored session keeps its history-row identity (and generated-session grouping).
					loadRemoteQueue(
						songs, index, entity.positionMs, play,
						savedQueueId = id,
						savedQueueKind = entity.sourceKind,
						savedQueueName = name
					)
				}
				_restoreFailed.value = false
			} catch (e: Exception) {
				// Silent failure here used to be invisible: the surfaces close themselves on tap, so
				// the user just saw the screen go away and nothing play.
				Logger.e("MediaPlayerViewModel", "Failed to restore saved queue $id", e)
				_restoreFailed.value = true
			}
		}
	}

	/**
	 * Records captured from the hub hold placeholder songs — synthesized from the wire metadata, so
	 * they have no album id, no mime type and (for anything the library hasn't synced) no real
	 * duration. Swap in the library's own rows before playing, strictly 1:1: dropping a track that is
	 * still missing would shift every index after it and resume on the wrong song.
	 */
	private suspend fun resolvePlaceholders(songs: List<DomainSong>): List<DomainSong> {
		if (songs.none { it.albumId == null }) return songs
		val router = remotePlaybackRouter ?: return songs
		return try {
			router.resolveLibrarySongs(songs)
		} catch (e: Exception) {
			Logger.e("MediaPlayerViewModel", "Failed to resolve saved-queue songs", e)
			songs
		}
	}

	/** One-shot "that queue wouldn't restore" signal for the saved-queue surfaces. */
	private val _restoreFailed = MutableStateFlow(false)
	val restoreFailed: StateFlow<Boolean> = _restoreFailed.asStateFlow()

	fun clearRestoreFailed() { _restoreFailed.value = false }

	/**
	 * A fresh saved-queue session id. Called synchronously by the platform players' queue-REPLACE
	 * paths (play collection / shuffle / radio), so [PlayerUiState.savedQueueId] always identifies
	 * the current queue the instant it starts — mutations then preserve it via `.copy`.
	 *
	 * The random suffix is not decoration: the hub's union-merge is keyed purely by id, so two devices
	 * that started a queue in the same millisecond used to mint the same `q_<t>_0` and have two
	 * unrelated listening sessions silently fused into one record. The hub (`os.urandom(3).hex()`) and
	 * Feishin (`crypto.randomUUID()`) both carry entropy for the same reason.
	 */
	@OptIn(ExperimentalTime::class)
	protected fun newSessionId(): String =
		"q_${Clock.System.now().toEpochMilliseconds()}_" +
			Random.nextInt(0x1000000).toString(16).padStart(6, '0')

	/**
	 * The saved-queue session id for [songs]: the id of the record we ALREADY have for this queue when
	 * one exists, else a fresh one. Without the lookup every replay of the same album, every relaunch
	 * and every restore minted another near-identical history card until the 20-row cache was nothing
	 * but duplicates. Feishin does the same (`findMatchingSavedQueueId`), so both clients converge on
	 * one record per listening session.
	 *
	 * Synchronous by design — the queue-REPLACE paths that call it are not suspending, which is why
	 * [SavedQueueRepository] keeps its membership index in memory.
	 */
	protected fun sessionIdFor(songs: List<DomainSong>): String =
		savedQueueRepository.findMatching(songs.map { it.id }) ?: newSessionId()

	/**
	 * Public minter for callers outside the platform players (e.g. RadioManager starting a local
	 * generated mix through [loadRemoteQueue]) that need a saved-queue session id.
	 */
	fun newQueueSessionId(songs: List<DomainSong> = emptyList()): String =
		if (songs.isEmpty()) newSessionId() else sessionIdFor(songs)

	/**
	 * Start a NEW session for the queue that's playing right now, re-deriving its identity from the
	 * queue itself. Used when the record the live queue was writing into disappears (the user deleted
	 * it, or cleared the whole history): without this the session id points at nothing, the publish
	 * dedupe means nothing re-mints, and what you are listening to has no card until you play something
	 * else. Feishin's `restartQueueSession` does the same.
	 */
	fun restartQueueSession() {
		val state = _uiState.value
		if (state.queue.isEmpty()) {
			_uiState.update { it.copy(savedQueueId = null) }
			return
		}
		// Bypass the membership lookup: the record it would find is the one just deleted.
		val fresh = newSessionId()
		_uiState.update { it.copy(savedQueueId = fresh) }
		val router = remotePlaybackRouter
		if (router?.isHubConnected == true) {
			// The hub owns the history while connected, and its publish dedupes on the queue's
			// contents — which haven't changed — so it needs telling explicitly.
			router.republishQueue()
			return
		}
		viewModelScope.launch {
			try {
				savedQueueRepository.upsert(fresh, _uiState.value)
			} catch (e: Exception) {
				Logger.e("MediaPlayerViewModel", "Failed to restart the queue session", e)
			}
		}
	}

	/**
	 * Remember the saved-queue id a publish just used, so a re-publish of the same queue reuses it.
	 *
	 * The hub-publish path derives an id when the state carries none (`HubManager.hubSavedQueueIdFor`)
	 * but that id lived only inside the frame it was sent in. The claim-republish loop then re-derived
	 * it on every pass and — until the hub's broadcast round-tripped back into the local index — could
	 * mint a *different* one each time, forking a history card per republish.
	 *
	 * Only fills a hole: a queue-replace that raced with the publish owns the id it set.
	 */
	fun adoptQueueSessionId(id: String) {
		_uiState.update { if (it.savedQueueId == null) it.copy(savedQueueId = id) else it }
	}

	companion object {
		private const val MAX_UNDO = 10
		private const val UNDO_TIMEOUT_MS = 6_000L
	}
}
