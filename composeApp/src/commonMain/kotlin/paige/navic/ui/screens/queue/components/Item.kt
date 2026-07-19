package paige.navic.ui.screens.queue.components

import androidx.compose.animation.core.animateDpAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedListItem
import androidx.compose.material3.Surface
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_remove_from_queue
import navic.composeapp.generated.resources.action_reorder
import org.jetbrains.compose.resources.stringResource
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.domain.models.DomainSong
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Delete
import paige.navic.icons.outlined.DragHandle
import paige.navic.ui.components.common.CoverArt
import paige.navic.ui.components.common.MarqueeText
import paige.navic.ui.components.common.SongRowDefaults
import paige.navic.ui.components.common.SongRowStatus
import paige.navic.util.ui.DraggableListState
import paige.navic.util.ui.dragHandle
import paige.navic.util.ui.segmentedShapes

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun QueueScreenItem(
	index: Int,
	count: Int,
	song: DomainSong,
	isPlaying: Boolean,
	isSelected: Boolean,
	isDragging: Boolean,
	draggableState: DraggableListState,
	onClick: () -> Unit,
	onRemove: () -> Unit,
	dragEnabled: Boolean = true,
	isOffline: Boolean = false,
	isDownloaded: Boolean = false
) {
	val canPlay = !isOffline || isDownloaded

	val elevation by animateDpAsState(
		targetValue = if (isDragging) 8.dp else 0.dp,
		animationSpec = MaterialTheme.motionScheme.defaultEffectsSpec()
	)

	val dismissState = rememberSwipeToDismissBoxState()
	val scope = rememberCoroutineScope()

	// Shared with the album/search rows — translucent, so the queue's ONE ambient wash
	// (from the now-playing song) shows through every card rather than each row tinting
	// itself from its own cover.
	val color = SongRowDefaults.containerColor(isSelected)
	val contentColor = SongRowDefaults.contentColor(isSelected)
	val supportingContentColor = SongRowDefaults.supportingContentColor(isSelected)

	val itemShape = segmentedShapes(
		index = index,
		count = count,
		dismissDirection = dismissState.dismissDirection
	)

	SwipeToDismissBox(
		state = dismissState,
		onDismiss = {
			onRemove()
			scope.launch {
				dismissState.reset()
			}
		},
		backgroundContent = {
			Box(
				modifier = Modifier
					.fillMaxSize()
					.clip(itemShape.shape)
					// Only paint the red delete background while actively swiping —
					// otherwise it would bleed through the translucent (frosted) card.
					.background(
						if (dismissState.dismissDirection == SwipeToDismissBoxValue.Settled)
							Color.Transparent
						else MaterialTheme.colorScheme.errorContainer
					)
					.padding(horizontal = 20.dp)
			) {
				Icon(
					imageVector = Icons.Outlined.Delete,
					contentDescription = stringResource(Res.string.action_remove_from_queue),
					tint = MaterialTheme.colorScheme.onErrorContainer,
					modifier = Modifier.align(when (dismissState.dismissDirection) {
						SwipeToDismissBoxValue.StartToEnd -> Alignment.CenterStart
						else -> Alignment.CenterEnd
					})
				)
			}
		},
		content = {
			Surface(
				shadowElevation = elevation,
				shape = itemShape.shape
			) {
				SegmentedListItem(
					onClick = onClick,
					enabled = canPlay,
					colors = ListItemDefaults.colors(
						containerColor = color,
						selectedContainerColor = color,
						disabledContainerColor = color,
						draggedContainerColor = color,
						contentColor = contentColor,
						supportingContentColor = supportingContentColor
					),
					shapes = itemShape,
					verticalAlignment = Alignment.CenterVertically,
					content = { MarqueeText(song.title) },
					supportingContent = { MarqueeText(song.artistName) },
					leadingContent = {
						CoverArt(
							modifier = Modifier.size(SongRowDefaults.CoverSize),
							coverArtId = song.coverArtId,
							shape = SongRowDefaults.CoverShape
						)
					},
					trailingContent = {
						SongRowStatus(
							canPlay = canPlay,
							downloadStatus = if (isDownloaded) DownloadStatus.DOWNLOADED else null,
							isCurrentTrack = isSelected,
							isPlaying = isPlaying,
							duration = song.duration,
							durationColor = supportingContentColor,
							// Appended AFTER the status cluster so the handle keeps the same
							// position whichever icons a given row happens to show. Drag reorders
							// the local queue, or the hub session queue when another device is
							// active (the hub broadcasts the new order back).
							trailing = if (dragEnabled) {
								{
									IconButton(
										modifier = Modifier.dragHandle(
											state = draggableState,
											index = index
										),
										onClick = {}
									) {
										Icon(
											Icons.Outlined.DragHandle,
											contentDescription = stringResource(Res.string.action_reorder)
										)
									}
								}
							} else null
						)
					},
					contentPadding = SongRowDefaults.ContentPadding
				)
			}
		}
	)
}
