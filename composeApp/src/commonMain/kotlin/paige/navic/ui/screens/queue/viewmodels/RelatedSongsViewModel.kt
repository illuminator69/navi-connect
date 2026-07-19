package paige.navic.ui.screens.queue.viewmodels

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.collections.immutable.ImmutableList
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toImmutableList
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.models.DomainSong
import paige.navic.domain.repositories.SongRepository
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.core.UiState

/**
 * Backs the queue sheet's "Related" tab. The song source is [RadioManager.fetchQueueRelatedSongs]
 * — recommendations derived from the WHOLE queue that aren't already queued — refetched whenever the
 * queue's composition changes. The selection / star / rating / download plumbing mirrors
 * [paige.navic.ui.screens.song.viewmodels.SongListViewModel] so the related rows reuse the same
 * interactive [paige.navic.ui.screens.song.components.songListScreenContent] card as other lists.
 */
class RelatedSongsViewModel(
	private val repository: SongRepository,
	private val downloadManager: DownloadManager,
	private val radioManager: RadioManager,
	private val mediaPlayer: MediaPlayerViewModel,
	connectivityManager: ConnectivityManager
) : ViewModel() {
	private val _songsState =
		MutableStateFlow<UiState<ImmutableList<DomainSong>>>(UiState.Loading())
	val songsState = _songsState.asStateFlow()

	val allDownloads = downloadManager.allDownloads
		.stateIn(
			scope = viewModelScope,
			started = SharingStarted.Lazily,
			initialValue = persistentListOf()
		)

	private val _selectedSong = MutableStateFlow<DomainSong?>(null)
	val selectedSong = _selectedSong.asStateFlow()

	private val _starred = MutableStateFlow(false)
	val starred = _starred.asStateFlow()

	private val _selectedSongRating = MutableStateFlow(0)
	val selectedSongRating = _selectedSongRating.asStateFlow()

	val isOnline = connectivityManager.isOnline

	@OptIn(FlowPreview::class)
	private val queueIds = mediaPlayer.uiState
		.map { state -> state.queue.map { it.id } }
		// The queue list identity is stable across the ~5 Hz progress emissions, but compare by
		// ids so a genuine add/remove/reorder (or autoplay top-up) still triggers a refetch.
		.distinctUntilChanged()
		// Coalesce autoplay bursts (several appends in quick succession) into one fetch.
		.debounce(400)

	init {
		viewModelScope.launch {
			// collectLatest cancels an in-flight fetch when the queue changes again.
			queueIds.collectLatest {
				val queue = mediaPlayer.uiState.value.queue
				_songsState.value = UiState.Loading()
				val related = radioManager.fetchQueueRelatedSongs(queue)
				_songsState.value = UiState.Success(related.toImmutableList())
			}
		}
	}

	fun selectSong(song: DomainSong) {
		viewModelScope.launch {
			_selectedSong.value = song
			_starred.value = repository.isSongStarred(song)
			_selectedSongRating.value = repository.getSongRating(song)
		}
	}

	fun clearSelection() {
		_selectedSong.value = null
	}

	fun starSong(starred: Boolean) {
		viewModelScope.launch {
			val selection = _selectedSong.value ?: return@launch
			runCatching {
				if (starred) repository.starSong(selection) else repository.unstarSong(selection)
				_starred.value = starred
			}
		}
	}

	fun rateSelectedSong(rating: Int) {
		viewModelScope.launch {
			val selection = _selectedSong.value ?: return@launch
			runCatching {
				repository.rateSong(selection, rating)
				_selectedSongRating.value = rating
			}
		}
	}

	fun downloadSong(song: DomainSong) {
		downloadManager.downloadSong(song)
	}

	fun cancelDownload(songId: String) {
		downloadManager.cancelDownload(songId)
	}

	fun deleteDownload(songId: String) {
		downloadManager.deleteDownload(songId)
	}
}
