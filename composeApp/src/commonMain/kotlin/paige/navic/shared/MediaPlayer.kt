package paige.navic.shared

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import paige.navic.domain.models.DomainRadio
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongCollection
import paige.navic.domain.repositories.PlayerStateRepository
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
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

	/** Replace the session queue and play [startIndex] on the active device. */
	fun setQueue(songs: List<DomainSong>, startIndex: Int)

	/** Append to the session queue, either next or at the end. */
	fun enqueue(songs: List<DomainSong>, playNext: Boolean)

	/**
	 * Undo restore: replace the session queue AND resume at [index]/[positionMs]. Unlike
	 * [setQueue] (which always restarts at position 0), this carries the saved playhead so an
	 * undone clear/remove/move lands the user exactly where they were.
	 */
	fun restoreQueue(songs: List<DomainSong>, index: Int, positionMs: Long, play: Boolean)
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
			router.restoreQueue(snap.queue, snap.index, snap.positionMs, play = !snap.wasPaused)
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

	protected fun isAvailable(songId: String): Boolean {
		val isOnline = connectivityManager.isOnline.value
		val isDownloaded = downloadManager.downloadedSongs.value.containsKey(songId)
		return isOnline || isDownloaded
	}

	init {
		viewModelScope.launch {
			restoreState()
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

	private val routeRemotely: RemotePlaybackRouter?
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
			router.setQueue(collection.songs, start)
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
			router.setQueue(collection.songs.shuffled(), 0)
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
		savedQueueKind: String = "manual"
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
		// Single-slot persistence + periodic snapshot refresh. Debounced so a playing track's
		// per-second progress ticks don't hammer either store.
		viewModelScope.launch {
			_uiState
				.debounce(1.seconds)
				.collect { state ->
					try {
						val jsonString = Json.encodeToString(state)
						stateRepository.saveState(jsonString)
					} catch (e: Exception) {
						Logger.e("MediaPlayerViewModel", "Failed to save state!", e)
					}
					try {
						val id = state.savedQueueId
						if (id != null && state.queue.isNotEmpty()) {
							savedQueueRepository.upsert(id, state)
						}
					} catch (e: Exception) {
						Logger.e("MediaPlayerViewModel", "Failed to save queue snapshot!", e)
					}
				}
		}

		// Create/refresh the row the MOMENT a queue session begins, so it shows up immediately and
		// the active-queue highlight matches without waiting for the debounce above. The session id
		// is minted synchronously at the replace points, so it's already correct here.
		viewModelScope.launch {
			_uiState
				.map { it.savedQueueId }
				.distinctUntilChanged()
				.collect { id ->
					if (id == null) return@collect
					val state = _uiState.value
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
			val entity = savedQueueRepository.get(id) ?: return@launch
			val songs = savedQueueRepository.decodeQueue(entity)
			if (songs.isEmpty()) return@launch
			val index = entity.currentIndex.coerceIn(0, songs.lastIndex)

			val router = routeRemotely
			if (router != null) {
				router.restoreQueue(songs, index, entity.positionMs, play)
				_uiState.update { it.copy(savedQueueId = id, savedQueueKind = entity.sourceKind) }
			} else {
				// loadRemoteQueue carries the id + kind through the queue replacement, so the
				// restored session keeps its history-row identity (and generated-session grouping).
				loadRemoteQueue(
					songs, index, entity.positionMs, play,
					savedQueueId = id,
					savedQueueKind = entity.sourceKind
				)
			}
		}
	}

	/**
	 * A fresh saved-queue session id. Called synchronously by the platform players' queue-REPLACE
	 * paths (play collection / shuffle / radio), so [PlayerUiState.savedQueueId] always identifies
	 * the current queue the instant it starts — mutations then preserve it via `.copy`.
	 */
	@OptIn(ExperimentalTime::class)
	protected fun newSessionId(): String =
		"q_${Clock.System.now().toEpochMilliseconds()}_${sessionIdCounter++}"

	/**
	 * Public minter for callers outside the platform players (e.g. RadioManager starting a local
	 * generated mix through [loadRemoteQueue]) that need a saved-queue session id.
	 */
	fun newQueueSessionId(): String = newSessionId()

	private var sessionIdCounter: Int = 0

	companion object {
		private const val MAX_UNDO = 10
		private const val UNDO_TIMEOUT_MS = 6_000L
	}
}
