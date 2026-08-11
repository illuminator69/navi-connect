package paige.navic.domain.manager

import com.russhwolf.settings.Settings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import paige.navic.data.database.dao.PlaylistDao
import paige.navic.data.database.entities.DownloadSource
import paige.navic.data.database.entities.SongEntity
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.data.database.relations.PlaylistSong
import paige.navic.domain.repositories.DbRepository
import paige.navic.util.core.Logger

@Serializable
data class PlaylistDownloadPolicy(
	val playlistId: String,
	val playlistName: String = "",
	/** "permanent" keeps the whole playlist; "rolling" keeps the first N (and/or first X bytes). */
	val mode: String = MODE_PERMANENT,
	val rollingLimit: Int = 50,
	/**
	 * Byte budget for rolling mode; 0 = no size cap (count-only). When both this and [rollingLimit]
	 * are set, the more restrictive one wins — songs are kept in playlist order until EITHER cap is
	 * reached. Roadmap item 7's "rolling(N songs/GB)".
	 */
	val budgetBytes: Long = 0,
	/** 0 = original quality; otherwise transcode ceiling in kbps. */
	val maxBitRate: Int = 0,
	/** "" = original container; otherwise e.g. "opus", "mp3". */
	val format: String = "",
	/** Song ids THIS policy downloaded — only these may be evicted. */
	val managed: List<String> = emptyList()
) {
	companion object {
		const val MODE_PERMANENT = "permanent"
		const val MODE_ROLLING = "rolling"
	}
}

/**
 * Auto-download for (smart) playlists, Symphonium-style: each playlist can opt
 * into a download policy — permanent (whole playlist) or rolling cache (first
 * N tracks; tracks that leave the playlist are evicted). Combined with
 * server-side smart playlists this gives self-refreshing offline mixes.
 *
 * Eviction only ever touches songs this manager downloaded itself (`managed`),
 * never manual downloads, and never songs another policy still wants.
 */
