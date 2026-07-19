package paige.navic.domain.models

/**
 * How a saved queue came to be — stored on `SavedQueueEntity.sourceKind` so the saved-queues list
 * can group/filter generated sessions (radio, Mood Flow, Journey) apart from ordinary album,
 * playlist, and manual queues. String-valued (not an enum column) to match the additive migration
 * pattern and stay forward-compatible with unknown kinds from newer builds.
 */
object SavedQueueSource {
	const val MANUAL = "manual"
	const val ALBUM = "album"
	const val PLAYLIST = "playlist"
	const val RADIO = "radio"
	const val MOOD_FLOW = "moodFlow"
	const val JOURNEY = "journey"

	val ALL = listOf(MANUAL, ALBUM, PLAYLIST, RADIO, MOOD_FLOW, JOURNEY)
}

/** The kind an ordinary queue-replace (tap a song / shuffle) implies from its source collection. */
fun DomainSongCollection.toSavedQueueKind(): String = when (this) {
	is DomainAlbum -> SavedQueueSource.ALBUM
	is DomainPlaylist -> SavedQueueSource.PLAYLIST
	else -> SavedQueueSource.MANUAL
}
