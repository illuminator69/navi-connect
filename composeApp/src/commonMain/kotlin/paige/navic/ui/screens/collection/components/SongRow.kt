package paige.navic.ui.screens.collection.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.TextAutoSize
import androidx.compose.foundation.text.appendInlineContent
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedListItem
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_add_to_queue
import navic.composeapp.generated.resources.action_play_next
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.data.database.entities.DownloadEntity
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.domain.models.DomainExplicitStatus
import paige.navic.domain.models.DomainSong
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Queue
import paige.navic.icons.outlined.QueuePlayNext
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.CoverArt
import paige.navic.ui.components.common.MarqueeText
import paige.navic.ui.components.common.SongRowDefaults
import paige.navic.ui.components.common.SongRowStatus
import paige.navic.util.core.InlineExplicitIcon
import paige.navic.util.ui.segmentedShapes

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun CollectionDetailScreenSongRow(
	song: DomainSong,
	index: Int,
	count: Int,
	isPlaylist: Boolean = false,
	onClick: (() -> Unit),
	onLongClick: (() -> Unit),
	onPlayNext: (() -> Unit),
	onAddToQueue: (() -> Unit),
	isStarred: Boolean,
	download: DownloadEntity? = null,
	isOffline: Boolean = false
) {
	val player = koinInject<MediaPlayerViewModel>()
	val playerState by player.uiState.collectAsStateWithLifecycle()

	val isDownloaded = download?.status == DownloadStatus.DOWNLOADED
	val isCurrentTrack = playerState.currentSong?.id == song.id
	val canPlay = !isOffline || isDownloaded

	val dismissState = rememberSwipeToDismissBoxState()
	val scope = rememberCoroutineScope()

	val itemShape = segmentedShapes(
		index = index,
		count = count,
		dismissDirection = dismissState.dismissDirection
	)

	SwipeToDismissBox(
		modifier = Modifier.padding(horizontal = 16.dp, vertical = 1.5.dp),
		state = dismissState,
		onDismiss = {
			if (it == SwipeToDismissBoxValue.StartToEnd) onAddToQueue()
			if (it == SwipeToDismissBoxValue.EndToStart) onPlayNext()
			scope.launch { dismissState.reset() }
		},
		backgroundContent = {
			Box(
				modifier = Modifier
					.fillMaxSize()
					// Match the segmented card shape: this background shows THROUGH the
					// translucent (frosted) card as a distinguishing backing, so it must
					// share the card's grouped corners (not the fully-rounded largeIncreased).
					.clip(itemShape.shape)
					.background(MaterialTheme.colorScheme.primaryContainer)
					.padding(horizontal = 20.dp)
			) {
				when (dismissState.dismissDirection) {
					SwipeToDismissBoxValue.StartToEnd -> {
						Icon(
							imageVector = Icons.Outlined.Queue,
							contentDescription = stringResource(Res.string.action_add_to_queue),
							tint = MaterialTheme.colorScheme.onPrimaryContainer,
							modifier = Modifier.align(Alignment.CenterStart)
						)
					}
					SwipeToDismissBoxValue.EndToStart -> {
						Icon(
							imageVector = Icons.Outlined.QueuePlayNext,
							contentDescription = stringResource(Res.string.action_play_next),
							tint = MaterialTheme.colorScheme.onPrimaryContainer,
							modifier = Modifier.align(Alignment.CenterEnd)
						)
					}
					else -> {}
				}
			}
		}
	) {
		SegmentedListItem(
			contentPadding = SongRowDefaults.ContentPadding,
			onClick = onClick,
			onLongClick = onLongClick,
			shapes = itemShape,
			colors = ListItemDefaults.segmentedColors(
				// Translucent so the album's cover gradient shows through (frosted look);
				// the playing row lifts to a higher tint — same rule as the queue.
				containerColor = SongRowDefaults.containerColor(isCurrentTrack),
				contentColor = SongRowDefaults.contentColor(isCurrentTrack)
			),
			leadingContent = {
				if (isPlaylist)
						CoverArt(
							modifier = Modifier.size(SongRowDefaults.CoverSize),
							coverArtId = song.coverArtId,
							shape = SongRowDefaults.CoverShape
						)
				else
					Text(
						text = "${index + 1}",
						modifier = Modifier.width(25.dp),
						style = LocalTextStyle.current.copy(fontFeatureSettings = "tnum"),
						fontWeight = FontWeight(400),
						color = SongRowDefaults.supportingContentColor(isCurrentTrack),
						maxLines = 1,
						textAlign = TextAlign.Center,
						autoSize = TextAutoSize.StepBased(6.sp, 13.sp)
					)
			},
			content = {
				Column {
					MarqueeText(
						text = buildAnnotatedString {
							append(song.title)
							if (song.explicitStatus == DomainExplicitStatus.Explicit) {
								append(" ")
								appendInlineContent("InlineExplicitIcon")
							}
						},
						inlineContent = InlineExplicitIcon
					)
					Text(
						song.artistName,
						style = MaterialTheme.typography.bodySmall,
						maxLines = 1
					)
				}
			},
			trailingContent = {
				SongRowStatus(
					isStarred = isStarred,
					canPlay = canPlay,
					downloadStatus = download?.status,
					downloadProgress = download?.progress ?: 0f,
					isCurrentTrack = isCurrentTrack,
					isPlaying = !playerState.isPaused,
					duration = song.duration,
					durationColor = SongRowDefaults.supportingContentColor(isCurrentTrack)
				)
			}
		)
	}
}
