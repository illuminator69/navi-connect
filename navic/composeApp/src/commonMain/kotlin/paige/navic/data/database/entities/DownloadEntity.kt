package paige.navic.data.database.entities

import androidx.room3.Entity
import androidx.room3.PrimaryKey
import kotlinx.serialization.Serializable

/**
 * One row per song the app has been asked to keep offline.
 *
 * Beyond the raw status, the row records HOW the file was fetched (quality, format), WHAT it cost
 * (size on disk), WHY it failed, and WHO asked for it ([sourcePolicy]) — so the download center can
 * show a real history, retry a failure with the settings it was originally requested with, and tell
 * a manual download apart from one a playlist policy manages on the user's behalf.
 *
 * Every field added after v3 carries a default, so the v3→v4 migration is a plain set of
 * `ALTER TABLE ADD COLUMN`s and existing rows — and the files they point at — survive untouched.
 */
@Serializable
@Entity
data class DownloadEntity(
	@PrimaryKey val songId: String,
	val status: DownloadStatus,
	val progress: Float = 0f,
	val filePath: String? = null,
	/** Transcode ceiling the file was requested at; 0 = original quality. */
	val maxBitRate: Int = 0,
	/** Requested container ("opus", "mp3"); null = server original. */
	val format: String? = null,
	/** Bytes on disk. Only meaningful once [status] is [DownloadStatus.DOWNLOADED]. */
	val fileSize: Long = 0L,
	/** Why the last attempt failed — surfaced in the download center, not just logged. */
	val error: String? = null,
	val retryCount: Int = 0,
	/** Which mechanism asked for this file; see [DownloadSource]. */
	val sourcePolicy: String = DownloadSource.MANUAL,
	/** Epoch millis of the first request, and of the last status change. */
	val createdAt: Long = 0L,
	val updatedAt: Long = 0L
)

/**
 * Who asked for a download. Plain strings rather than an enum: this is a stored column that the
 * playlist policies also write, and they key their own state on playlist ids.
 */
object DownloadSource {
	const val MANUAL = "manual"
	const val ALBUM = "album"
	const val PLAYLIST = "playlist"
	const val LIBRARY = "library"
	const val QUEUE = "queue"
}

@Serializable
enum class DownloadStatus {
	NOT_DOWNLOADED,

	/**
	 * Accepted, but waiting on a concurrency permit. Downloads used to be written as
	 * [DOWNLOADING] the moment they were requested while actually sitting behind the semaphore,
	 * so "download album" on 50 tracks showed 50 spinners with no way to tell what was really
	 * transferring.
	 */
	QUEUED,
	DOWNLOADING,
	DOWNLOADED,
	FAILED
}
