package paige.navic.ui.screens.savedqueues

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import org.koin.compose.koinInject
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.manager.HubManager
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.screens.savedqueues.viewmodels.SavedQueuesViewModel

/**
 * The saved-queue operations shared by the Saved Queues screen and the home "Continue listening"
 * row, so the two surfaces can't drift on the parts that are easy to get subtly wrong: routing edits
 * through the hub, and re-starting the listening session when the record being played is destroyed.
 */
class SavedQueueActions(
	/** Id of the record that is playing right now (on ANY device), or null. */
	val activeId: String?,
	/** Restore paused, at the saved playhead. */
	val restore: (SavedQueueEntity) -> Unit,
	/** Restore and start playing. */
	val resume: (SavedQueueEntity) -> Unit,
	val rename: (String, String?) -> Unit,
	val delete: (SavedQueueEntity) -> Unit,
	val deleteOthers: (List<SavedQueueEntity>, String) -> Unit,
	val clearAll: (List<SavedQueueEntity>) -> Unit
)

@Composable
fun rememberSavedQueueActions(
	viewModel: SavedQueuesViewModel,
	mediaPlayer: MediaPlayerViewModel
): SavedQueueActions {
	val hubManager = koinInject<HubManager>()
	val hubConnected by hubManager.connected.collectAsState()
	val remoteSession by hubManager.remoteSession.collectAsState()
	val playerState by mediaPlayer.localUiState.collectAsState()

	// The "current" queue is the hub session's when connected (any device), else the local one.
	// Gate on `hubConnected`: after the socket drops the mirror still holds the last session,
	// and using it then highlighted a queue nothing was playing.
	val activeId = (if (hubConnected) remoteSession.savedQueueId else null)
		?: playerState.savedQueueId

	return remember(activeId, hubConnected) {
		// Every edit is applied locally AND announced to the hub, which propagates it to every client
		// and echoes the merged list back. Local-first throughout: the edit is instant, and the local
		// delete records a tombstone that the next reconnect pushes up — so a deletion made while
		// offline survives instead of being undone by the hub's reply.
		fun deleteOne(entry: SavedQueueEntity) {
			val wasLive = entry.id == activeId
			viewModel.delete(entry.id)
			if (hubConnected) hubManager.actDeleteSavedQueue(entry.id)
			// Deleting the record the live queue writes into leaves the session pointing at nothing,
			// and the publish dedupe means nothing re-mints — so what you're listening to would have
			// no card until you played something else.
			if (wasLive) mediaPlayer.restartQueueSession()
		}

		SavedQueueActions(
			activeId = activeId,
			restore = { mediaPlayer.swapToSavedQueue(it.id, play = false) },
			resume = { mediaPlayer.swapToSavedQueue(it.id, play = true) },
			rename = { id, name ->
				viewModel.rename(id, name)
				if (hubConnected) hubManager.actRenameSavedQueue(id, name)
			},
			delete = ::deleteOne,
			deleteOthers = { all, keepId ->
				val removed = all.map { it.id }.filter { it != keepId }
				viewModel.deleteOthers(keepId, removed)
				if (hubConnected) hubManager.actDeleteSavedQueues(removed)
			},
			clearAll = { all ->
				// A local-only clear was pointless: the hub kept every record and rebroadcast the lot
				// on the next connect, so the whole list came back. Tombstone each one instead — one
				// act rather than one per row, so the hub answers with a single broadcast.
				val removed = all.map { it.id }
				viewModel.clearAll(removed)
				if (hubConnected) hubManager.actDeleteSavedQueues(removed)
				if (activeId != null) mediaPlayer.restartQueueSession()
			}
		)
	}
}
