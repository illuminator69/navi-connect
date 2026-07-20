package paige.navic.ui.screens.settings.viewmodels

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import paige.navic.data.database.dao.SongDao
import paige.navic.data.database.entities.DownloadEntity
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.manager.DownloadManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.DomainSong

/** One download plus the song it belongs to — the rows are useless without a title. */
data class DownloadCenterItem(
	val download: DownloadEntity,
	val song: DomainSong?
)

/**
 * The download center's sections. Active and queued are split on purpose: a queued row is accepted
 * work waiting on a concurrency permit, and conflating the two is why a 50-track album download
 * used to look like 50 simultaneous transfers.
 */
data class DownloadCenterState(
	val active: List<DownloadCenterItem> = emptyList(),
	val queued: List<DownloadCenterItem> = emptyList(),
	val failed: List<DownloadCenterItem> = emptyList(),
	val completed: List<DownloadCenterItem> = emptyList(),
	val totalSize: Long = 0L
)

/** User-tunable download constraints, mirrored from [PreferenceManager] for the UI. */
data class DownloadSettings(
	val wifiOnly: Boolean = false,
	val chargingOnly: Boolean = false,
	val maxConcurrency: Int = 4
)

class DownloadCenterViewModel(
	private val downloadManager: DownloadManager,
	private val songDao: SongDao,
	private val preferenceManager: PreferenceManager
) : ViewModel() {

	private val _state = MutableStateFlow(DownloadCenterState())
	val state = _state.asStateFlow()

	private val _settings = MutableStateFlow(
		DownloadSettings(
			wifiOnly = preferenceManager.downloadWifiOnly,
			chargingOnly = preferenceManager.downloadChargingOnly,
			maxConcurrency = preferenceManager.downloadMaxConcurrency
		)
	)
	val settings = _settings.asStateFlow()

	/** True while transfers are being held back by Wi-Fi-only / charging-only constraints. */
	val constrained = downloadManager.downloadsConstrained.stateIn(
		viewModelScope, SharingStarted.WhileSubscribed(5000), false
	)

	val totalSize = downloadManager.downloadSize.stateIn(
		viewModelScope, SharingStarted.WhileSubscribed(5000), 0L
	)

	fun setWifiOnly(value: Boolean) {
		preferenceManager.downloadWifiOnly = value
		_settings.value = _settings.value.copy(wifiOnly = value)
	}

	fun setChargingOnly(value: Boolean) {
		preferenceManager.downloadChargingOnly = value
		_settings.value = _settings.value.copy(chargingOnly = value)
	}

	/** Persisted; the semaphore permit count applies from the next app start (see DownloadManager). */
	fun setMaxConcurrency(value: Int) {
		val clamped = value.coerceIn(1, 10)
		preferenceManager.downloadMaxConcurrency = clamped
		_settings.value = _settings.value.copy(maxConcurrency = clamped)
	}

	init {
		viewModelScope.launch(Dispatchers.IO) {
			// allDownloads re-emits on every ~1% progress write. Only the ACTIVE/queued rows
			// actually change on a tick, so cache the expensive, whole-library-scale work
			// (the song-resolution DB query and the terminal-section builds) and rebuild it
			// only when the relevant SET of downloads changes — not on every progress tick.
			var cachedIdSet: Set<String> = emptySet()
			var cachedSongs: Map<String, DomainSong> = emptyMap()
			var completedIds: Set<String> = emptySet()
			var failedIds: Set<String> = emptySet()
			var cachedCompleted: List<DownloadCenterItem> = emptyList()
			var cachedFailed: List<DownloadCenterItem> = emptyList()
			var cachedTotalSize = 0L

			downloadManager.allDownloads.collect { downloads ->
				val idSet = downloads.mapTo(HashSet()) { it.songId }
				val songsChanged = idSet != cachedIdSet
				val songs = if (songsChanged) {
					cachedIdSet = idSet
					songDao.getSongsByIds(downloads.map { it.songId })
						.associate { it.songId to it.toDomainModel() }
						.also { cachedSongs = it }
				} else {
					cachedSongs
				}

				// In-flight and queued rows are ordered by when they were REQUESTED, and that order
				// never changes while they run. Sorting them by `updatedAt` (as this first did)
				// re-sorted the list on every progress tick, so downloads visibly swapped places
				// with each other depending on which one last ticked.
				fun byRequest(status: DownloadStatus) = downloads
					.filter { it.status == status }
					.sortedBy { it.createdAt }
					.map { DownloadCenterItem(it, songs[it.songId]) }

				// Finished and failed rows are terminal — their `updatedAt` is the moment they
				// settled, so most-recent-first is both meaningful and stable. Rebuild them only
				// when their membership changes (never on an active download's progress tick).
				fun byCompletion(status: DownloadStatus) = downloads
					.filter { it.status == status }
					.sortedByDescending { it.updatedAt }
					.map { DownloadCenterItem(it, songs[it.songId]) }

				val newCompletedIds = downloads.filterTo(HashSet()) {
					it.status == DownloadStatus.DOWNLOADED
				}.mapTo(HashSet()) { it.songId }
				if (newCompletedIds != completedIds || songsChanged) {
					completedIds = newCompletedIds
					cachedCompleted = byCompletion(DownloadStatus.DOWNLOADED)
					cachedTotalSize = downloads
						.filter { it.status == DownloadStatus.DOWNLOADED }
						.sumOf { it.fileSize }
				}

				val newFailedIds = downloads.filterTo(HashSet()) {
					it.status == DownloadStatus.FAILED
				}.mapTo(HashSet()) { it.songId }
				if (newFailedIds != failedIds || songsChanged) {
					failedIds = newFailedIds
					cachedFailed = byCompletion(DownloadStatus.FAILED)
				}

				_state.value = DownloadCenterState(
					active = byRequest(DownloadStatus.DOWNLOADING),
					queued = byRequest(DownloadStatus.QUEUED),
					failed = cachedFailed,
					completed = cachedCompleted,
					totalSize = cachedTotalSize
				)
			}
		}
	}

	fun retry(item: DownloadCenterItem) {
		val song = item.song ?: return
		downloadManager.retryDownload(song)
	}

	fun retryAllFailed() {
		_state.value.failed.forEach { retry(it) }
	}

	fun cancel(item: DownloadCenterItem) {
		downloadManager.cancelDownload(item.download.songId)
	}

	fun delete(item: DownloadCenterItem) {
		downloadManager.deleteDownload(item.download.songId)
	}

	fun clearFailed() {
		downloadManager.clearFailedDownloads()
	}

	/**
	 * Re-check every completed download's file. Any whose file has vanished is re-downloaded with
	 * its original settings when the song is still known, or dropped otherwise so the library stops
	 * advertising it as available offline.
	 */
	fun repairMissing() {
		viewModelScope.launch(Dispatchers.IO) {
			_state.value.completed.forEach { item ->
				if (!downloadManager.isDownloadFilePresent(item.download)) {
					val song = item.song
					if (song != null) downloadManager.retryDownload(song)
					else downloadManager.deleteDownload(item.download.songId)
				}
			}
		}
	}

	fun cancelAll() {
		downloadManager.cancelAllActiveDownloads()
	}
}
