package paige.navic.domain.manager

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.IO
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.info_status_idle
import org.jetbrains.compose.resources.StringResource
import paige.navic.data.database.dao.AlbumDao
import paige.navic.data.database.dao.SyncActionDao
import paige.navic.data.database.entities.SyncActionEntity
import paige.navic.data.database.entities.SyncActionType
import paige.navic.domain.repositories.DbRepository
import paige.navic.util.core.Logger
import kotlin.time.Clock
import kotlin.time.Duration.Companion.hours
import kotlin.time.Duration.Companion.minutes
import kotlin.time.Duration.Companion.seconds
import kotlin.time.Instant

data class SyncState(
	val isSyncing: Boolean = false,
	val progress: Float = 0f,
	val message: StringResource = Res.string.info_status_idle,
	/**
	 * True when the last full-library sync attempt failed at the top level (e.g. server unreachable),
	 * so the UI can say "showing cached library, sync failed" instead of implying the library is
	 * empty/broken. Cleared the moment a sync succeeds or a new one starts.
	 */
	val lastSyncFailed: Boolean = false
)

class SyncManager(
	private val repository: DbRepository,
	private val syncDao: SyncActionDao,
	private val albumDao: AlbumDao,
	private val connectivityManager: ConnectivityManager,
	private val sessionManager: SessionManager,
	private val preferenceManager: PreferenceManager,
	private val lbBotManager: LbBotManager
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
	private var syncJob: Job? = null
	private val syncMutex = Mutex()

	private val fullSyncThreshold = 1.hours

	private companion object {
		/** How long after the last completed fill to wait before pulling. */
		val LB_BOT_SYNC_DEBOUNCE = 90.seconds

		/** Hard floor between lb-bot-triggered pulls, whatever the debounce says. */
		val LB_BOT_SYNC_MIN_INTERVAL = 5.minutes.inWholeMilliseconds
	}

	private val _syncState = MutableStateFlow(SyncState())
	val syncState = _syncState.asStateFlow()

	/**
	 * Pull the library in after lb-bot places an album, without doing it five times
	 * for five albums.
	 *
	 * A completed fill leaves Navidrome holding files that Room knows nothing about,
	 * so the album isn't playable until a sync — and the only sync Navic has is the
	 * full one. Syncing per fill would be brutal on battery for anyone filling a
	 * handful of albums in a sitting, so each signal *restarts* the timer instead of
	 * starting a sync: a burst collapses into one pull at the end of it.
	 *
	 * The delay does double duty. Navidrome has to finish its own scan before a pull
	 * could see the new files at all, so syncing the instant lb-bot says "placed"
	 * would mostly fetch the library as it was a moment ago.
	 */
	private var lbBotSyncJob: Job? = null
	private var lastLbBotSyncMs = 0L

	private fun observeLbBotFills() {
		scope.launch {
			lbBotManager.libraryRevision.collect { revision ->
				if (revision == 0L) return@collect
				lbBotSyncJob?.cancel()
				lbBotSyncJob = scope.launch {
					delay(LB_BOT_SYNC_DEBOUNCE)
					val now = Clock.System.now().toEpochMilliseconds()
					// A floor as well as a debounce: back-to-back fills that each land
					// just outside the debounce window must still not each buy a full
					// library pull.
					if (now - lastLbBotSyncMs < LB_BOT_SYNC_MIN_INTERVAL) {
						Logger.i("SyncManager", "lb-bot sync skipped: one ran recently")
						return@launch
					}
					if (_syncState.value.isSyncing || syncMutex.isLocked) {
						Logger.i("SyncManager", "lb-bot sync skipped: already syncing")
						return@launch
					}
					lastLbBotSyncMs = now
					Logger.i("SyncManager", "Syncing after lb-bot placed an album")
					preferenceManager.lastFullSyncTime = 0
					runSyncCycle()
				}
			}
		}
	}

	init {
		observeLbBotFills()
		scope.launch {
			connectivityManager.isOnline.collect { isOnline ->
				if (!syncMutex.isLocked && isOnline) {
					syncMutex.withLock { processQueue() }
				}
			}
		}
	}

	fun startPeriodicSync() {
		Logger.i("SyncManager", "Starting periodic sync cicle.")
		if (syncJob?.isActive == true) return

		scope.launch {
			if (albumDao.getAlbumCount() == 0
				|| preferenceManager.lastFullSyncTime <= 0L) {
				Logger.i("SyncManager", "Syncing now because we haven't synced before")
				runSyncCycle()
			}
		}

		syncJob = scope.launch {
			while (isActive) {
				runSyncCycle()
				delay(15.minutes)
			}
		}
	}

	fun triggerManualSync() {
		scope.launch {
			preferenceManager.lastFullSyncTime = 0
			runSyncCycle()
		}
	}

	fun stopPeriodicSync() {
		syncJob?.cancel()
		_syncState.value = SyncState(isSyncing = false)
	}

	fun enqueueAction(actionType: SyncActionType, itemId: String) {
		scope.launch {
			syncDao.enqueue(SyncActionEntity(actionType = actionType, itemId = itemId))
			if (!syncMutex.isLocked) {
				syncMutex.withLock { processQueue() }
			}
		}
	}

	private suspend fun runSyncCycle() {
		syncMutex.withLock {
			processQueue()

			val currentTime = Clock.System.now()
			if (currentTime - Instant.fromEpochMilliseconds(preferenceManager.lastFullSyncTime) > fullSyncThreshold) {
				Logger.i("SyncManager", "Starting full library pull...")

				_syncState.update {
					// A new attempt clears any prior failure flag while it runs.
					it.copy(isSyncing = true, lastSyncFailed = false)
				}

				val result = repository.syncEverything { progress, message ->
					_syncState.update {
						it.copy(isSyncing = true, progress = progress, message = message)
					}
				}

				if (result.isSuccess) {
					preferenceManager.lastFullSyncTime = currentTime.toEpochMilliseconds()
					Logger.i("SyncManager", "Full library sync complete.")
				} else {
					Logger.w("SyncManager", "Full library sync failed; keeping cached library.")
				}

				_syncState.update {
					it.copy(
						isSyncing = false,
						message = Res.string.info_status_idle,
						lastSyncFailed = result.isFailure
					)
				}
			}
		}
	}

	private suspend fun processQueue() {
		val actions = syncDao.getPendingActions()
		if (actions.isEmpty()) return

		for (action in actions) {
			try {
				when (action.actionType) {
					SyncActionType.STAR -> sessionManager.api.star(action.itemId)
					SyncActionType.UNSTAR -> sessionManager.api.unstar(action.itemId)
					SyncActionType.DELETE_PLAYLIST -> sessionManager.api.deletePlaylist(action.itemId)
					SyncActionType.SCROBBLE -> sessionManager.api.scrobble(action.itemId, submission = true)
					SyncActionType.STAR_0 -> sessionManager.api.setRating(action.itemId, 0)
					SyncActionType.STAR_1 -> sessionManager.api.setRating(action.itemId, 1)
					SyncActionType.STAR_2 -> sessionManager.api.setRating(action.itemId, 2)
					SyncActionType.STAR_3 -> sessionManager.api.setRating(action.itemId, 3)
					SyncActionType.STAR_4 -> sessionManager.api.setRating(action.itemId, 4)
					SyncActionType.STAR_5 -> sessionManager.api.setRating(action.itemId, 5)
				}

				syncDao.removeAction(action.id)
				Logger.i(
					"SyncManager",
					"Successfully synced ${action.actionType} for ${action.itemId}"
				)

			} catch (e: Exception) {
				Logger.e("SyncManager", "Network failed. Action left in queue.", e)
				break
			}
		}
	}
}
