package paige.navic.ui.core

import kotlinx.serialization.Serializable
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongCollection

@Serializable
data class PlayerUiState(
	val queue: List<DomainSong> = emptyList(),
	val currentSong: DomainSong? = null,
	val currentCollection: DomainSongCollection? = null,
	val currentIndex: Int = -1,
	val isPaused: Boolean = false,
	val isShuffleEnabled: Boolean = false,
	val repeatMode: Int = 0,
	val progress: Float = 0f,
	val isLoading: Boolean = false,
	val playbackSpeed: Float = 1.0f,
	val playbackBitrate: Int? = null,
	val playbackSampleRate: Int? = null,
	val playbackMimeType: String? = null,
	/**
	 * Identifies which auto-saved queue this state belongs to (see SavedQueueRepository). Minted
	 * fresh when a new queue starts, preserved across edits to the same queue, null when the queue
	 * is empty. Carried in the persisted blob so the active session survives an app restart.
	 */
	val savedQueueId: String? = null,
	/**
	 * How the current queue was created — one of [paige.navic.domain.models.SavedQueueSource].
	 * Stamped at the same moment as [savedQueueId] (queue-replace), preserved across edits, and
	 * written onto the saved-queue history row so generated sessions can be grouped. Persisted with
	 * the blob so it survives restart.
	 */
	val savedQueueKind: String = "manual",
	/**
	 * What this queue should be CALLED in the saved-queue history, announced by whoever replaced the
	 * queue. [currentCollection] can't answer this on its own: generated mixes (radio / Mood Flow /
	 * journey) have no collection at all, and for the ones that do it resolves asynchronously from the
	 * playing song, so a title read from it drifted mid-session. Stamped at birth alongside
	 * [savedQueueId], persisted with the blob.
	 */
	val savedQueueName: String? = null
)
