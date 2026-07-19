package paige.navic.domain.repositories

import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.json.Json
import paige.navic.data.database.dao.SavedQueueDao
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.DomainSong
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
import kotlin.time.Clock
import kotlin.time.ExperimentalTime

/**
 * The store behind the Symfonium-style "saved queues" list. Owns the rolling cache of automatically
 * captured queue snapshots (capped at [MAX_QUEUES], oldest by `updatedAt` evicted) and the encode /
 * decode of the queue blob.
 *
 * [upsert] is called from the player's central state observer on every debounced change, so it is
 * written to be cheap: an unchanged queue (the common case — a progress tick) only moves the cursor
 * via [SavedQueueDao.updateProgress] instead of re-encoding the whole `List<DomainSong>`.
 */
@OptIn(ExperimentalTime::class)
class SavedQueueRepository(
	private val savedQueueDao: SavedQueueDao
) {
	private val json = Json { ignoreUnknownKeys = true }

	// Cheap-path gate: remember the queue we last fully wrote (by reference + id), so identical
	// follow-up ticks skip the blob rewrite. Reset implicitly whenever the id or contents differ.
	private var cachedId: String? = null
	private var cachedQueueRef: List<DomainSong>? = null
	private var cachedSig: String = ""

	fun observeAll(): Flow<List<SavedQueueEntity>> = savedQueueDao.observeAll()

	suspend fun get(id: String): SavedQueueEntity? = savedQueueDao.getById(id)

	suspend fun rename(id: String, name: String?) =
		savedQueueDao.rename(id, name?.trim()?.takeIf { it.isNotEmpty() })

	suspend fun delete(id: String) = savedQueueDao.deleteById(id)

	suspend fun deleteOthers(keepId: String) = savedQueueDao.deleteOthers(keepId)

	fun decodeQueue(entity: SavedQueueEntity): List<DomainSong> =
		try {
			json.decodeFromString(entity.queueJson)
		} catch (e: Exception) {
			Logger.e("SavedQueueRepository", "Failed to decode saved queue ${entity.id}", e)
			emptyList()
		}

	/**
	 * Insert or update the snapshot for [id] from the current player [state]. Empty queues are
	 * ignored (a cleared queue keeps its last snapshot in history rather than blanking it).
	 */
	suspend fun upsert(id: String, state: PlayerUiState) {
		val queue = state.queue
		if (queue.isEmpty()) return

		val now = Clock.System.now().toEpochMilliseconds()
		val index = state.currentIndex.coerceIn(0, queue.lastIndex)
		val currentSong = state.currentSong ?: queue.getOrNull(index)
		val positionMs = (state.progress.coerceIn(0f, 1f) *
			(currentSong?.duration?.inWholeMilliseconds ?: 0L)).toLong()

		val sig = signature(queue)
		val unchanged = id == cachedId && sig == cachedSig

		if (unchanged) {
			// Same queue, just a moving cursor — no blob rewrite.
			savedQueueDao.updateProgress(
				id = id,
				index = index,
				songId = currentSong?.id,
				coverArtId = currentSong?.coverArtId,
				positionMs = positionMs,
				updatedAt = now
			)
			return
		}

		// New session, or the queue was edited: full write. Preserve the row's identity fields
		// (createdAt, user-assigned name) since REPLACE would otherwise clobber them.
		val existing = savedQueueDao.getById(id)
		savedQueueDao.upsert(
			SavedQueueEntity(
				id = id,
				name = existing?.name,
				sourceName = state.currentCollection?.name,
				queueJson = json.encodeToString(queue),
				currentIndex = index,
				currentSongId = currentSong?.id,
				coverArtId = currentSong?.coverArtId,
				positionMs = positionMs,
				shuffle = state.isShuffleEnabled,
				repeatMode = state.repeatMode,
				songCount = queue.size,
				sourceKind = state.savedQueueKind,
				createdAt = existing?.createdAt ?: now,
				updatedAt = now
			)
		)
		savedQueueDao.evictBeyond(MAX_QUEUES)

		cachedId = id
		cachedQueueRef = queue
		cachedSig = sig
	}

	// Same reference-cached signature trick HubManager uses: avoid rebuilding a huge id string when
	// the queue list instance hasn't changed.
	private fun signature(queue: List<DomainSong>): String {
		if (queue !== cachedQueueRef) {
			return queue.joinToString(",") { it.id }
		}
		return cachedSig
	}

	companion object {
		const val MAX_QUEUES = 20
	}
}
