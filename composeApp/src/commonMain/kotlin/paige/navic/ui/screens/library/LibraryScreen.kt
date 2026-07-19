package paige.navic.ui.screens.library

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.title_library
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinViewModel
import org.koin.core.parameter.parametersOf
import paige.navic.LocalBottomBarScrollManager
import paige.navic.domain.models.DomainAlbum
import paige.navic.domain.models.DomainAlbumListType
import paige.navic.domain.models.DomainArtistListType
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.DomainSongCollection
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.ErrorSnackbar
import paige.navic.ui.components.dialogs.DeletionDialog
import paige.navic.ui.components.dialogs.DeletionEndpoint
import paige.navic.ui.components.layouts.PullToRefreshBox
import paige.navic.ui.components.layouts.RootBottomBar
import paige.navic.ui.components.layouts.RootTopBar
import paige.navic.ui.screens.album.viewmodels.AlbumListViewModel
import paige.navic.ui.screens.artist.viewmodels.ArtistListViewModel
import paige.navic.ui.screens.genre.viewmodels.GenreListViewModel
import paige.navic.ui.screens.library.components.LibraryScreenContent
import paige.navic.ui.screens.login.viewmodels.LoginViewModel
import paige.navic.ui.screens.playlist.dialogs.PlaylistCreateDialog
import paige.navic.ui.screens.playlist.viewmodels.PlaylistListViewModel
import paige.navic.ui.screens.share.dialogs.ShareDialog
import paige.navic.ui.theme.NavicTheme
import paige.navic.util.ui.LibraryHeroAmbient
import paige.navic.util.ui.rememberAppIsDark
import paige.navic.util.ui.rememberCoverColorScheme
import paige.navic.util.ui.rememberLibraryTabBackground
import paige.navic.util.ui.rememberNowPlayingCoverArtId
import kotlinx.collections.immutable.ImmutableList
import kotlinx.collections.immutable.persistentListOf
import paige.navic.domain.repositories.AlbumRepository
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.ui.core.LoginUiState
import paige.navic.ui.core.UiState
import kotlin.time.Duration

