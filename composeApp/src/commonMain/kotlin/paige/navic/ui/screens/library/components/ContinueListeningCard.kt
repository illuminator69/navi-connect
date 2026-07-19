package paige.navic.ui.screens.library.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.queue_kind_album
import navic.composeapp.generated.resources.queue_kind_journey
import navic.composeapp.generated.resources.queue_kind_manual
import navic.composeapp.generated.resources.queue_kind_mood_flow
import navic.composeapp.generated.resources.queue_kind_playlist
import navic.composeapp.generated.resources.queue_kind_radio
import navic.composeapp.generated.resources.queue_unnamed
import org.jetbrains.compose.resources.stringResource
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.SavedQueueSource
import paige.navic.ui.components.common.CoverArt

/**
 * A "Continue listening" card: the queue's current-track cover, its name, and how it was made.
 * Tapping resumes the queue at its saved playhead. Mirrors the album/playlist card footprint
 * (150.dp square + two text lines) so the home rows line up.
 */
@Composable
fun ContinueListeningCard(
	queue: SavedQueueEntity,
	onClick: () -> Unit,
	modifier: Modifier = Modifier
) {
	val name = queue.name?.takeIf { it.isNotBlank() }
		?: queue.sourceName?.takeIf { it.isNotBlank() }
		?: stringResource(Res.string.queue_unnamed)

	Column(modifier) {
		CoverArt(
			modifier = Modifier.fillMaxWidth(),
			coverArtId = queue.coverArtId,
			contentDescription = name,
			onClick = onClick
		)
		Text(
			text = name,
			style = MaterialTheme.typography.bodyMedium,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(top = 6.dp, start = 2.dp, end = 2.dp)
		)
		Text(
			text = continueListeningKindLabel(queue.sourceKind),
			style = MaterialTheme.typography.bodySmall,
			color = MaterialTheme.colorScheme.onSurfaceVariant,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(start = 2.dp, end = 2.dp)
		)
	}
}

@Composable
private fun continueListeningKindLabel(kind: String): String = when (kind) {
	SavedQueueSource.ALBUM -> stringResource(Res.string.queue_kind_album)
	SavedQueueSource.PLAYLIST -> stringResource(Res.string.queue_kind_playlist)
	SavedQueueSource.RADIO -> stringResource(Res.string.queue_kind_radio)
	SavedQueueSource.MOOD_FLOW -> stringResource(Res.string.queue_kind_mood_flow)
	SavedQueueSource.JOURNEY -> stringResource(Res.string.queue_kind_journey)
	SavedQueueSource.MANUAL -> stringResource(Res.string.queue_kind_manual)
	else -> kind
}
