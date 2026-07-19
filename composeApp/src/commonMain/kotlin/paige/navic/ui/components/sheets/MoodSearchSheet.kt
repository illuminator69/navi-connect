package paige.navic.ui.components.sheets

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import paige.navic.domain.models.DomainSong
import paige.navic.util.ui.rememberNowPlayingCoverAmbient

/**
 * Feishin-style preview for a CLAP text→mood search: shows the proposed queue
 * (best match first) and lets the user Play it as the queue or append it — it
 * does NOT auto-play on open, so the user reviews the matches first.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MoodSearchSheet(
	query: String,
	songs: List<DomainSong>,
	loading: Boolean,
	onPlay: () -> Unit,
	onAddToQueue: () -> Unit,
	onDismissRequest: () -> Unit
) {
	ModalBottomSheet(
		onDismissRequest = onDismissRequest,
		sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
		// Cover-tinted surface + cover-themed content (see the shared wrapper).
		ambient = rememberNowPlayingCoverAmbient()
	) {
		Column(
			modifier = Modifier.padding(horizontal = 24.dp),
			verticalArrangement = Arrangement.spacedBy(12.dp)
		) {
			Text(
				"Mood search",
				style = MaterialTheme.typography.titleLarge,
				color = MaterialTheme.colorScheme.primary
			)
			Text(
				"\"$query\"",
				style = MaterialTheme.typography.bodyMedium,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)

			if (loading) {
				Row(
					modifier = Modifier.fillMaxWidth().padding(vertical = 24.dp),
					horizontalArrangement = Arrangement.Center
				) {
					CircularProgressIndicator()
				}
			} else if (songs.isEmpty()) {
				Text(
					"No matches. CLAP search may be unavailable or your library isn't analyzed yet.",
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			} else {
				Text(
					"${songs.size} tracks",
					style = MaterialTheme.typography.labelLarge,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
				Row(
					modifier = Modifier.fillMaxWidth(),
					horizontalArrangement = Arrangement.spacedBy(12.dp),
					verticalAlignment = Alignment.CenterVertically
				) {
					Button(onClick = {
						onPlay()
						onDismissRequest()
					}) {
						Text("Play")
					}
					OutlinedButton(onClick = {
						onAddToQueue()
						onDismissRequest()
					}) {
						Text("Add to queue")
					}
				}
				LazyColumn(
					modifier = Modifier.fillMaxWidth().heightIn(max = 360.dp),
					verticalArrangement = Arrangement.spacedBy(4.dp)
				) {
					items(songs) { song ->
						Column(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
							Text(
								song.title,
								style = MaterialTheme.typography.bodyLarge,
								maxLines = 1,
								overflow = TextOverflow.Ellipsis
							)
							Text(
								song.artistName,
								style = MaterialTheme.typography.bodySmall,
								color = MaterialTheme.colorScheme.onSurfaceVariant,
								maxLines = 1,
								overflow = TextOverflow.Ellipsis
							)
						}
					}
				}
			}
			Spacer(Modifier.height(24.dp))
		}
	}
}
