package paige.navic.data.database.entities

import androidx.room3.Entity
import androidx.room3.PrimaryKey
import kotlinx.serialization.Serializable

/**
 * One automatically-captured play-queue snapshot — the backing store for the Symfonium-style
 * "saved queues" list. A new row is minted whenever a fresh queue starts playing; edits to the
 * current queue update its existing row in place. The list is a rolling cache capped at ~20 rows
 * (oldest by [updatedAt] evicted), so this is deliberately cheap to write.
 *
 * The full [queueJson] blob (an encoded `List<DomainSong>`), not just song ids, is stored: the app
 * already persists the live queue the same way, and ids alone can't faithfully restore synthetic
 * radio songs or offline metadata.
 */
@Serializable
@Entity
data class SavedQueueEntity(
	@PrimaryKey val id: String,
	/** User-assigned name; null means fall back to [sourceName], then "No name". */
	val name: String? = null,
	/** Auto-derived at save time from the source collection (album/playlist), if any. */
	val sourceName: String? = null,
	/** `Json.encodeToString(List<DomainSong>)` of the whole queue. */
	val queueJson: String,
	/**
	 * The queue's song ids, comma-joined. Redundant with [queueJson], but it lets the repository keep
	 * an in-memory membership index for "is this the queue I already have a record for?" without
	 * decoding twenty blobs. Null on rows written before this column existed; backfilled on first use.
	 */
	val songIdsCsv: String? = null,
	val currentIndex: Int = 0,
	val currentSongId: String? = null,
	/** Title of the current track, cached so the list's third line needn't decode the blob. */
	val currentSongName: String? = null,
	/**
	 * Cover art for the card, stamped at the queue's BIRTH and never rewritten — part of the record's
	 * identity, like `sourceName`. That's the hub's rule (PROTOCOL.md §8.3) and Feishin's; letting it
	 * follow the resume cursor here meant one shared record showed different art on each client.
	 *
	 * Rendered by id with this client's own credentials — a peer's cover URL points at its own server
	 * with its own auth and won't load here.
	 */
	val coverArtId: String? = null,
	/** Playback position of the current track, so a swap resumes where it left off. */
	val positionMs: Long = 0L,
	val shuffle: Boolean = false,
	val repeatMode: Int = 0,
	/** Cached so the row subtitle needn't decode [queueJson]. */
	val songCount: Int = 0,
	/**
	 * How this queue was created — one of [paige.navic.domain.models.SavedQueueSource]. Lets the
	 * saved-queues list group generated sessions (radio / Mood Flow / journey) apart from album /
	 * playlist / manual queues. Defaults to `manual` so pre-migration rows read sensibly.
	 */
	val sourceKind: String = "manual",
	val createdAt: Long = 0L,
	val updatedAt: Long = 0L
)
