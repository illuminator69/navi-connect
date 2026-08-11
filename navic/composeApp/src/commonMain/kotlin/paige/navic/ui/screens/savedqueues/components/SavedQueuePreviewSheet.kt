package paige.navic.ui.screens.savedqueues.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_close
import navic.composeapp.generated.resources.action_resume_queue
import navic.composeapp.generated.resources.queue_resumes_here
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.DomainSong
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.ui.components.common.CoverArt
import paige.navic.ui.components.common.FormButton
import paige.navic.ui.components.sheets.ModalBottomSheet
import paige.navic.ui.screens.savedqueues.queueKindLabel
import paige.navic.ui.screens.savedqueues.savedQueueDuration
import paige.navic.ui.screens.savedqueues.savedQueueTitle
import paige.navic.ui.screens.savedqueues.savedQueueCoverArtId
import paige.navic.ui.screens.savedqueues.trackCountLabel
import paige.navic.ui.screens.savedqueues.trackDuration

/**
 * "Preview queue": what's actually in a saved queue, and where resuming it would land, without
 * committing to replacing what's playing. Matches Feishin's preview modal — the track list with the
 * resume row marked, the total runtime, and Resume / Close.
 *
 * Nothing is fetched: the record already carries its songs, so this only has to decode the blob.
 */
@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun SavedQueuePreviewSheet(
	queue: SavedQueueEntity,
	onResume: () -> Unit,
	onDismissRequest: () -> Unit
) {
	val repository = koinInject<SavedQueueRepository>()
	// Decoding is off the main thread's critical path and only happens while the sheet is open.
	val songs by produceState(initialValue = emptyList<DomainSong>(), queue.id) {
		value = repository.decodeQueue(queue)
	}
	val resumeIndex = remember(queue.currentIndex, songs.size) {
		if (songs.isEmpty()) -1 else queue.currentIndex.coerceIn(0, songs.lastIndex)
	}
	val listState = rememberLazyListState()
	// Open ON the resume row rather than at the top: it's the one row the user came here to see.
	LaunchedEffect(resumeIndex) {
		if (resumeIndex > 2) listState.scrollToItem(resumeIndex - 2)
	}

	ModalBottomSheet(onDismissRequest = onDismissRequest) {
		Row(
			Modifier.fillMaxWidth().padding(horizontal = 20.dp),
			verticalAlignment = Alignment.CenterVertically
		) {
			CoverArt(
				coverArtId = savedQueueCoverArtId(queue),
				modifier = Modifier.size(56.dp),
				shape = RoundedCornerShape(8.dp)
			)
			Spacer(Modifier.width(14.dp))
			Column(Modifier.weight(1f)) {
				Text(
					savedQueueTitle(queue),
					style = MaterialTheme.typography.titleMedium,
					maxLines = 1,
					overflow = TextOverflow.Ellipsis
				)
				Text(
					"${queueKindLabel(queue.sourceKind)} · ${trackCountLabel(songs.size)}" +
						if (songs.isEmpty()) "" else " · ${savedQueueDuration(songs)}",
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onSurfaceVariant,
					maxLines = 1,
					overflow = TextOverflow.Ellipsis
				)
			}
		}

		Spacer(Modifier.height(12.dp))

		LazyColumn(
			state = listState,
			modifier = Modifier
				.fillMaxWidth()
				.heightIn(max = 360.dp)
				.padding(horizontal = 12.dp)
		) {
			itemsIndexed(songs, key = { i, song -> "$i-${song.id}" }) { i, song ->
				PreviewTrackRow(
					position = i + 1,
					song = song,
					isResumePoint = i == resumeIndex
				)
			}
		}

		Spacer(Modifier.height(12.dp))

		Row(
			Modifier.fillMaxWidth().padding(horizontal = 16.dp),
			horizontalArrangement = Arrangement.spacedBy(8.dp)
		) {
			Box(Modifier.weight(1f)) {
				FormButton(onClick = onDismissRequest) {
					Text(stringResource(Res.string.action_close))
				}
			}
			Box(Modifier.weight(1f)) {
				FormButton(
					onClick = onResume,
					color = MaterialTheme.colorScheme.primary
				) {
					Text(stringResource(Res.string.action_resume_queue))
				}
			}
		}
		Spacer(Modifier.height(16.dp))
	}
}

@Composable
private fun PreviewTrackRow(position: Int, song: DomainSong, isResumePoint: Boolean) {
	Row(
		Modifier
			.fillMaxWidth()
			.clip(RoundedCornerShape(8.dp))
			.background(
				if (isResumePoint) MaterialTheme.colorScheme.primaryContainer
				else androidx.compose.ui.graphics.Color.Transparent
			)
			.padding(horizontal = 8.dp, vertical = 6.dp),
		verticalAlignment = Alignment.CenterVertically
	) {
		Text(
			position.toString(),
			style = MaterialTheme.typography.bodySmall,
			color = if (isResumePoint) MaterialTheme.colorScheme.onPrimaryContainer
			else MaterialTheme.colorScheme.onSurfaceVariant,
			modifier = Modifier.width(28.dp)
		)
		Column(Modifier.weight(1f)) {
			Text(
				song.title,
				style = MaterialTheme.typography.bodyMedium,
				fontWeight = if (isResumePoint) FontWeight.SemiBold else null,
				color = if (isResumePoint) MaterialTheme.colorScheme.onPrimaryContainer
				else MaterialTheme.colorScheme.onSurface,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
			Text(
				listOfNotNull(
					song.artistName.takeIf { it.isNotBlank() },
					song.albumTitle?.takeIf { it.isNotBlank() },
					stringResource(Res.string.queue_resumes_here).takeIf { isResumePoint }
				).joinToString(" · "),
				style = MaterialTheme.typography.bodySmall,
				color = if (isResumePoint) MaterialTheme.colorScheme.onPrimaryContainer
				else MaterialTheme.colorScheme.onSurfaceVariant,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
		}
		Spacer(Modifier.width(8.dp))
		Text(
			trackDuration(song),
			style = MaterialTheme.typography.bodySmall,
			color = if (isResumePoint) MaterialTheme.colorScheme.onPrimaryContainer
			else MaterialTheme.colorScheme.onSurfaceVariant
		)
	}
}
