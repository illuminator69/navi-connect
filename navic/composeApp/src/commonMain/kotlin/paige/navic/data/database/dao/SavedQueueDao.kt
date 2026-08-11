package paige.navic.data.database.dao

import androidx.room3.Dao
import androidx.room3.Insert
import androidx.room3.OnConflictStrategy
import androidx.room3.Query
import kotlinx.coroutines.flow.Flow
import paige.navic.data.database.entities.SavedQueueEntity

/** Projection for [SavedQueueDao.getIndexRows] — the identity index, without the queue blobs. */
data class SavedQueueIndexRow(
	val id: String,
	val songIdsCsv: String?,
	val updatedAt: Long
)

@Dao
interface SavedQueueDao {
	@Insert(onConflict = OnConflictStrategy.REPLACE)
	suspend fun upsert(queue: SavedQueueEntity)

	/**
	 * Cheap path for progress ticks: the queue itself is unchanged, so only the playback cursor and
	 * timestamp move — no need to rewrite the (potentially large) [SavedQueueEntity.queueJson] blob.
	 *
	 * [SavedQueueEntity.coverArtId] is deliberately NOT touched: the card's art is frozen at the
	 * queue's birth (PROTOCOL.md §8.3), which is what the hub and Feishin do — moving it with the
	 * cursor made the same shared record render different art on each client. Only the title follows
	 * the resume point.
	 */
	@Query(
		"UPDATE SavedQueueEntity SET currentIndex = :index, currentSongId = :songId, " +
			"currentSongName = :songName, positionMs = :positionMs, " +
			"updatedAt = :updatedAt WHERE id = :id"
	)
	suspend fun updateProgress(
		id: String,
		index: Int,
		songId: String?,
		songName: String?,
		positionMs: Long,
		updatedAt: Long
	)

	@Query("SELECT * FROM SavedQueueEntity ORDER BY updatedAt DESC")
	fun observeAll(): Flow<List<SavedQueueEntity>>

	/** Non-reactive snapshot — used to push local rows up to the hub on (re)connect. */
	@Query("SELECT * FROM SavedQueueEntity")
	suspend fun getAll(): List<SavedQueueEntity>

	/** Drop every local row — used before adopting the hub's authoritative history. */
	@Query("DELETE FROM SavedQueueEntity")
	suspend fun clear()

	/** Drop local rows the hub no longer has (so a delete on another client propagates). */
	@Query("DELETE FROM SavedQueueEntity WHERE id NOT IN (:ids)")
	suspend fun deleteNotIn(ids: List<String>)

	/**
	 * Just enough of every row to rebuild the in-memory membership index — deliberately not
	 * `SELECT *`, so priming it never pulls twenty queue blobs into memory.
	 */
	@Query("SELECT id, songIdsCsv, updatedAt FROM SavedQueueEntity")
	suspend fun getIndexRows(): List<SavedQueueIndexRow>

	/** Rows written before `songIdsCsv` existed, so the index can backfill them from their blob. */
	@Query("SELECT * FROM SavedQueueEntity WHERE songIdsCsv IS NULL")
	suspend fun getRowsMissingIds(): List<SavedQueueEntity>

	@Query("UPDATE SavedQueueEntity SET songIdsCsv = :ids WHERE id = :id")
	suspend fun setSongIds(id: String, ids: String)

	@Query("SELECT * FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getById(id: String): SavedQueueEntity?

	@Query("SELECT name FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getName(id: String): String?

	@Query("SELECT createdAt FROM SavedQueueEntity WHERE id = :id LIMIT 1")
	suspend fun getCreatedAt(id: String): Long?

	/**
	 * Renames bump [SavedQueueEntity.updatedAt]: the hub merges shared history newest-wins, so an
	 * offline rename that kept the old timestamp lost to the hub's copy on the next sync.
	 */
	@Query("UPDATE SavedQueueEntity SET name = :name, updatedAt = :updatedAt WHERE id = :id")
	suspend fun rename(id: String, name: String?, updatedAt: Long)

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
