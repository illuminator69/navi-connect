package paige.navic.ui.screens.savedqueues

import androidx.compose.runtime.Composable
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.count_tracks
import navic.composeapp.generated.resources.queue_from_source
import navic.composeapp.generated.resources.queue_kind_album
import navic.composeapp.generated.resources.queue_kind_journey
import navic.composeapp.generated.resources.queue_kind_manual
import navic.composeapp.generated.resources.queue_kind_mood_flow
import navic.composeapp.generated.resources.queue_kind_playlist
import navic.composeapp.generated.resources.queue_kind_radio
import navic.composeapp.generated.resources.queue_now_playing
import org.jetbrains.compose.resources.pluralStringResource
import org.jetbrains.compose.resources.stringResource
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.SavedQueueSource
import kotlin.time.Duration
import kotlin.time.Duration.Companion.milliseconds

/**
 * Display rules for saved queues, shared by the Saved Queues screen, the home "Continue listening"
 * row and the preview sheet — the Kotlin twin of Feishin's `saved-queue-format.ts`. Kept in one place
 * and in step with that file, because the two clients render one shared history: a record must read
 * the same wherever it appears.
 */

/** Human label for a [SavedQueueSource] kind. Unknown/newer kinds fall back to the raw value. */
@Composable
fun queueKindLabel(kind: String): String = when (kind) {
	SavedQueueSource.MANUAL -> stringResource(Res.string.queue_kind_manual)
	SavedQueueSource.ALBUM -> stringResource(Res.string.queue_kind_album)
	SavedQueueSource.PLAYLIST -> stringResource(Res.string.queue_kind_playlist)
	SavedQueueSource.RADIO -> stringResource(Res.string.queue_kind_radio)
	SavedQueueSource.MOOD_FLOW -> stringResource(Res.string.queue_kind_mood_flow)
	SavedQueueSource.JOURNEY -> stringResource(Res.string.queue_kind_journey)
	else -> kind
}

/**
 * Names of the shape "Queue · 12 tracks" that older builds SYNTHESIZED and then stored as a real
 * source name. They go stale as soon as the queue is edited and they duplicate the subtitle word for
 * word. Nothing writes them any more; this hides the ones already in the history (including the ones
 * that arrived from Feishin before it stopped writing them).
 */
private val SYNTHESIZED_NAME =
	Regex("""^(Album|Journey|Manual|Mood Flow|Playlist|Queue|Radio) · \d+ (songs?|tracks?)$""")

fun realSourceName(queue: SavedQueueEntity): String? =
	queue.sourceName?.trim()?.takeIf { it.isNotEmpty() && !SYNTHESIZED_NAME.matches(it) }

/**
 * What to call a saved queue: the user's own name if they set one, else **the track that will
 * actually play when it's restored**.
 *
 * That last part is deliberate. Titling by origin was stable but unhelpful — half the history read
 * "Manual · 29 songs" or the name of an album long since played past, and the rows gave no clue what
 * tapping them would do. The origin is still shown, on the third line.
 */
@Composable
fun savedQueueTitle(queue: SavedQueueEntity): String =
	queue.name?.takeIf { it.isNotBlank() }
		?: queue.currentSongName?.takeIf { it.isNotBlank() }
		?: realSourceName(queue)
		?: "${queueKindLabel(queue.sourceKind)} · ${trackCountLabel(queue.songCount)}"

/** Second line: what kind of queue this is and how big, prefixed with the live marker. */
@Composable
fun savedQueueSubtitle(queue: SavedQueueEntity, isActive: Boolean): String = listOfNotNull(
	stringResource(Res.string.queue_now_playing).takeIf { isActive },
	queueKindLabel(queue.sourceKind),
	trackCountLabel(queue.songCount)
).joinToString(" · ")

/** Third line: where the queue came from, when that isn't already the title. */
@Composable
fun savedQueueSourceLine(queue: SavedQueueEntity): String? {
	val source = realSourceName(queue) ?: return null
	if (source == savedQueueTitle(queue)) return null
	return stringResource(Res.string.queue_from_source, source)
}

/**
 * The song whose artwork represents this queue: the one it will resume on, matching the title.
 * Rendered by id with this client's own credentials — a peer's cover URL points at ITS server with
 * ITS auth, which is why cross-client cards showed a broken-image placeholder.
 */
fun savedQueueCoverArtId(queue: SavedQueueEntity): String? =
	queue.coverArtId ?: queue.currentSongId

@Composable
fun trackCountLabel(count: Int): String = pluralStringResource(Res.plurals.count_tracks, count, count)

/** Total runtime of a decoded queue, `h:mm:ss` or `m:ss`. */
fun savedQueueDuration(songs: List<DomainSong>): String =
	formatDuration(songs.fold(Duration.ZERO) { acc, song -> acc + song.duration })

/** A single track's runtime, `m:ss` (or `h:mm:ss` for the occasional long mix/DJ set). */
fun trackDuration(song: DomainSong): String = formatDuration(song.duration)

fun formatDuration(duration: Duration): String {
	val total = duration.inWholeSeconds.coerceAtLeast(0)
	val hours = total / 3600
	val minutes = (total % 3600) / 60
	val seconds = total % 60
	return if (hours > 0) {
		"$hours:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}"
	} else {
		"$minutes:${seconds.toString().padStart(2, '0')}"
	}
}

fun formatPosition(positionMs: Long): String = formatDuration(positionMs.milliseconds)
