package paige.navic.ui.screens.library.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.plus
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarScrollBehavior
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.collections.immutable.ImmutableList
import kotlin.time.Clock
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime
import org.jetbrains.compose.resources.StringResource
import org.jetbrains.compose.resources.stringResource
import paige.navic.util.ui.rememberNowPlayingCoverAmbient
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.banner_sync_failed_cached
import navic.composeapp.generated.resources.greeting_afternoon
import navic.composeapp.generated.resources.greeting_evening
import navic.composeapp.generated.resources.greeting_morning
import navic.composeapp.generated.resources.option_sort_frequent
import navic.composeapp.generated.resources.option_sort_newest
import navic.composeapp.generated.resources.option_sort_random
import navic.composeapp.generated.resources.option_sort_recent
import navic.composeapp.generated.resources.option_sort_starred
import navic.composeapp.generated.resources.title_artists
import navic.composeapp.generated.resources.title_continue_listening
import navic.composeapp.generated.resources.title_genres
import navic.composeapp.generated.resources.title_playlists
import paige.navic.ui.navigation.Screen
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.DomainAlbum
import paige.navic.domain.models.DomainAlbumListType
import paige.navic.domain.models.DomainArtist
import paige.navic.domain.models.DomainGenre
import paige.navic.domain.models.DomainPlaylist
import paige.navic.icons.Icons
import paige.navic.icons.outlined.History
import paige.navic.icons.outlined.LibraryAdd
import paige.navic.icons.outlined.Shuffle
import paige.navic.icons.outlined.Star
import paige.navic.ui.components.layouts.horizontalSection
import paige.navic.ui.screens.album.components.AlbumListScreenItem
import paige.navic.ui.screens.artist.ArtistsScreenItem
import paige.navic.ui.screens.genre.components.GenreListScreenCard
import paige.navic.ui.screens.playlist.components.PlaylistListScreenItem
import paige.navic.ui.core.UiState
import paige.navic.util.ui.withoutTop

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreenContent(
	scrollBehavior: TopAppBarScrollBehavior,
	innerPadding: PaddingValues,
	onSetShareId: (String) -> Unit,

	// continue listening (recent saved queues, most-recent first)
	continueListening: List<SavedQueueEntity>,
	onResumeQueue: (String) -> Unit,
	// true when the last full sync failed and we're showing the cached library
	syncFailed: Boolean,

	// albums
	albumsState: UiState<ImmutableList<DomainAlbum>>,
	frequentAlbumsState: UiState<ImmutableList<DomainAlbum>>,
	newestAlbumsState: UiState<ImmutableList<DomainAlbum>>,
	selectedAlbum: DomainAlbum?,
	selectedAlbumIsStarred: Boolean,
	selectedAlbumRating: Int,
	onSelectAlbum: (DomainAlbum) -> Unit,
	onClearAlbumSelection: () -> Unit,
	onStarSelectedAlbum: (Boolean) -> Unit,
	onRateSelectedAlbum: (Int) -> Unit,
	onPlayAlbumNext: () -> Unit,
	onAddAlbumToQueue: () -> Unit,

	// artists
	artistsState: UiState<ImmutableList<DomainArtist>>,
	selectedArtist: DomainArtist?,
	selectedArtistAlbums: ImmutableList<DomainAlbum>?,
	selectedArtistIsStarred: Boolean,
	onSelectArtist: (DomainArtist) -> Unit,
	onClearArtistSelection: () -> Unit,
	onStarSelectedArtist: (Boolean) -> Unit,
	onPlayArtistNext: () -> Unit,
	onAddArtistToQueue: () -> Unit,

	// playlists
	playlistsState: UiState<ImmutableList<DomainPlaylist>>,
	selectedPlaylist: DomainPlaylist?,
	onSelectPlaylist: (DomainPlaylist) -> Unit,
	onClearPlaylistSelection: () -> Unit,
	onDeletePlaylist: (String) -> Unit,
	onPlayPlaylistNext: () -> Unit,
	onAddPlaylistToQueue: () -> Unit,

	// genres
	genresState: UiState<ImmutableList<DomainGenre>>
) {
	// Resolved OUTSIDE the grid, and deliberately so: read inside the greeting's `item`, this state
	// was destroyed and rebuilt every time the greeting scrolled out of view and back, so it
	// restarted at the neutral default and faded to the cover colour again — the greeting "popped"
	// a different colour on each pass. The grid gets disposed items; this composable does not.
	val ambient = rememberNowPlayingCoverAmbient()

	LazyVerticalGrid(
		modifier = Modifier.nestedScroll(scrollBehavior.nestedScrollConnection),
		columns = GridCells.Fixed(2),
		contentPadding = innerPadding.withoutTop() + PaddingValues(top = 8.dp),
		verticalArrangement = Arrangement.spacedBy(5.dp),
		horizontalArrangement = Arrangement.spacedBy(5.dp),
	) {
		// Stale-library banner: only when a sync actually failed, so the happy path is unchanged.
		if (syncFailed) {
			item(span = { GridItemSpan(maxLineSpan) }) {
				Text(
					stringResource(Res.string.banner_sync_failed_cached),
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onErrorContainer,
					modifier = Modifier
						.fillMaxWidth()
						.padding(horizontal = 16.dp, vertical = 4.dp)
						.background(
							MaterialTheme.colorScheme.errorContainer,
							RoundedCornerShape(12.dp)
						)
						.padding(horizontal = 16.dp, vertical = 10.dp)
				)
			}
		}

		// Personal greeting sitting in the now-playing hero wash, above the sections.
		// onAmbient keeps it legible over any cover (and resolves to onSurface when nothing
		// is playing, so it never fakes contrast). "Library" itself stays in the top bar.
		item(span = { GridItemSpan(maxLineSpan) }) {
			Text(
				text = stringResource(greetingStringRes()),
				style = MaterialTheme.typography.headlineSmall,
				fontWeight = FontWeight.SemiBold,
				color = ambient.onAmbient,
				modifier = Modifier
					.fillMaxWidth()
					// start = 16.dp to line up with the section headers + overview buttons below.
					.padding(start = 16.dp, end = 16.dp, top = 2.dp, bottom = 10.dp)
			)
		}

		libraryScreenOverviewButton(
			icon = Icons.Outlined.LibraryAdd,
			label = Res.string.option_sort_newest,
			destination = Screen.AlbumList(true, DomainAlbumListType.Newest),
			start = true
		)
		libraryScreenOverviewButton(
			icon = Icons.Outlined.Shuffle,
			label = Res.string.option_sort_random,
			destination = Screen.AlbumList(true, DomainAlbumListType.Random),
			start = false
		)
		libraryScreenOverviewButton(
			icon = Icons.Outlined.Star,
			label = Res.string.option_sort_starred,
			destination = Screen.Starred(),
			start = true
		)
		libraryScreenOverviewButton(
			icon = Icons.Outlined.History,
			label = Res.string.option_sort_frequent,
			destination = Screen.AlbumList(true, DomainAlbumListType.Frequent),
			start = false
		)

		// Continue listening: jump straight back into a recent queue (radio, album, Mood Flow…),
		// resumed at where it left off. Sits above the album rows since it's the fastest way to
		// pick up an interrupted session. "See all" opens the full saved-queues screen.
		horizontalSection(
			title = Res.string.title_continue_listening,
			destination = Screen.SavedQueues,
			state = UiState.Success(continueListening),
			key = { it.id },
			seeAll = true
		) { queue ->
			ContinueListeningCard(
				modifier = Modifier.animateItem().width(150.dp),
				queue = queue,
				onClick = { onResumeQueue(queue.id) }
			)
		}

		horizontalSection(
			title = Res.string.option_sort_recent,
			destination = Screen.AlbumList(true, DomainAlbumListType.Recent),
			state = albumsState,
			key = { it.id },
			seeAll = true
		) { album ->
			AlbumListScreenItem(
				modifier = Modifier.animateItem().width(150.dp),
				tab = "library",
				album = album,
				selected = album == selectedAlbum,
				starred = selectedAlbumIsStarred,
				onSelect = { onSelectAlbum(album) },
				onDeselect = { onClearAlbumSelection() },
				onSetStarred = { onStarSelectedAlbum(it) },
				onSetShareId = { onSetShareId(it) },
				onPlayNext = onPlayAlbumNext,
				onAddToQueue = onAddAlbumToQueue,
				rating = selectedAlbumRating,
				onSetRating = onRateSelectedAlbum
			)
		}

		horizontalSection(
			title = Res.string.option_sort_frequent,
			destination = Screen.AlbumList(true, DomainAlbumListType.Frequent),
			state = frequentAlbumsState,
			key = { it.id },
			seeAll = true
		) { album ->
			AlbumListScreenItem(
				modifier = Modifier.animateItem().width(150.dp),
				tab = "library",
				album = album,
				selected = album == selectedAlbum,
				starred = selectedAlbumIsStarred,
				onSelect = { onSelectAlbum(album) },
				onDeselect = { onClearAlbumSelection() },
				onSetStarred = { onStarSelectedAlbum(it) },
				onSetShareId = { onSetShareId(it) },
				onPlayNext = onPlayAlbumNext,
				onAddToQueue = onAddAlbumToQueue,
				rating = selectedAlbumRating,
				onSetRating = onRateSelectedAlbum
			)
		}

		horizontalSection(
			title = Res.string.option_sort_newest,
			destination = Screen.AlbumList(true, DomainAlbumListType.Newest),
			state = newestAlbumsState,
			key = { it.id },
			seeAll = true
		) { album ->
			AlbumListScreenItem(
				modifier = Modifier.animateItem().width(150.dp),
				tab = "library",
				album = album,
				selected = album == selectedAlbum,
				starred = selectedAlbumIsStarred,
				onSelect = { onSelectAlbum(album) },
				onDeselect = { onClearAlbumSelection() },
				onSetStarred = { onStarSelectedAlbum(it) },
				onSetShareId = { onSetShareId(it) },
				onPlayNext = onPlayAlbumNext,
				onAddToQueue = onAddAlbumToQueue,
				rating = selectedAlbumRating,
				onSetRating = onRateSelectedAlbum
			)
		}

		horizontalSection(
			title = Res.string.title_playlists,
			destination = Screen.PlaylistList(true),
			state = playlistsState,
			key = { it.id },
			seeAll = true
		) { playlist ->
			PlaylistListScreenItem(
				modifier = Modifier.animateItem().width(150.dp),
				tab = "library",
				playlist = playlist,
				selected = playlist == selectedPlaylist,
				onSelect = { onSelectPlaylist(playlist) },
				onDeselect = { onClearPlaylistSelection() },
				onSetDeletionId = { onDeletePlaylist(it) },
				onSetShareId = { onSetShareId(it) },
				onPlayNext = onPlayPlaylistNext,
				onAddToQueue = onAddPlaylistToQueue
			)
		}

		horizontalSection(
			title = Res.string.title_artists,
			destination = Screen.ArtistList(true),
			state = artistsState,
			key = { it.id },
			seeAll = true
		) { artist ->
			ArtistsScreenItem(
				modifier = Modifier.animateItem().width(150.dp),
				tab = "library",
				artist = artist,
				selected = artist == selectedArtist,
				selectedArtistAlbums = selectedArtistAlbums,
				starred = selectedArtistIsStarred,
				onSelect = { onSelectArtist(artist) },
				onDeselect = { onClearArtistSelection() },
				onSetStarred = { onStarSelectedArtist(it) },
				onPlayNext = onPlayArtistNext,
				onAddToQueue = onAddArtistToQueue
			)
		}

		horizontalSection(
			title = Res.string.title_genres,
			destination = Screen.GenreList(true),
			state = genresState,
			key = { it.name },
			seeAll = true
		) { genreWithAlbums ->
			GenreListScreenCard(genre = genreWithAlbums)
		}
	}
}

/** Time-of-day greeting shown atop the library. */
private fun greetingStringRes(): StringResource {
	val hour = Clock.System.now()
		.toLocalDateTime(TimeZone.currentSystemDefault())
		.hour
	return when (hour) {
		in 5..11 -> Res.string.greeting_morning
		in 12..16 -> Res.string.greeting_afternoon
		else -> Res.string.greeting_evening
	}
}
