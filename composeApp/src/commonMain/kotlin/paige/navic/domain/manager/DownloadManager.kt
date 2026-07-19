package paige.navic.domain.manager

import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import coil3.size.Size
import io.ktor.client.HttpClient
import io.ktor.client.plugins.defaultRequest
import io.ktor.client.plugins.onDownload
import io.ktor.client.request.header
import io.ktor.client.request.prepareRequest
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.HttpMethod
import kotlinx.collections.immutable.toImmutableList
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.IO
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.sync.withPermit
import paige.navic.data.database.dao.AlbumDao
import paige.navic.data.database.dao.DownloadDao
import paige.navic.data.database.dao.LyricDao
import paige.navic.data.database.entities.DownloadEntity
import paige.navic.data.database.entities.DownloadSource
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.data.database.entities.LyricEntity
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongCollection
import paige.navic.domain.repositories.LyricsRepository
import paige.navic.util.core.Logger
import paige.navic.util.core.PlatformType
import paige.navic.util.core.currentPlatformType
import kotlin.time.Clock
import coil3.PlatformContext as CoilPlatformContext

class DownloadManager(
	private val coilPlatformContext: CoilPlatformContext,
	private val downloadDao: DownloadDao,
	private val albumDao: AlbumDao,
	private val storageManager: StorageManager,
	private val lyricsRepository: LyricsRepository,
	private val lyricDao: LyricDao,
	private val sessionManager: SessionManager,
	private val preferenceManager: PreferenceManager,
	private val connectivityManager: ConnectivityManager
) {
	private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
	private val client = HttpClient {
		val customHeaders = preferenceManager.customHeadersMap()
		if (customHeaders.isNotEmpty()) {
			defaultRequest {
				customHeaders.forEach { (key, value) -> header(key, value) }
			}
		}
	}
	private val activeDownloadsMutex = Mutex()
	private val activeDownloads = mutableMapOf<String, Job>()
	// Max simultaneous transfers = the user's max-concurrency setting (capped at [MAX_CONCURRENCY]).
	// Read once at construction: Semaphore permits are fixed, so a changed setting takes effect on
	// the next app start — acceptable for a rarely-touched knob, and keeps the gate lock-free.
	private val downloadSemaphore =
		Semaphore(preferenceManager.downloadMaxConcurrency.coerceIn(1, MAX_CONCURRENCY))

	/**
	 * True when transfers are being held back by a user constraint (Wi-Fi-only on a metered link, or
	 * charging-only off charger). Queued rows stay QUEUED and resume automatically when the
	 * constraint clears; the download center surfaces this so a "stuck" queue reads as intentional.
	 */
	val downloadsConstrained: StateFlow<Boolean> =
		combine(connectivityManager.isCellular, connectivityManager.isCharging) { cellular, charging ->
			!constraintsSatisfied(cellular, charging)
		}.stateIn(scope, kotlinx.coroutines.flow.SharingStarted.WhileSubscribed(5000), false)

	private fun constraintsSatisfied(cellular: Boolean, charging: Boolean): Boolean =
		(!preferenceManager.downloadWifiOnly || !cellular) &&
			(!preferenceManager.downloadChargingOnly || charging)

	/**
	 * Suspend until the user's download constraints are satisfied. Returns immediately when neither
	 * Wi-Fi-only nor charging-only is on. Cancellation (a cancelled download) propagates normally.
	 */
	private suspend fun awaitDownloadConstraints() {
		if (!preferenceManager.downloadWifiOnly && !preferenceManager.downloadChargingOnly) return
		combine(connectivityManager.isCellular, connectivityManager.isCharging) { cellular, charging ->
			constraintsSatisfied(cellular, charging)
		}.first { it }
	}

	val allDownloads = downloadDao.getAllDownloads().map { it.toImmutableList() }
	val downloadCount = downloadDao.getDownloadsCount()
	/**
	 * Read from the stored `fileSize` column rather than stat-ing every file on every emission —
	 * the old version hit the filesystem once per downloaded track each time ANY row changed, so
	 * a library download made it re-stat the whole library on every progress tick.
	 */
	val downloadSize = downloadDao.getTotalSize()

	private val _downloadedSongs = MutableStateFlow<Map<String, String>>(emptyMap())
	val downloadedSongs: StateFlow<Map<String, String>> = _downloadedSongs.asStateFlow()

	private var libraryDownloadJob: Job? = null
	private val _isDownloadingLibrary = MutableStateFlow(false)
	val isDownloadingLibrary: StateFlow<Boolean> = _isDownloadingLibrary.asStateFlow()
	private val _libraryDownloadProgress = MutableStateFlow(0f)
	val libraryDownloadProgress: StateFlow<Float> = _libraryDownloadProgress.asStateFlow()

	init {
		scope.launch {
			allDownloads.collectLatest { downloads ->
				_downloadedSongs.value = downloads
					.filter { it.status == DownloadStatus.DOWNLOADED && it.filePath != null }
					.associate { it.songId to it.filePath!! }
			}
		}
		scope.launch {
			// Downloads that predate the size column (v3) report 0 bytes, which would make the
			// storage total read as empty for anyone upgrading. Measure them ONCE, in the
			// background, instead of blocking the migration on a stat of every file.
			backfillMissingFileSizes()
			// Nothing survives a process death mid-transfer: any row still claiming to be active
			// has no coroutine behind it, so park it as failed and let the user retry.
			reconcileInterruptedDownloads()
		}
	}

	private suspend fun backfillMissingFileSizes() {
		try {
			downloadDao.getDownloadsMissingSize().forEach { download ->
				val path = download.filePath ?: return@forEach
				val size = storageManager.getFileSize(path)
				if (size > 0L) {
					downloadDao.updateFileSize(download.songId, size)
				} else {
					// The row says downloaded but the file is gone (cleared cache, manual delete).
					// Drop the row so the UI stops lying about it being available offline.
					downloadDao.deleteDownload(download.songId)
				}
			}
		} catch (e: Exception) {
			if (e is CancellationException) throw e
			Logger.e("DownloadManager", "failed to backfill download sizes", e)
		}
	}

	private suspend fun reconcileInterruptedDownloads() {
		try {
			val stranded = downloadDao.getDownloadsByStatus(DownloadStatus.DOWNLOADING) +
				downloadDao.getDownloadsByStatus(DownloadStatus.QUEUED)
			stranded.forEach { download ->
				downloadDao.insertDownload(
					download.copy(
						status = DownloadStatus.FAILED,
						progress = 0f,
						error = "Interrupted",
						updatedAt = now()
					)
				)
			}
			if (stranded.isNotEmpty()) {
				Logger.i("DownloadManager", "reset ${stranded.size} interrupted downloads to failed")
			}
		} catch (e: Exception) {
			if (e is CancellationException) throw e
			Logger.e("DownloadManager", "failed to reconcile interrupted downloads", e)
		}
	}

	fun getDownloadedFilePath(songId: String): String? {
		return _downloadedSongs.value[songId]
	}

	/** songId -> on-disk byte size for every completed download, for storage-budget accounting. */
	suspend fun downloadedFileSizes(): Map<String, Long> =
		downloadDao.getDownloadsByStatus(DownloadStatus.DOWNLOADED)
			.associate { it.songId to it.fileSize }

	/** The quality a download is fetched at: a transcode ceiling in kbps (0 = original) + container. */
	data class DownloadQuality(val bitrate: Int, val format: String?)

	/**
	 * The user's chosen download quality. Deliberately its own setting rather than the streaming
	 * one: a stream is discarded, a download is kept, so it shouldn't inherit the bitrate you
	 * picked to save cellular data.
	 */
	fun preferredQuality(): DownloadQuality {
		val quality = preferenceManager.downloadQuality
		val isAndroid = currentPlatformType == PlatformType.Android
		// The custom ceiling only applies when the quality isn't already "original" — an explicit
		// Lossless choice must not be silently capped.
		val defaultBitrate = if (isAndroid) quality.bitrateAndroid else quality.bitrateIos
		val custom = preferenceManager.customMaxBitrateDownload
		return DownloadQuality(
			bitrate = if (custom > 0 && defaultBitrate > 0) custom else defaultBitrate,
			format = if (isAndroid) quality.containerAndroid else quality.containerIos
		)
	}

	/**
	 * [quality] null means "whatever the user picked in settings". Callers that KNOW what they want
	 * — a playlist policy, or a retry reusing the settings a file was first requested with — pass it
	 * explicitly, so their choice is never overwritten by a later preference change.
	 */
	fun downloadSong(
		song: DomainSong,
		quality: DownloadQuality? = null,
		source: String = DownloadSource.MANUAL
	): Job {
		val job = scope.launch(Dispatchers.IO) {
			val alreadyActive = activeDownloadsMutex.withLock { activeDownloads.containsKey(song.id) }
			if (alreadyActive) return@launch

			val resolved = quality ?: preferredQuality()
			val bitrate = resolved.bitrate
			val format = resolved.format

			try {
				activeDownloadsMutex.withLock { activeDownloads[song.id] = coroutineContext[Job]!! }

				// Marked QUEUED *before* asking for a permit, so a 50-track album download shows 50
				// queued rows and ~10 genuinely transferring — rather than 50 identical spinners.
				val existing = downloadDao.getDownloadById(song.id)
				downloadDao.insertDownload(
					DownloadEntity(
						songId = song.id,
						status = DownloadStatus.QUEUED,
						progress = 0f,
						maxBitRate = bitrate,
						format = format,
						// Keep the original request time across retries; the attempt count too.
						retryCount = existing?.retryCount ?: 0,
						sourcePolicy = source,
						createdAt = existing?.createdAt?.takeIf { it > 0L } ?: now(),
						updatedAt = now()
					)
				)

				// Hold the row as QUEUED until Wi-Fi-only / charging-only constraints allow it. Done
				// before taking a permit so a blocked download doesn't occupy a concurrency slot.
				awaitDownloadConstraints()

				downloadSemaphore.withPermit {
					executeDownloadProcess(song, bitrate, format, source)
				}
			} catch (e: CancellationException) {
				// A cancelled download must not linger as QUEUED/DOWNLOADING forever.
				markCancelled(song.id)
				throw e
			} finally {
				activeDownloadsMutex.withLock { activeDownloads.remove(song.id) }
			}
		}
		return job
	}

	/** Re-run a failed download with the quality/format it was originally asked for. */
	fun retryDownload(song: DomainSong) {
		scope.launch(Dispatchers.IO) {
			val existing = downloadDao.getDownloadById(song.id)
			downloadDao.insertDownload(
				(existing ?: DownloadEntity(song.id, DownloadStatus.QUEUED)).copy(
					status = DownloadStatus.QUEUED,
					progress = 0f,
					error = null,
					retryCount = (existing?.retryCount ?: 0) + 1,
					updatedAt = now()
				)
			)
			downloadSong(
				song,
				// Retry with the settings it was ORIGINALLY requested at, not today's preference.
				quality = existing?.let { DownloadQuality(it.maxBitRate, it.format) },
				source = existing?.sourcePolicy ?: DownloadSource.MANUAL
			)
		}
	}

	/** Drop every failed row without touching what's queued or already downloaded. */
	fun clearFailedDownloads() {
		scope.launch(Dispatchers.IO) {
			downloadDao.clearDownloadsByStatus(DownloadStatus.FAILED)
		}
	}

	private suspend fun markCancelled(songId: String) {
		val existing = downloadDao.getDownloadById(songId) ?: return
		if (existing.status == DownloadStatus.QUEUED || existing.status == DownloadStatus.DOWNLOADING) {
			downloadDao.deleteDownload(songId)
		}
	}

	suspend fun downloadCollection(collection: DomainSongCollection) {
		downloadSongs(collection.songs, DownloadSource.ALBUM)
	}

	/** Download an arbitrary list of songs (e.g. the current queue), skipping ones already held. */
	suspend fun downloadSongs(songs: List<DomainSong>, source: String = DownloadSource.MANUAL) {
		songs
			.filter { !isDownloaded(it.id) }
			.forEach { downloadSong(it, source = source) }
	}

	/**
	 * Download the next [count] songs of a queue starting AT [fromIndex] (inclusive) — the "download
	 * upcoming" entry point. Tagged [DownloadSource.QUEUE] like a whole-queue download; already-held
	 * songs are skipped by [downloadSongs].
	 */
	suspend fun downloadNextSongs(queue: List<DomainSong>, fromIndex: Int, count: Int) {
		if (queue.isEmpty() || count <= 0) return
		val start = fromIndex.coerceIn(0, queue.lastIndex)
		val slice = queue.subList(start, minOf(start + count, queue.size))
		downloadSongs(slice, source = DownloadSource.QUEUE)
	}

	fun cancelDownloads(songIds: List<String>) = songIds.forEach { cancelDownload(it) }

	fun deleteDownloads(songIds: List<String>) = songIds.forEach { deleteDownload(it) }

	/**
	 * Whether a completed download's file is still on disk. A row can outlive its file (cache
	 * cleared, manual delete, OS eviction), at which point the app wrongly believes the song is
	 * available offline — the download center's "repair" action uses this to find those.
	 */
	suspend fun isDownloadFilePresent(download: DownloadEntity): Boolean {
		val path = download.filePath ?: return false
		return storageManager.getFileSize(path) > 0L
	}

	fun downloadEntireLibrary(songs: List<DomainSong>) {
		if (_isDownloadingLibrary.value) return

		libraryDownloadJob = scope.launch(Dispatchers.IO) {
			try {
				_isDownloadingLibrary.value = true
				_libraryDownloadProgress.value = 0f

				val songsToDownload = songs.filter { !isDownloaded(it.id) }
				val totalToDownload = songsToDownload.size

				if (totalToDownload == 0) {
					_isDownloadingLibrary.value = false
					_libraryDownloadProgress.value = 1f
					return@launch
				}

				val downloadQueue = Channel<DomainSong>(Channel.UNLIMITED)
				songsToDownload.forEach { downloadQueue.trySend(it) }
				downloadQueue.close()

				var processedCount = 0
				val progressMutex = Mutex()

				val workers = List(10) {
					launch {
						for (song in downloadQueue) {
							downloadSong(song, source = DownloadSource.LIBRARY).join()

							progressMutex.withLock {
								processedCount++
								_libraryDownloadProgress.value = processedCount.toFloat() / totalToDownload.toFloat()
							}
						}
					}
				}

				workers.joinAll()
				_isDownloadingLibrary.value = false

			} catch (_: CancellationException) {
				_isDownloadingLibrary.value = false
				_libraryDownloadProgress.value = 0f
			}
		}
	}

	fun cancelAllActiveDownloads() {
		libraryDownloadJob?.cancel()
		libraryDownloadJob = null
		_isDownloadingLibrary.value = false
		_libraryDownloadProgress.value = 0f

		scope.launch(Dispatchers.IO) {
			val jobsToCancel = activeDownloadsMutex.withLock {
				val copy = activeDownloads.toMap()
				activeDownloads.clear()
				copy
			}

			jobsToCancel.forEach { (songId, job) ->
				job.cancel()
				val existing = downloadDao.getDownloadById(songId)
				if (existing?.status == DownloadStatus.DOWNLOADING
					|| existing?.status == DownloadStatus.QUEUED
				) {
					downloadDao.deleteDownload(songId)
				}
			}
		}
	}

	fun cancelDownload(songId: String) {
		scope.launch(Dispatchers.IO) {
			activeDownloadsMutex.withLock {
				activeDownloads[songId]?.cancel()
				activeDownloads.remove(songId)
			}

			val existing = downloadDao.getDownloadById(songId)
			if (existing?.status == DownloadStatus.DOWNLOADING
				|| existing?.status == DownloadStatus.QUEUED
				|| existing?.status == DownloadStatus.FAILED
			) {
				downloadDao.deleteDownload(songId)
			}
		}
	}

	fun cancelCollectionDownload(collection: DomainSongCollection) {
		collection.songs.forEach { song ->
			cancelDownload(song.id)
		}
	}

	fun deleteDownload(songId: String) {
		cancelDownload(songId)
		scope.launch {
			val download = downloadDao.getDownloadById(songId)
			download?.filePath?.let { storageManager.deleteFile(it) }
			downloadDao.deleteDownload(songId)
		}
	}

	fun deleteDownloadedCollection(collection: DomainSongCollection) {
		collection.songs.forEach { song ->
			deleteDownload(song.id)
		}
	}

	suspend fun isDownloaded(songId: String): Boolean {
		return downloadDao.getDownloadById(songId)?.status == DownloadStatus.DOWNLOADED
	}

	fun getCollectionDownloadStatus(songIds: List<String>): Flow<DownloadStatus> {
		return allDownloads.map { downloads ->
			val collectionDownloads = downloads.filter { it.songId in songIds }
			when {
				collectionDownloads.isEmpty() -> DownloadStatus.NOT_DOWNLOADED
				// Queued counts as in-progress to the rest of the app: the collection-level button
				// should read "downloading" (and offer cancel) the moment work is accepted, not
				// only once a permit frees up.
				collectionDownloads.any {
					it.status == DownloadStatus.DOWNLOADING || it.status == DownloadStatus.QUEUED
				} -> DownloadStatus.DOWNLOADING

				collectionDownloads.any { it.status == DownloadStatus.FAILED } -> DownloadStatus.FAILED
				(collectionDownloads.size == songIds.size &&
					collectionDownloads.all { it.status == DownloadStatus.DOWNLOADED })
					-> DownloadStatus.DOWNLOADED

				else -> DownloadStatus.NOT_DOWNLOADED
			}
		}
	}

	fun clearAllDownloads() {
		scope.launch(Dispatchers.IO) {
			cancelAllActiveDownloads()
			storageManager.clearDownloads()
			downloadDao.clearAllDownloads()
			Logger.i("DownloadManager", "cleared all downloads")
		}
	}

	private suspend fun executeDownloadProcess(
		song: DomainSong,
		bitrate: Int = 0,
		format: String? = null,
		source: String = DownloadSource.MANUAL
	) {
		try {
			Logger.i("DownloadManager", "beginning download for ${song.id}")
			downloadDao.updateProgress(song.id, DownloadStatus.DOWNLOADING, 0f, now())

			cacheCoverArt(song.coverArtId)
			cacheAlbumCoverArt(song.albumId)
			cacheLyrics(song)
			downloadAudioFile(song, bitrate, format, source)

		} catch (e: Exception) {
			if (e is CancellationException) throw e
			Logger.e("DownloadManager", "Failed to download song ${song.id}", e)
			// Keep the request's settings and attempt count on the failed row so the download
			// center can show WHY it failed and retry it exactly as it was first asked for.
			val existing = downloadDao.getDownloadById(song.id)
			downloadDao.insertDownload(
				DownloadEntity(
					songId = song.id,
					status = DownloadStatus.FAILED,
					progress = 0f,
					maxBitRate = bitrate,
					format = format,
					error = e.message ?: e::class.simpleName ?: "Unknown error",
					retryCount = existing?.retryCount ?: 0,
					sourcePolicy = source,
					createdAt = existing?.createdAt?.takeIf { it > 0L } ?: now(),
					updatedAt = now()
				)
			)
		} finally {
			activeDownloadsMutex.withLock {
				activeDownloads.remove(song.id)
			}
		}
	}

	private suspend fun cacheCoverArt(coverId: String?) {
		if (coverId == null) return

		Logger.i("DownloadManager", "caching cover art for $coverId")
		val coverArtUrl = sessionManager.getCoverArtUrl(coverId)

		val imageRequest = ImageRequest.Builder(coilPlatformContext)
			.data(coverArtUrl)
			.size(Size.ORIGINAL)
			.memoryCacheKey(coverId)
			.diskCacheKey(coverId)
			.diskCachePolicy(CachePolicy.ENABLED)
			.memoryCachePolicy(CachePolicy.DISABLED)
			.build()

		SingletonImageLoader.get(coilPlatformContext).execute(imageRequest)
		Logger.i("DownloadManager", "cached cover art for $coverId")
	}

	private suspend fun cacheAlbumCoverArt(albumId: String?) {
		if (albumId == null) return

		try {
			val albumWithSongs = albumDao.getAlbumById(albumId)
			val albumCoverId = albumWithSongs?.album?.coverArtId

			if (albumCoverId != null) {
				Logger.i("DownloadManager", "Found album cover $albumCoverId for album $albumId")
				cacheCoverArt(albumCoverId)
			}
		} catch (e: Exception) {
			if (e is CancellationException) throw e
			Logger.e("DownloadManager", "Failed to cache album cover art for album $albumId", e)
		}
	}

	private suspend fun cacheLyrics(song: DomainSong) {
		Logger.i("DownloadManager", "caching lyrics for ${song.id}")
		try {
			val lyricsResult = lyricsRepository.fetchLyrics(song)
			if (lyricsResult != null && lyricsResult.rawContent != null) {
				lyricDao.insertLyrics(
					LyricEntity(
						song.id,
						lyricsResult.rawContent,
						lyricsResult.provider
					)
				)
				Logger.i("DownloadManager", "cached lyrics for ${song.id}")
			}
		} catch (e: Exception) {
			if (e is CancellationException) throw e
			Logger.e("DownloadManager", "Failed to cache lyrics for ${song.id}", e)
		}
	}

	private suspend fun downloadAudioFile(
		song: DomainSong,
		bitrate: Int = 0,
		format: String? = null,
		source: String = DownloadSource.MANUAL
	) {
		var lastProgress = 0f
		var progressJob: Job? = null

		val request = client.prepareRequest(sessionManager.api.getStreamUrl(song.id, bitrate, format)) {
			method = HttpMethod.Get
			onDownload { bytesSentTotal, contentLength ->
				if (contentLength != null && contentLength > 0L) {
					val progress = (bytesSentTotal.toDouble() / contentLength).toFloat()
					if (progress - lastProgress >= 0.01f || progress == 1f) {
						lastProgress = progress

						progressJob?.cancel()

						progressJob = scope.launch {
							downloadDao.updateProgress(
								song.id,
								DownloadStatus.DOWNLOADING,
								progress,
								now()
							)
						}
					}
				}
			}
		}

		val path = storageManager.getDownloadPath(song.id, song.fileExtension)
		val tempPath = storageManager.getTempDownloadPath(song.id, song.fileExtension)

		try {
			request.execute { response ->
				// Stream to a `.part` file. Until the transfer completes, the REAL path either
				// doesn't exist or still holds the previous good copy — so a cancel or a crash
				// can never leave a truncated file that the player treats as a full download.
				storageManager.saveFile(tempPath, response.bodyAsChannel())
			}

			progressJob?.cancel()

			if (!storageManager.finalizeFile(tempPath, path)) {
				throw IllegalStateException("Could not finalize download file")
			}

			val existing = downloadDao.getDownloadById(song.id)
			downloadDao.insertDownload(
				DownloadEntity(
					songId = song.id,
					status = DownloadStatus.DOWNLOADED,
					progress = 1f,
					filePath = path,
					maxBitRate = bitrate,
					format = format,
					fileSize = storageManager.getFileSize(path),
					error = null,
					retryCount = existing?.retryCount ?: 0,
					sourcePolicy = source,
					createdAt = existing?.createdAt?.takeIf { it > 0L } ?: now(),
					updatedAt = now()
				)
			)
			Logger.i("DownloadManager", "wrote download for ${song.id}")
		} catch (e: Throwable) {
			progressJob?.cancel()
			// Cancellation included: never leave the scratch file behind to rot.
			storageManager.deleteFile(tempPath)
			throw e
		}
	}

	private fun now(): Long = Clock.System.now().toEpochMilliseconds()

	private companion object {
		/** Hard ceiling on the user-configurable max-concurrency setting. */
		const val MAX_CONCURRENCY = 10
	}
}
