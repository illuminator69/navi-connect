package paige.navic.data.database.dao

import androidx.room3.Dao
import androidx.room3.Insert
import androidx.room3.OnConflictStrategy
import androidx.room3.Query
import kotlinx.coroutines.flow.Flow
import paige.navic.data.database.entities.DownloadEntity
import paige.navic.data.database.entities.DownloadStatus

@Dao
interface DownloadDao {
	@Insert(onConflict = OnConflictStrategy.REPLACE)
	suspend fun insertDownload(download: DownloadEntity)

	@Query("SELECT * FROM DownloadEntity WHERE songId = :songId")
	suspend fun getDownloadById(songId: String): DownloadEntity?

	@Query("SELECT * FROM DownloadEntity")
	fun getAllDownloads(): Flow<List<DownloadEntity>>

	@Query("SELECT * FROM DownloadEntity")
	suspend fun getAllDownloadsList(): List<DownloadEntity>

	@Query("SELECT COUNT(*) FROM DownloadEntity WHERE status = :status")
	fun getDownloadsCount(status: DownloadStatus = DownloadStatus.DOWNLOADED): Flow<Int>

	@Query("SELECT * FROM DownloadEntity WHERE status = :status ORDER BY updatedAt DESC")
	suspend fun getDownloadsByStatus(status: DownloadStatus): List<DownloadEntity>

	/**
	 * Ids only, for filtering the song list down to downloaded tracks. Songs live in a different
	 * database file, so this can't be a JOIN — the ids get bound into the song query instead.
	 */
	@Query("SELECT songId FROM DownloadEntity WHERE status = :status")
	suspend fun getSongIdsByStatus(
		status: DownloadStatus = DownloadStatus.DOWNLOADED
	): List<String>

	/**
	 * Summed from the stored column instead of stat-ing every file, so showing a storage total
	 * doesn't mean one filesystem call per downloaded track.
	 */
	@Query("SELECT COALESCE(SUM(fileSize), 0) FROM DownloadEntity WHERE status = :status")
	fun getTotalSize(status: DownloadStatus = DownloadStatus.DOWNLOADED): Flow<Long>

	/** Rows carried over from v3, which predate the size column and so have nothing recorded. */
	@Query(
		"SELECT * FROM DownloadEntity " +
			"WHERE status = :status AND fileSize = 0 AND filePath IS NOT NULL"
	)
	suspend fun getDownloadsMissingSize(
		status: DownloadStatus = DownloadStatus.DOWNLOADED
	): List<DownloadEntity>

	@Query("UPDATE DownloadEntity SET fileSize = :fileSize WHERE songId = :songId")
	suspend fun updateFileSize(songId: String, fileSize: Long)

	@Query("DELETE FROM DownloadEntity WHERE songId = :songId")
	suspend fun deleteDownload(songId: String)

	@Query(
		"UPDATE DownloadEntity SET status = :status, progress = :progress, updatedAt = :updatedAt " +
			"WHERE songId = :songId"
	)
	suspend fun updateProgress(
		songId: String,
		status: DownloadStatus,
		progress: Float,
		updatedAt: Long
	)

	@Query("DELETE FROM DownloadEntity")
	suspend fun clearAllDownloads()

	/** Clear one section of the download center at once (e.g. "dismiss all failed"). */
	@Query("DELETE FROM DownloadEntity WHERE status = :status")
	suspend fun clearDownloadsByStatus(status: DownloadStatus)
}
