package paige.navic.ui.components.common

import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.appendInlineContent
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.dropUnlessResumed
import kotlinx.collections.immutable.persistentListOf
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.info_unknown_album
import navic.composeapp.generated.resources.info_unknown_year
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.LocalNavStack
import paige.navic.data.database.entities.DownloadEntity
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.models.DomainExplicitStatus
import paige.navic.domain.models.DomainSong
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.dialogs.QueueDuplicateDialog
import paige.navic.ui.components.sheets.SongSheet
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.playlist.dialogs.PlaylistUpdateDialog
import paige.navic.util.core.InlineExplicitIcon

@Composable
fun SongRow(
	modifier: Modifier = Modifier,
	song: DomainSong,
	selected: Boolean = false,
	onClick: (() -> Unit),
	onLongClick: (() -> Unit),
	isOnline: Boolean = false,
	onDismissRequest: () -> Unit,
	onRemoveStar: () -> Unit,
	onAddStar: () -> Unit,
	onShare: () -> Unit,
	starredState: Boolean,
	download: DownloadEntity? = null,
	onDownload: () -> Unit,
	onCancelDownload: () -> Unit,
	onDeleteDownload: () -> Unit,
	onPlayNext: () -> Unit,
	onAddToQueue: () -> Unit,
	rating: Int,
	onSetRating: (Int) -> Unit,
	containerColor: Color = MaterialTheme.colorScheme.surface,
	width: Dp = 400.dp
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val player = koinInject<MediaPlayerViewModel>()
	val radioManager = koinInject<RadioManager>()
	// steadyState, not uiState: this row is rendered once per visible song in every list in the
	// app, and only reads currentSong/isPaused — collecting the playhead too would recompose all
	// of them ~5x a second for the whole of playback.
	val playerState by player.steadyState.collectAsStateWithLifecycle()
	val sonicAvailable by radioManager.sonicSimilarityAvailable.collectAsStateWithLifecycle()

	val backStack = LocalNavStack.current
	var playlistDialogShown by rememberSaveable { mutableStateOf(false) }
	var duplicateQueueDialogShown by rememberSaveable { mutableStateOf(false) }
	var duplicateQueueDialogShownPlayNext by rememberSaveable { mutableStateOf(false) }

	val isDownloaded = download?.status == DownloadStatus.DOWNLOADED
	val isCurrentTrack = playerState.currentSong?.id == song.id
	val canPlay = isOnline || isDownloaded

	ListItem(
		modifier = modifier
			.width(width)
			.combinedClickable (
				onClick = onClick,
				onLongClick = onLongClick
			),
		colors = ListItemDefaults.colors(containerColor = containerColor),
		headlineContent = {
			Text(
				text = buildAnnotatedString {
					append(song.title)
					if (song.explicitStatus == DomainExplicitStatus.Explicit) {
						append(" ")
						appendInlineContent("InlineExplicitIcon")
					}
				},
				inlineContent = InlineExplicitIcon,
				maxLines = 2
			)
		},
		supportingContent = {
			MarqueeText(
				text = buildString {
					append(song.albumTitle ?: stringResource(Res.string.info_unknown_album))
					append(" • ")
					append(song.artistName)
					append(" • ")
					append(song.year ?: stringResource(Res.string.info_unknown_year))
				}
			)
		},
		leadingContent = {
			CoverArt(
				coverArtId = song.coverArtId,
				modifier = Modifier.size(50.dp),
				shape = preferenceManager.coverArtShape.decreasedShape
			)
		},
		trailingContent = {
			// No duration here: this row already spends its supporting line on
			// album • artist • year, and the lists it serves are browsing surfaces.
			SongRowStatus(
				modifier = Modifier.height(83.dp),
				isStarred = starredState,
				canPlay = canPlay,
				downloadStatus = download?.status,
				downloadProgress = download?.progress ?: 0f,
				isCurrentTrack = isCurrentTrack,
				isPlaying = !playerState.isPaused
			)
		}
	)

	if (selected) {
		SongSheet(
			onDismissRequest = onDismissRequest,
			song = song,
			starred = starredState,
			rating = rating,
			onSetStarred = { starred ->
				if (starred) onAddStar() else onRemoveStar()
			},
			onShare = onShare,
			onPlayNext = {
				if (player.uiState.value.queue.any { it.id == song.id }) {
					duplicateQueueDialogShown = true
					duplicateQueueDialogShownPlayNext = true
				} else {
					onPlayNext()
				}
			},
			onAddToQueue = {
				if (player.uiState.value.queue.any { it.id == song.id }) {
					duplicateQueueDialogShown = true
					duplicateQueueDialogShownPlayNext = false
				} else {
					onAddToQueue()
				}
			},
			onTrackInfo = dropUnlessResumed {
				backStack.add(Screen.SongDetail(song.id))
			},
			onViewAlbum = song.albumId?.let { albumId ->
				dropUnlessResumed {
					backStack.add(
						Screen.CollectionDetail(
							collectionId = albumId,
							tab = "library"
						)
					)
				}
			},
			onAddToPlaylist = {
				playlistDialogShown = true
			},
			onStartRadio = {
				radioManager.startRadio(song.id, song)
			},
			// Sonic journey from the now-playing track to this one. Only offered
			// when the AudioMuse plugin is present and something else is playing.
			onStartJourney = playerState.currentSong?.takeIf {
				sonicAvailable && it.id != song.id
			}?.let { nowPlaying ->
				{ radioManager.startJourney(nowPlaying.id, song.id) }
			},
			downloadStatus = download?.status,
			onDownload = onDownload,
			onCancelDownload = onCancelDownload,
			onDeleteDownload = onDeleteDownload,
			onSetRating = onSetRating
		)
	}

	if (playlistDialogShown) {
		PlaylistUpdateDialog(
			songs = persistentListOf(song),
			onDismissRequest = { playlistDialogShown = false }
		)
	}

	if (duplicateQueueDialogShown) {
		QueueDuplicateDialog(
			onDismissRequest = {
				duplicateQueueDialogShown = false
				onDismissRequest()
			},
			onConfirm = {
				if (duplicateQueueDialogShownPlayNext) onPlayNext() else onAddToQueue()
			}
		)
	}
}
