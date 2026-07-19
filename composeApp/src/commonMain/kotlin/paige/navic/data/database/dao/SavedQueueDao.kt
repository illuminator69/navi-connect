package paige.navic.data.database.dao

import androidx.room3.Dao
import androidx.room3.Insert
import androidx.room3.OnConflictStrategy
import androidx.room3.Query
import kotlinx.coroutines.flow.Flow
import paige.navic.data.database.entities.SavedQueueEntity

@Dao
interface SavedQueueDao {
	@Insert(onConflict = OnConflictStrategy.REPLACE)
	suspend fun upsert(queue: SavedQueueEntity)

	/**
	 * Cheap path for progress ticks: the queue itself is unchanged, so only the playback cursor and
	 * timestamp move — no need to rewrite the (potentially large) [SavedQueueEntity.queueJson] blob.
	 */
	@Query(
		"UPDATE SavedQueueEntity SET currentIndex = :index, currentSongId = :songId, " +
			"coverArtId = :coverArtId, positionMs = :positionMs, updatedAt = :updatedAt WHERE id = :id"
	)
	suspend fun updateProgress(
		id: String,
		index: Int,
		songId: String?,
		coverArtId: String?,
		positionMs: Long,
		updatedAt: Long
	)

	@Query("SELECT * FROM SavedQueueEntity ORDER BY updatedAt DESC")
	fun observeAll(): Flow<List<SavedQueueEntity>>

	@Query("SELECT * FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getById(id: String): SavedQueueEntity?

	@Query("SELECT name FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getName(id: String): String?

	@Query("SELECT createdAt FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getCreatedAt(id: String): Long?

	@Query("UPDATE SavedQueueEntity SET name = :name WHERE id = :id")
	suspend fun rename(id: String, name: String?)

	@Query("DELETE FROM SavedQueueEntity WHERE id = :id")
	suspend fun deleteById(id: String)

	@Query("DELETE FROM SavedQueueEntity WHERE id != :keepId")
	suspend fun deleteOthers(keepId: String)

	/** Rolling-cache eviction: keep the [limit] most-recently-updated rows, drop the rest. */
	@Query(
		"DELETE FROM SavedQueueEntity WHERE id NOT IN " +
			"(SELECT id FROM SavedQueueEntity ORDER BY updatedAt DESC LIMIT :limit)"
	)
	suspend fun evictBeyond(limit: Int)
}
