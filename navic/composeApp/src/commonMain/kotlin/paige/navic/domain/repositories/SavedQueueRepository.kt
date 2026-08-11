package paige.navic.domain.repositories

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import paige.navic.data.database.dao.SavedQueueDao
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.DomainSong
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
import kotlin.concurrent.Volatile
import kotlin.math.roundToLong
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
 *
 * A record's identity is a **listening session**, not a track list: `sourceName` and `coverArtId` are
 * stamped when the queue is born and never rewritten, and [findMatching] recognises a queue we already
 * have a record for so replaying an album refreshes its card instead of cloning it. Both rules mirror
 * Feishin and the hub (`hub.py._upsert_saved_queue`), which is the point — all three render one shared
 * history.
 */
@OptIn(ExperimentalTime::class)
class SavedQueueRepository(
	private val savedQueueDao: SavedQueueDao,
	private val preferenceManager: PreferenceManager
) {
	private val json = Json { ignoreUnknownKeys = true }

	// Cheap-path gate: remember the queue we last fully wrote (by reference + id), so identical
	// follow-up ticks skip the blob rewrite. Reset implicitly whenever the id or contents differ.
	private var cachedId: String? = null
	private var cachedQueueRef: List<DomainSong>? = null
	private var cachedSig: String = ""

	// Membership index (record id -> its song ids + updatedAt), kept in memory so [findMatching] can
	// answer synchronously: the queue-replace paths that need an id are not suspending.
	//
	// Copy-on-write rather than a mutable map, because those synchronous readers run on the main
	// thread while every writer here runs on an IO coroutine — iterating a map another coroutine is
	// mutating is a ConcurrentModificationException at worst and a torn read (a duplicate history
	// card) at best. Readers take one snapshot of the reference; writers swap a whole new map in
	// under [indexMutex].
	@Volatile
	private var index: Map<String, IndexEntry> = emptyMap()
	private val indexMutex = Mutex()

	private data class IndexEntry(val ids: Set<String>, val updatedAt: Long)

	// Deletions not yet acknowledged by the hub, id -> when. Loaded lazily from preferences and
	// then kept in memory, since [isDeleted] is called per record during a hub broadcast.
	@Volatile
	private var tombstones: Map<String, Long>? = null
	private val tombstoneMutex = Mutex()

	fun observeAll(): Flow<List<SavedQueueEntity>> = savedQueueDao.observeAll()

	suspend fun get(id: String): SavedQueueEntity? = savedQueueDao.getById(id)

	/** A hub saved-queue record, songs already resolved to [DomainSong] (placeholders for un-synced). */
	data class RemoteSavedQueue(
		val id: String,
		val name: String?,
		val sourceName: String?,
		val songs: List<DomainSong>,
		val currentIndex: Int,
		val currentSongId: String?,
		val currentSongName: String?,
		val coverArtId: String?,
		val positionMs: Long,
		val shuffle: Boolean,
		val repeatMode: Int,
		val sourceKind: String,
		val createdAt: Long,
		val updatedAt: Long,
		val songCount: Int
	)

	/** Non-reactive snapshot of local rows (to push up to the hub on connect). */
	suspend fun allForSync(): List<SavedQueueEntity> = savedQueueDao.getAll()

	/**
	 * Load the membership index once at startup, backfilling `songIdsCsv` for rows written before that
	 * column existed (a one-off decode of those rows only — afterwards the index costs one projection
	 * query with no blobs). Safe to call more than once.
	 */
	suspend fun primeIndex() {
		try {
			savedQueueDao.getRowsMissingIds().forEach { row ->
				val ids = decodeQueue(row).joinToString(",") { it.id }
				savedQueueDao.setSongIds(row.id, ids)
			}
			val fresh = mutableMapOf<String, IndexEntry>()
			savedQueueDao.getIndexRows().forEach { row ->
				val ids = row.songIdsCsv?.split(",")?.filter { it.isNotEmpty() }.orEmpty()
				if (ids.isNotEmpty()) fresh[row.id] = IndexEntry(ids.toSet(), row.updatedAt)
			}
			indexMutex.withLock { index = fresh }
		} catch (e: Exception) {
			Logger.e("SavedQueueRepository", "Failed to prime the saved-queue index", e)
		}
	}

	/**
	 * The id of an existing record whose songs are ~the same set as [ids], or null. This is what stops
	 * a relaunch, a replay of the same album, or a queue restore from minting a second card for one
	 * listening session. Best score wins; ties go to the more recently updated record.
	 *
	 * The threshold is deliberately a *membership* ratio rather than an ordered prefix: a reorder, a
	 * removal, a play-next or a shuffle must all still count as the same queue.
	 */
	fun findMatching(ids: List<String>): String? {
		if (ids.isEmpty()) return null
		val wanted = ids.toSet()
		// One read of the volatile reference: the map it points at is never mutated in place.
		val snapshot = index
		var bestId: String? = null
		var bestScore = 0.0
		var bestUpdatedAt = 0L
		snapshot.forEach { (id, entry) ->
			if (entry.ids.isEmpty()) return@forEach
			val shared = entry.ids.count { it in wanted }
			val score = shared.toDouble() / maxOf(entry.ids.size, wanted.size)
			if (score < SAME_QUEUE_THRESHOLD) return@forEach
			if (score > bestScore || (score == bestScore && entry.updatedAt > bestUpdatedAt)) {
				bestId = id
				bestScore = score
				bestUpdatedAt = entry.updatedAt
			}
		}
		return bestId
	}

	/**
	 * Adopt the hub's authoritative history: rows the hub no longer has are dropped (so a delete on any
	 * client propagates), and rows it still has are rewritten **only when they actually changed**. The
	 * hub rebroadcasts the whole list on every queue edit anywhere, so blindly re-encoding twenty blobs
	 * per broadcast churned the table and recomposed every screen observing it.
	 *
	 * Safe because online writes all go through the hub and offline rows are pushed up before this runs
	 * (see HubManager.syncLocalSavedQueuesUp).
	 */
	suspend fun replaceFromHub(records: List<RemoteSavedQueue>) {
		// Cap before anything else, matching Feishin's mergeFromHub: the hub force-includes the live
		// record even when the cap would push it out, so its list can be one longer than MAX_QUEUES.
		val capped = records.sortedByDescending { it.updatedAt }.take(MAX_QUEUES)
		if (capped.isEmpty()) {
			savedQueueDao.clear()
			indexMutex.withLock { index = emptyMap() }
		} else {
			savedQueueDao.deleteNotIn(capped.map { it.id })
			// Built up locally and swapped in once at the end — a per-record mutation would expose a
			// half-rebuilt index to the synchronous readers on the main thread.
			val fresh = mutableMapOf<String, IndexEntry>()
			capped.forEach { r ->
				val existing = savedQueueDao.getById(r.id)
				// Nothing new in this record — skip the blob rewrite entirely.
				if (existing != null &&
					existing.updatedAt == r.updatedAt &&
					existing.songCount == r.songCount &&
					existing.currentIndex == r.currentIndex
				) {
					fresh.putEntry(r.id, r.songs.map { it.id }, r.updatedAt)
					return@forEach
				}
				// Keep a local user-assigned name the hub record doesn't carry (an offline
				// rename that hasn't been merged up yet) — a rewrite would otherwise blank it.
				val localName = if (r.name == null) existing?.name else null
				val songIds = r.songs.map { it.id }
				savedQueueDao.upsert(
					SavedQueueEntity(
						id = r.id,
						name = r.name ?: localName,
						sourceName = r.sourceName ?: existing?.sourceName,
						queueJson = json.encodeToString(r.songs),
						songIdsCsv = songIds.joinToString(","),
						currentIndex = r.currentIndex,
						currentSongId = r.currentSongId,
						currentSongName = r.currentSongName,
						// Same "established wins, null is not established" rule as sourceName: the
						// cover is frozen at the queue's birth, so a record that already has one
						// keeps it rather than adopting whatever the broadcast happened to carry.
						coverArtId = existing?.coverArtId ?: r.coverArtId,
						positionMs = r.positionMs,
						shuffle = r.shuffle,
						repeatMode = r.repeatMode,
						songCount = r.songCount,
						sourceKind = r.sourceKind,
						createdAt = r.createdAt,
						updatedAt = r.updatedAt
					)
				)
				fresh.putEntry(r.id, songIds, r.updatedAt)
			}
			indexMutex.withLock { index = fresh }
		}
		// The blobs just changed wholesale — reset the cheap-path cache.
		invalidateCache()
	}

	suspend fun rename(id: String, name: String?) {
		val now = Clock.System.now().toEpochMilliseconds()
		savedQueueDao.rename(id, name?.trim()?.takeIf { it.isNotEmpty() }, now)
		// Keep the in-memory recency in step with the row, or findMatching's tie-break between two
		// equally-matching records decides on a timestamp Room has already moved past.
		indexMutex.withLock {
			index[id]?.let { index = index + (id to it.copy(updatedAt = now)) }
		}
	}

	suspend fun delete(id: String) {
		tombstone(listOf(id))
		savedQueueDao.deleteById(id)
		indexMutex.withLock { index = index - id }
		// The cheap path caches "the row for cachedId is up to date". Deleting the row the player is
		// still writing into left that belief in place, so every later tick issued an UPDATE against a
		// row that no longer exists and the live queue silently had no card.
		if (id == cachedId) invalidateCache()
	}

	/** [removedIds] is what the caller is dropping — needed so the deletions can be tombstoned. */
	suspend fun deleteOthers(keepId: String, removedIds: Collection<String>) {
		tombstone(removedIds.filter { it != keepId })
		savedQueueDao.deleteOthers(keepId)
		indexMutex.withLock { index = index.filterKeys { it == keepId } }
		if (cachedId != keepId) invalidateCache()
	}

	/** Drop the whole history (the "Clear all" surface). */
	suspend fun clearAll(removedIds: Collection<String>) {
		tombstone(removedIds)
		savedQueueDao.clear()
		indexMutex.withLock { index = emptyMap() }
		invalidateCache()
	}

	fun decodeQueue(entity: SavedQueueEntity): List<DomainSong> =
		try {
			json.decodeFromString<List<DomainSong>>(entity.queueJson).also {
				if (it.isEmpty()) {
					// Not fatal, but this row can never sync: the hub rejects a record with no
					// songs, so it would silently sit in local history forever.
					Logger.w("SavedQueueRepository", "Saved queue ${entity.id} decoded to no songs")
				}
			}
		} catch (e: Exception) {
			Logger.e("SavedQueueRepository", "Failed to decode saved queue ${entity.id}", e)
			emptyList()
		}

	// ----- offline deletion tombstones ------------------------------------------------------- //

	/**
	 * True if [id] was deleted locally and the hub hasn't been told yet. A hub broadcast built before
	 * our sync-up frame arrived still contains the row, so without this it reappears for one render.
	 */
	fun isDeleted(id: String): Boolean = loadedTombstones().containsKey(id)

	/** The deletions to push up on the next connect (see HubManager.syncLocalSavedQueuesUp). */
	fun pendingTombstoneIds(): List<String> = loadedTombstones().keys.toList()

	suspend fun tombstone(ids: Collection<String>) {
		if (ids.isEmpty()) return
		val now = Clock.System.now().toEpochMilliseconds()
		tombstoneMutex.withLock {
			val merged = loadedTombstones() + ids.associateWith { now }
			persistTombstones(merged)
		}
	}

	private fun loadedTombstones(): Map<String, Long> = tombstones ?: run {
		val cutoff = Clock.System.now().toEpochMilliseconds() - TOMBSTONE_TTL_MS
		val parsed = preferenceManager.deletedSavedQueueIds
			.split(",")
			.mapNotNull { entry ->
				val at = entry.substringAfterLast(':', "").toLongOrNull() ?: return@mapNotNull null
				val id = entry.substringBeforeLast(':')
				if (id.isEmpty() || at < cutoff) null else id to at
			}
			.toMap()
		parsed.also { tombstones = it }
	}

	private fun persistTombstones(all: Map<String, Long>) {
		// Newest kept when over the cap — an old deletion the hub never acknowledged is far less
		// likely to still be sitting in someone's history than a recent one.
		val kept = if (all.size <= TOMBSTONE_MAX) all
		else all.entries.sortedByDescending { it.value }.take(TOMBSTONE_MAX).associate { it.toPair() }
		tombstones = kept
		preferenceManager.deletedSavedQueueIds = kept.entries.joinToString(",") { "${it.key}:${it.value}" }
	}

	/**
	 * Insert or update the snapshot for [id] from the current player [state]. Empty queues are
	 * ignored (a cleared queue keeps its last snapshot in history rather than blanking it).
	 */
	suspend fun upsert(id: String, state: PlayerUiState) {
		val queue = state.queue
		if (queue.isEmpty()) return

		val now = Clock.System.now().toEpochMilliseconds()
		val idx = state.currentIndex.coerceIn(0, queue.lastIndex)
		val currentSong = state.currentSong ?: queue.getOrNull(idx)
		// Rounded, not truncated: the hub carries an integer positionMs, so a truncating round-trip
		// walked the resume point backwards a little on every sync hop.
		val positionMs = (state.progress.coerceIn(0f, 1f) *
			(currentSong?.duration?.inWholeMilliseconds ?: 0L)).roundToLong()

		// Shuffle and repeat belong in the signature: they're persisted on the full-write path only,
		// so leaving them out meant toggling either one was never saved until the queue also changed.
		val sig = signature(queue, state.isShuffleEnabled, state.repeatMode)
		val unchanged = id == cachedId && sig == cachedSig

		if (unchanged) {
			// Same queue, just a moving cursor — no blob rewrite. The cover does NOT move: it is
			// frozen at the queue's birth (PROTOCOL.md §8.3), so the same shared record renders the
			// same art here, in Feishin and on the hub. Only the title tracks the resume point.
			savedQueueDao.updateProgress(
				id = id,
				index = idx,
				songId = currentSong?.id,
				songName = currentSong?.title,
				positionMs = positionMs,
				updatedAt = now
			)
			return
		}

		// New session, or the queue was edited: full write. Identity fields (createdAt, the user's
		// name, the birth-stamped source name and the cover) are preserved, since a rewrite would
		// otherwise clobber them. "Established wins, but a null is not established" — the same rule
		// the hub applies, so a name that only resolves a moment after playback starts still lands.
		val existing = savedQueueDao.getById(id)
		val songIds = queue.map { it.id }
		savedQueueDao.upsert(
			SavedQueueEntity(
				id = id,
				name = existing?.name,
				sourceName = existing?.sourceName
					?: state.savedQueueName
					?: state.currentCollection?.name,
				queueJson = json.encodeToString(queue),
				songIdsCsv = songIds.joinToString(","),
				currentIndex = idx,
				currentSongId = currentSong?.id,
				currentSongName = currentSong?.title,
				// Birth TRACK, not birth cursor: the hub derives a record's art from `songs[0]`
				// (HubManager.applySavedQueues does the same), so taking it from the queue's first
				// entry is what makes one shared record render identically here and in Feishin.
				// It also can't be null — the queue is non-empty by the guard above — whereas
				// `currentSong` is briefly null right as a queue starts, and a record born in that
				// window kept a null cover forever: the cheap progress path never writes the column,
				// so nothing healed it until the queue itself was edited.
				coverArtId = existing?.coverArtId
					?: queue.firstOrNull()?.coverArtId
					?: currentSong?.coverArtId,
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
		indexMutex.withLock {
			index = index.toMutableMap().apply { putEntry(id, songIds, now) }
		}

		cachedId = id
		cachedQueueRef = queue
		cachedSig = sig
	}

	private fun MutableMap<String, IndexEntry>.putEntry(
		id: String,
		ids: List<String>,
		updatedAt: Long
	) {
		if (ids.isEmpty()) remove(id) else put(id, IndexEntry(ids.toSet(), updatedAt))
	}

	private fun invalidateCache() {
		cachedId = null
		cachedQueueRef = null
		cachedSig = ""
	}

	// Same reference-cached signature trick HubManager uses: avoid rebuilding a huge id string when
	// the queue list instance hasn't changed.
	private fun signature(queue: List<DomainSong>, shuffle: Boolean, repeatMode: Int): String {
		val queuePart = if (queue !== cachedQueueRef) {
			queue.joinToString(",") { it.id }
		} else {
			cachedSig.substringBeforeLast('|')
		}
		return "$queuePart|$shuffle,$repeatMode"
	}

	companion object {
		const val MAX_QUEUES = 20

		/** Membership overlap at which two queues count as the same listening session. */
		private const val SAME_QUEUE_THRESHOLD = 0.8

		/** Remembered local deletions, matching hub.py's TOMBSTONE_MAX. */
		private const val TOMBSTONE_MAX = 200

		/** After this long a deletion the hub never acknowledged is forgotten. */
		private const val TOMBSTONE_TTL_MS = 30L * 24 * 60 * 60 * 1000
	}
}
