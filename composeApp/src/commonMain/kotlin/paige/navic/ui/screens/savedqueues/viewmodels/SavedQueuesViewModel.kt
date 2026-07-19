package paige.navic.ui.screens.savedqueues.viewmodels

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import paige.navic.data.database.dao.PlaylistDao
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.data.database.mappers.toEntity
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.util.core.Logger

/**
 * Backs the "Saved queues" list: the rolling history from [SavedQueueRepository] plus rename/delete
 * and "save as Navidrome playlist" actions. Swapping into a queue (restore / resume) is driven by
 * the shared player VM (read in the screen), not here — this VM owns the list and its edits.
 */
class SavedQueuesViewModel(
	private val savedQueueRepository: SavedQueueRepository,
	private val sessionManager: SessionManager,
	private val playlistDao: PlaylistDao
) : ViewModel() {

	val queues = savedQueueRepository.observeAll()
		.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList<SavedQueueEntity>())

	/** One-shot user feedback (e.g. "Saved as playlist") the screen shows then clears. */
	private val _message = MutableStateFlow<SavedQueueMessage?>(null)
	val message = _message.asStateFlow()

	fun clearMessage() { _message.value = null }

	fun rename(id: String, name: String?) {
		viewModelScope.launch { savedQueueRepository.rename(id, name) }
	}

	fun delete(id: String) {
		viewModelScope.launch { savedQueueRepository.delete(id) }
	}

	fun deleteOthers(keepId: String) {
		viewModelScope.launch { savedQueueRepository.deleteOthers(keepId) }
	}

	/**
	 * Export a saved queue to a real Navidrome playlist via the native API, then cache the created
	 * playlist locally so it appears without waiting for the next sync. Mirrors
	 * `PlaylistCreateDialogViewModel`. Reports success/failure through [message].
	 */
	fun saveAsPlaylist(id: String, name: String) {
		viewModelScope.launch {
			try {
				val entity = savedQueueRepository.get(id) ?: return@launch
				val songIds = savedQueueRepository.decodeQueue(entity).map { it.id }
				if (songIds.isEmpty()) {
					_message.value = SavedQueueMessage.Error
					return@launch
				}
				val playlist = sessionManager.api.createPlaylist(
					name = name.trim().ifEmpty { entity.name ?: entity.sourceName ?: "Queue" },
					songIds = songIds
				)
				playlistDao.insertPlaylist(playlist.toEntity())
				_message.value = SavedQueueMessage.SavedAsPlaylist
			} catch (e: Exception) {
				Logger.e("SavedQueuesViewModel", "save as playlist failed", e)
				_message.value = SavedQueueMessage.Error
			}
		}
	}
}

enum class SavedQueueMessage { SavedAsPlaylist, Error }