@OptIn(ExperimentalMaterial3ExpressiveApi::class, ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen() {
	val albumsViewModel = koinViewModel<AlbumListViewModel>(
		key = "libraryAlbums",
		parameters = { parametersOf(DomainAlbumListType.Recent) }
	)
	val albumsState by albumsViewModel.albumsState.collectAsStateWithLifecycle()

	// Lightweight top-20 loads (full AlbumListViewModels here would read the
	// ENTIRE library with songs three times in parallel — jank + crashes).
	val albumRepository = koinInject<AlbumRepository>()
	var frequentAlbumsState by remember {
		mutableStateOf<UiState<ImmutableList<DomainAlbum>>>(UiState.Loading(persistentListOf()))
	}
	var newestAlbumsState by remember {
		mutableStateOf<UiState<ImmutableList<DomainAlbum>>>(UiState.Loading(persistentListOf()))
	}
	val selectedAlbum by albumsViewModel.selectedAlbum.collectAsStateWithLifecycle()
	val selectedAlbumIsStarred by albumsViewModel.starred.collectAsStateWithLifecycle()
	val selectedAlbumRating by albumsViewModel.rating.collectAsStateWithLifecycle()

	val playlistsViewModel = koinViewModel<PlaylistListViewModel>()
	val playlistsState by playlistsViewModel.playlistsState.collectAsStateWithLifecycle()
	val selectedPlaylist by playlistsViewModel.selectedPlaylist.collectAsStateWithLifecycle()

	val artistsViewModel = koinViewModel<ArtistListViewModel>(
		key = "libraryArtists",
		parameters = { parametersOf(DomainArtistListType.AlphabeticalByName) }
	)
	val artistsState by artistsViewModel.artistsState.collectAsStateWithLifecycle()
	val selectedArtist by artistsViewModel.selectedArtist.collectAsStateWithLifecycle()
	val selectedArtistAlbums by artistsViewModel.selectedArtistAlbums.collectAsStateWithLifecycle()
	val selectedArtistIsStarred by artistsViewModel.starred.collectAsStateWithLifecycle()

	val genresViewModel = koinViewModel<GenreListViewModel>()
	val genresState by genresViewModel.genresState.collectAsStateWithLifecycle()

	val loginViewModel = koinViewModel<LoginViewModel>()
	val loginState by loginViewModel.loginState.collectAsStateWithLifecycle()

	var shareId by rememberSaveable { mutableStateOf<String?>(null) }
	var shareExpiry by remember { mutableStateOf<Duration?>(null) }
	var playlistDeletionId by rememberSaveable { mutableStateOf<String?>(null) }
	var playlistCreateDialogShown by rememberSaveable { mutableStateOf(false) }

	val player = koinInject<MediaPlayerViewModel>()
	val preferenceManager = koinInject<PreferenceManager>()

	// Continue listening: the most-recent saved queues, minus the one already playing (no point
	// offering to resume what's live). Capped so the home row stays short.
	val savedQueueRepository = koinInject<SavedQueueRepository>()
	val allSavedQueues by savedQueueRepository.observeAll()
		.collectAsStateWithLifecycle(initialValue = emptyList())
	val playerLocalState by player.localUiState.collectAsStateWithLifecycle()
	val continueListening = remember(allSavedQueues, playerLocalState.savedQueueId) {
		allSavedQueues
			.filter { it.id != playerLocalState.savedQueueId }
			.take(10)
	}

	// Stale-library signal: when a full sync last failed at the top level (server unreachable), tell
	// the user the grid below is the cached copy instead of implying it's empty/broken.
	val syncManager = koinInject<paige.navic.domain.manager.SyncManager>()
	val syncState by syncManager.syncState.collectAsStateWithLifecycle()

	val scrollBehavior = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()

	// Theme the whole home to the NOW-PLAYING song so the chrome (buttons, section labels,
	// nav chip, top bar) carries its hue — not just the hero wash. Keep the app's light/dark
	// brightness (followArtworkBrightness = false) so the page doesn't flip per cover; when
	// nothing is playing, fall back to the default app scheme.
	val appIsDark = rememberAppIsDark()
	val nowPlayingCoverId = rememberNowPlayingCoverArtId()
	val nowPlayingCover = rememberCoverColorScheme(
		nowPlayingCoverId,
		isDark = appIsDark,
		followArtworkBrightness = false
	)
	val libraryScheme = if (nowPlayingCoverId != null) nowPlayingCover.scheme
		else MaterialTheme.colorScheme

	LaunchedEffect(loginState is LoginUiState.Success) {
		albumsViewModel.refreshAlbums(false)
		runCatching {
			frequentAlbumsState =
				UiState.Success(albumRepository.getAlbumsLimited(DomainAlbumListType.Frequent, 20))
			newestAlbumsState =
				UiState.Success(albumRepository.getAlbumsLimited(DomainAlbumListType.Newest, 20))
		}
		playlistsViewModel.refreshPlaylists(false)
		artistsViewModel.refreshArtists(false)
		genresViewModel.refreshGenres(false)
	}

	NavicTheme(libraryScheme) {
	// Surface the grids fade back to under the wash — now cover-tinted at app brightness.
	val homeBackground = rememberLibraryTabBackground()
	Scaffold(
		topBar = { RootTopBar({ Text(stringResource(Res.string.title_library)) }, scrollBehavior) },
		bottomBar = {
			val scrollManager = LocalBottomBarScrollManager.current
			RootBottomBar(scrolled = scrollManager.isTriggered)
		}
	) { innerPadding ->
		Box(
			modifier = Modifier
				.fillMaxSize()
				.background(homeBackground)
		) {
		// Fixed-size and behind the scrolling grid — never a grid item, so scrolling
		// never re-measures the blur (the reason the old tinted home background was ripped out).
		// Starts at y=0 (behind the status bar + transparent top bar) so the now-playing wash
		// shows THROUGH the bar instead of butting against a solid slab; the cap is grown by the
		// top inset so the bottom fade still lands in the same place (below the greeting/buttons).
		LibraryHeroAmbient(
			heightCap = 300.dp + innerPadding.calculateTopPadding(),
			modifier = Modifier.align(Alignment.TopCenter)
		)
		PullToRefreshBox(
			modifier = Modifier
				.padding(top = innerPadding.calculateTopPadding()),
			finished = albumsState !is UiState.Loading &&
				playlistsState !is UiState.Loading &&
				artistsState !is UiState.Loading &&
				genresState !is UiState.Loading,
			onRefresh = {
				albumsViewModel.refreshAlbums(true)
				playlistsViewModel.refreshPlaylists(true)
				artistsViewModel.refreshArtists(true)
				genresViewModel.refreshGenres(true)
			},
			key = listOf(albumsState, playlistsState, artistsState, genresState)
		) {
			LibraryScreenContent(
				scrollBehavior = scrollBehavior,
				innerPadding = innerPadding,
				onSetShareId = { shareId = it },

				continueListening = continueListening,
				onResumeQueue = { player.swapToSavedQueue(it, play = true) },
				syncFailed = syncState.lastSyncFailed && !syncState.isSyncing,

				albumsState = albumsState,
				frequentAlbumsState = frequentAlbumsState,
				newestAlbumsState = newestAlbumsState,
				selectedAlbum = selectedAlbum,
				selectedAlbumIsStarred = selectedAlbumIsStarred,
				selectedAlbumRating = selectedAlbumRating,
				onSelectAlbum = { albumsViewModel.selectAlbum(it) },
				onClearAlbumSelection = { albumsViewModel.clearSelection() },
				onStarSelectedAlbum = { albumsViewModel.starAlbum(it) },
				onPlayAlbumNext = { if (selectedAlbum != null) player.playNext(selectedAlbum as DomainSongCollection)},
				onAddAlbumToQueue = { if (selectedAlbum != null) player.addToQueue(selectedAlbum as DomainSongCollection)},
				onRateSelectedAlbum = { albumsViewModel.setRating(it) },

				artistsState = artistsState,
				selectedArtist = selectedArtist,
				selectedArtistAlbums = selectedArtistAlbums,
				selectedArtistIsStarred = selectedArtistIsStarred,
				onSelectArtist = { artistsViewModel.selectArtist(it) },
				onClearArtistSelection = { artistsViewModel.clearSelection() },
				onStarSelectedArtist = { artistsViewModel.starArtist(it) },
				onPlayArtistNext = { if (selectedArtist != null) artistsViewModel.playArtistAlbumsNext(player)},
				onAddArtistToQueue = { if (selectedArtist != null) artistsViewModel.addArtistAlbumsToQueue(player)},

				playlistsState = playlistsState,
				selectedPlaylist = selectedPlaylist,
				onSelectPlaylist = { playlistsViewModel.selectPlaylist(it) },
				onClearPlaylistSelection = { playlistsViewModel.clearSelection() },
				onDeletePlaylist = { playlistDeletionId = it },
				onPlayPlaylistNext = { if (selectedPlaylist != null) player.playNext(selectedPlaylist as DomainSongCollection)},
				onAddPlaylistToQueue = { if (selectedPlaylist != null) player.addToQueue(selectedPlaylist as DomainSongCollection)},

				genresState = genresState
			)
		}
		}
	}
	}

	val flattenedErrors = listOf(
		(albumsState as? UiState.Error)?.error,
		(playlistsState as? UiState.Error)?.error,
		(artistsState as? UiState.Error)?.error,
		(genresState as? UiState.Error)?.error
	).mapNotNull { it?.stackTraceToString() }.takeIf { it.isNotEmpty() }?.joinToString("\n\n")

	ErrorSnackbar(
		error = flattenedErrors?.let { Error(it) },
		onClearError = {
			albumsViewModel.clearError()
			playlistsViewModel.clearError()
			artistsViewModel.clearError()
			genresViewModel.clearError()
		}
	)

    ShareDialog(
        id = shareId,
        onIdClear = { shareId = null },
        expiry = shareExpiry,
        onExpiryChange = { shareExpiry = it }
    )

    DeletionDialog(
        endpoint = DeletionEndpoint.PLAYLIST,
        id = playlistDeletionId,
        onIdClear = { playlistDeletionId = null },
        onRefresh = { playlistsViewModel.refreshPlaylists(false) }
    )

	if (playlistCreateDialogShown) {
        PlaylistCreateDialog(
            onDismissRequest = { playlistCreateDialogShown = false },
            onRefresh = { playlistsViewModel.refreshPlaylists(true) }
        )
	}
}
