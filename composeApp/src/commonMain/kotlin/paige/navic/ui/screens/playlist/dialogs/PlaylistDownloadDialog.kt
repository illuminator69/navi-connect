package paige.navic.ui.screens.playlist.dialogs

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.koin.compose.koinInject
import paige.navic.domain.manager.PlaylistDownloadManager
import paige.navic.domain.manager.PlaylistDownloadPolicy

private val QUALITIES = listOf(0, 320, 192, 128)
private val FORMATS = listOf("", "opus", "mp3")
// Decimal GB, matching how storage sizes are shown to users elsewhere.
private const val BYTES_PER_GB = 1_000_000_000.0

/**
 * Per-playlist auto-download settings: off / permanent / rolling cache, with
 * transcode quality + format for the downloaded files. Pairs with server-side
 * smart playlists for self-refreshing offline mixes.
 */
@Composable
fun PlaylistDownloadDialog(
	playlistId: String,
	playlistName: String,
	onDismissRequest: () -> Unit
) {
	val manager = koinInject<PlaylistDownloadManager>()
	val policies by manager.policies.collectAsState()
	val existing = policies[playlistId]

	var modeIndex by remember {
		mutableStateOf(
			when (existing?.mode) {
				PlaylistDownloadPolicy.MODE_PERMANENT -> 1
				PlaylistDownloadPolicy.MODE_ROLLING -> 2
				else -> 0
			}
		)
	}
	var rollingLimit by remember { mutableStateOf((existing?.rollingLimit ?: 50).toString()) }
	var budgetGb by remember {
		mutableStateOf(
			existing?.budgetBytes
				?.takeIf { it > 0 }
				?.let { (it.toDouble() / BYTES_PER_GB).toString() }
				?: ""
		)
	}
	var qualityIndex by remember {
		mutableStateOf(QUALITIES.indexOf(existing?.maxBitRate ?: 0).coerceAtLeast(0))
	}
	var formatIndex by remember {
		mutableStateOf(FORMATS.indexOf(existing?.format ?: "").coerceAtLeast(0))
	}
	var deleteOnDisable by remember { mutableStateOf(true) }

	AlertDialog(
		onDismissRequest = onDismissRequest,
		title = { Text("Auto-download") },
		text = {
			Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
				Text(playlistName, style = MaterialTheme.typography.bodyMedium)

				SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
					listOf("Off", "Permanent", "Rolling").forEachIndexed { index, label ->
						SegmentedButton(
							selected = modeIndex == index,
							onClick = { modeIndex = index },
							shape = SegmentedButtonDefaults.itemShape(index, 3)
						) { Text(label) }
					}
				}

				if (modeIndex == 2) {
					OutlinedTextField(
						value = rollingLimit,
						onValueChange = { rollingLimit = it },
						label = { Text("Keep first N songs") },
						singleLine = true
					)
					OutlinedTextField(
						value = budgetGb,
						onValueChange = { budgetGb = it },
						label = { Text("Max size in GB (optional)") },
						singleLine = true
					)
				}

				if (modeIndex != 0) {
					Text("Quality", style = MaterialTheme.typography.labelLarge)
					SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
						listOf("Original", "320", "192", "128").forEachIndexed { index, label ->
							SegmentedButton(
								selected = qualityIndex == index,
								onClick = { qualityIndex = index },
								shape = SegmentedButtonDefaults.itemShape(index, 4)
							) { Text(label) }
						}
					}
					Text("Format", style = MaterialTheme.typography.labelLarge)
					SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
						listOf("Original", "opus", "mp3").forEachIndexed { index, label ->
							SegmentedButton(
								selected = formatIndex == index,
								onClick = { formatIndex = index },
								shape = SegmentedButtonDefaults.itemShape(index, 3)
							) { Text(label) }
						}
					}
				} else if (existing != null) {
					Row(verticalAlignment = Alignment.CenterVertically) {
						Switch(
							checked = deleteOnDisable,
							onCheckedChange = { deleteOnDisable = it }
						)
						Text(
							"  Delete downloaded files",
							style = MaterialTheme.typography.bodyMedium
						)
					}
				}
			}
		},
		confirmButton = {
			TextButton(onClick = {
				if (modeIndex == 0) {
					manager.removePolicy(playlistId, deleteDownloads = deleteOnDisable)
				} else {
					manager.setPolicy(
						PlaylistDownloadPolicy(
							playlistId = playlistId,
							playlistName = playlistName,
							mode = if (modeIndex == 1) {
								PlaylistDownloadPolicy.MODE_PERMANENT
							} else {
								PlaylistDownloadPolicy.MODE_ROLLING
							},
							rollingLimit = rollingLimit.toIntOrNull() ?: 50,
							budgetBytes = budgetGb.trim().toDoubleOrNull()
								?.takeIf { it > 0 }
								?.let { (it * BYTES_PER_GB).toLong() }
								?: 0L,
							maxBitRate = QUALITIES[qualityIndex],
							format = FORMATS[formatIndex]
						)
					)
				}
				onDismissRequest()
			}) { Text("Save") }
		},
		dismissButton = {
			TextButton(onClick = onDismissRequest) { Text("Cancel") }
		}
	)
}