class PlaylistDownloadManager(
	private val settings: Settings,
	private val dbRepository: DbRepository,
	private val playlistDao: PlaylistDao,
	private val downloadManager: DownloadManager
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
	private val json = Json { ignoreUnknownKeys = true }

	private val _policies = MutableStateFlow<Map<String, PlaylistDownloadPolicy>>(emptyMap())
	val policies: StateFlow<Map<String, PlaylistDownloadPolicy>> = _policies.asStateFlow()

	init {
		load()
		scope.launch {
			// Refresh managed playlists shortly after startup (lets login/sync
			// settle first), then every 6 hours while the app lives.
			delay(30_000)
			while (true) {
				syncAll()
				delay(6 * 60 * 60 * 1000L)
			}
		}
	}

	fun policyFor(playlistId: String): PlaylistDownloadPolicy? = _policies.value[playlistId]

	fun setPolicy(policy: PlaylistDownloadPolicy) {
		val existing = _policies.value[policy.playlistId]
		val merged = policy.copy(managed = existing?.managed ?: emptyList())
		_policies.value = _policies.value + (policy.playlistId to merged)
		persist()
		scope.launch { syncPlaylist(policy.playlistId) }
	}

	fun removePolicy(playlistId: String, deleteDownloads: Boolean) {
		val policy = _policies.value[playlistId] ?: return
		if (deleteDownloads) {
			val keptElsewhere = _policies.value.values
				.filter { it.playlistId != playlistId }
				.flatMap { it.managed }
				.toSet()
			policy.managed
				.filter { it !in keptElsewhere }
				.forEach { downloadManager.deleteDownload(it) }
		}
		_policies.value = _policies.value - playlistId
		persist()
	}

	suspend fun syncAll() {
		_policies.value.keys.forEach { playlistId ->
			try {
				syncPlaylist(playlistId)
			} catch (e: Exception) {
				Logger.e("PlaylistDownloadManager", "sync failed for $playlistId", e)
			}
		}
	}

	suspend fun syncPlaylist(playlistId: String) {
		val policy = _policies.value[playlistId] ?: return

		// Refresh contents from the server first — for smart playlists this is
		// where the "dynamically updated" part comes from.
		runCatching { dbRepository.syncPlaylistSongs(playlistId) }

		val playlist = playlistDao.getPlaylistById(playlistId) ?: return
		val ordered = playlist.songs.sortedBy { it.crossRef.position }
		val target = if (policy.mode == PlaylistDownloadPolicy.MODE_ROLLING) {
			rollingWindow(ordered, policy)
		} else {
			ordered
		}
		val targetIds = target.map { it.song.songId }.toSet()

		// Evict managed songs that left the target window — unless some other
		// policy still manages them.
		val managedByOthers = _policies.value.values
			.filter { it.playlistId != playlistId }
			.flatMap { it.managed }
			.toSet()
		val evicted = policy.managed.filter { it !in targetIds && it !in managedByOthers }
		evicted.forEach { downloadManager.deleteDownload(it) }

		// Download what's missing, at the policy's quality.
		val downloaded = downloadManager.downloadedSongs.value.keys
		// A song that has already failed MAX_DOWNLOAD_RETRIES times is left alone until the user
		// retries it by hand — otherwise a permanently-failing track (deleted server-side, transcode
		// error, …) is re-queued on every 6h sync forever, draining battery and data.
		val failedRetryCounts = downloadManager.failedRetryCounts()
		val toDownload = target.filter { item ->
			val id = item.song.songId
			id !in downloaded &&
				(failedRetryCounts[id] ?: 0) < DownloadManager.MAX_DOWNLOAD_RETRIES
		}
		val quality = DownloadManager.DownloadQuality(
			policy.maxBitRate,
			policy.format.ifBlank { null }
		)
		toDownload.forEach {
			downloadManager.downloadSong(
				it.song.toDomainModel(),
				// The policy's own quality wins over the global download preference.
				quality = quality,
				// Tagged so the download center can show these as playlist-managed rather than
				// manual — the user didn't ask for each of these files individually.
				source = DownloadSource.PLAYLIST,
				// A re-attempt of a previously-failed track counts toward the retry cap; a fresh
				// download does not.
				incrementRetry = it.song.songId in failedRetryCounts
			)
		}

		val newManaged =
			(policy.managed.filter { it in targetIds } + toDownload.map { it.song.songId })
				.distinct()
		_policies.value = _policies.value +
			(playlistId to policy.copy(managed = newManaged))
		persist()
		if (evicted.isNotEmpty() || toDownload.isNotEmpty()) {
			Logger.i(
				"PlaylistDownloadManager",
				"${policy.playlistName.ifBlank { playlistId }}: +${toDownload.size} -${evicted.size}"
			)
		}
	}

	/**
	 * The prefix of [ordered] the rolling policy should keep: playlist order, stopping at whichever
	 * of the count cap or the byte budget is reached first. At least the first song is always kept,
	 * even if it alone exceeds the budget — a rolling cache of zero songs is never what's wanted.
	 */
	private suspend fun rollingWindow(
		ordered: List<PlaylistSong>,
		policy: PlaylistDownloadPolicy
	): List<PlaylistSong> {
		val countCap = policy.rollingLimit.coerceAtLeast(1)
		val byCount = ordered.take(countCap)
		if (policy.budgetBytes <= 0) return byCount

		// Prefer the real on-disk size for anything already downloaded (any policy), so the budget
		// tracks actual storage rather than an estimate once files exist.
		val actualSizes = downloadManager.downloadedFileSizes()
		val kept = mutableListOf<PlaylistSong>()
		var used = 0L
		for (item in byCount) {
			val size = actualSizes[item.song.songId]?.takeIf { it > 0 }
				?: estimatedBytes(item.song, policy)
			if (kept.isEmpty() || used + size <= policy.budgetBytes) {
				kept.add(item)
				used += size
			} else {
				break
			}
		}
		return kept
	}

	/**
	 * Best-effort byte size for a not-yet-downloaded song, for budgeting only. Uses the transcode
	 * ceiling when the policy transcodes (bytes ≈ kbps × duration), otherwise the server's reported
	 * original size, falling back to a bitrate estimate when either is missing.
	 */
	private fun estimatedBytes(song: SongEntity, policy: PlaylistDownloadPolicy): Long {
		val durationSec = song.duration.inWholeSeconds
		// kbps × 1000 bits/s ÷ 8 bits/byte = kbps × 125 bytes/s.
		fun fromBitrate(kbps: Int) = kbps.toLong() * 125L * durationSec
		return when {
			policy.maxBitRate > 0 && durationSec > 0 -> fromBitrate(policy.maxBitRate)
			song.fileSize > 0 -> song.fileSize
			(song.bitRate ?: 0) > 0 && durationSec > 0 -> fromBitrate(song.bitRate!!)
			else -> 0L
		}
	}

	private fun load() {
		try {
			val raw = settings.getString(KEY, "")
			if (raw.isNotBlank()) {
				val list: List<PlaylistDownloadPolicy> = json.decodeFromString(raw)
				_policies.value = list.associateBy { it.playlistId }
			}
		} catch (e: Exception) {
			Logger.e("PlaylistDownloadManager", "failed to load policies", e)
		}
	}

	private fun persist() {
		try {
			settings.putString(KEY, json.encodeToString(_policies.value.values.toList()))
		} catch (e: Exception) {
			Logger.e("PlaylistDownloadManager", "failed to persist policies", e)
		}
	}

	private companion object {
		const val KEY = "playlistDownloadPolicies"
	}
}
