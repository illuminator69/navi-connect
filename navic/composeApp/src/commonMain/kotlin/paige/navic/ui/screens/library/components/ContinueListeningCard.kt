package paige.navic.ui.screens.library.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_delete_queue
import navic.composeapp.generated.resources.action_preview_queue
import navic.composeapp.generated.resources.action_resume_queue
import org.jetbrains.compose.resources.stringResource
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Delete
import paige.navic.icons.outlined.MoreVert
import paige.navic.icons.outlined.Queue
import paige.navic.ui.components.common.CoverArt
import paige.navic.ui.components.common.Dropdown
import paige.navic.ui.components.common.DropdownItem
import paige.navic.ui.screens.savedqueues.savedQueueCoverArtId
import paige.navic.ui.screens.savedqueues.savedQueueSubtitle
import paige.navic.ui.screens.savedqueues.savedQueueTitle

/**
 * A "Continue listening" card: the queue's cover (frozen at its first track, so it doesn't change as
 * playback moves), what it's called, and how it was made. Tapping resumes the queue at its saved
 * playhead; the overflow previews its tracks or drops it from the history.
 *
 * The queue playing right now appears here too, first and marked — see LibraryScreen. Mirrors the
 * album/playlist card footprint (150.dp square + two text lines) so the home rows line up.
 */
@Composable
fun ContinueListeningCard(
	queue: SavedQueueEntity,
	isActive: Boolean,
	onClick: () -> Unit,
	onPreview: () -> Unit,
	onRemove: () -> Unit,
	modifier: Modifier = Modifier
) {
	val name = savedQueueTitle(queue)
	val subtitle = savedQueueSubtitle(queue, isActive)
	var menuOpen by remember { mutableStateOf(false) }

	Column(modifier) {
		Box {
			CoverArt(
				modifier = Modifier.fillMaxWidth(),
				coverArtId = savedQueueCoverArtId(queue),
				contentDescription = name,
				onClick = onClick,
				onLongClick = { menuOpen = true }
			)
			Box(Modifier.align(Alignment.TopEnd)) {
				IconButton(onClick = { menuOpen = true }) {
					Icon(Icons.Outlined.MoreVert, contentDescription = null)
				}
				Dropdown(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
					DropdownItem(
						text = { Text(stringResource(Res.string.action_resume_queue)) },
						onClick = {
							menuOpen = false
							onClick()
						},
						leadingIcon = { Icon(Icons.Outlined.Queue, null) }
					)
					DropdownItem(
						text = { Text(stringResource(Res.string.action_preview_queue)) },
						onClick = {
							menuOpen = false
							onPreview()
						},
						leadingIcon = { Icon(Icons.Outlined.Queue, null) }
					)
					DropdownItem(
						text = { Text(stringResource(Res.string.action_delete_queue)) },
						onClick = {
							menuOpen = false
							onRemove()
						},
						leadingIcon = { Icon(Icons.Outlined.Delete, null) }
					)
				}
			}
		}
		Text(
			text = name,
			style = MaterialTheme.typography.bodyMedium,
			fontWeight = if (isActive) FontWeight.SemiBold else null,
			color = if (isActive) MaterialTheme.colorScheme.primary
			else MaterialTheme.colorScheme.onSurface,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(top = 6.dp, start = 2.dp, end = 2.dp)
		)
		Text(
			text = subtitle,
			style = MaterialTheme.typography.bodySmall,
			color = if (isActive) MaterialTheme.colorScheme.primary
			else MaterialTheme.colorScheme.onSurfaceVariant,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(start = 2.dp, end = 2.dp)
		)
	}
}
