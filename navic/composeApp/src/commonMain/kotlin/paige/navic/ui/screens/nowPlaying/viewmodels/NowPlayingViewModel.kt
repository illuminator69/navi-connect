package paige.navic.ui.screens.nowPlaying.viewmodels

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChangedBy
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import org.koin.core.component.KoinComponent
import paige.navic.domain.repositories.SongRepository
import paige.navic.shared.MediaPlayerViewModel

class NowPlayingViewModel(
	private val player: MediaPlayerViewModel,
	private val songRepository: SongRepository
) : ViewModel(), KoinComponent {

	private val _songIsStarred = MutableStateFlow(false)
	val songIsStarred = _songIsStarred.asStateFlow()

	private val _songRating = MutableStateFlow(0)
	val songRating = _songRating.asStateFlow()

	init {
		viewModelScope.launch {
			// Distinct on the song, NOT the whole state: `uiState` re-emits every 200 ms with a
			// new playhead, and this used to fire two Room queries on each of those — twice a
			// second's worth of database work forever, to answer a question that can only change
			// when the track does.
			player.uiState
				.map { it.currentSong }
				.distinctUntilChangedBy { it?.id }
				.collect { song ->
					song?.let {
						_songIsStarred.value = songRepository.isSongStarred(it)
						_songRating.value = songRepository.getSongRating(it)
					}
				}
		}
	}

	fun starSong(starred: Boolean) {
		viewModelScope.launch {
			runCatching {
				player.uiState.value.currentSong?.let { song ->
					_songIsStarred.value = starred
					if (starred) {
						songRepository.starSong(song)
					} else {
						songRepository.unstarSong(song)
					}
				}
			}
		}
	}

	fun rateSong(rating: Int) {
		viewModelScope.launch {
			runCatching {
				player.uiState.value.currentSong?.let { song ->
					_songRating.value = rating
					songRepository.rateSong(song, rating)
				}
			}
		}
	}
}
