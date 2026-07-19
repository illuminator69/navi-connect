package paige.navic.ui.components.common

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.kyant.capsule.ContinuousRoundedRectangle
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.info_download_failed
import navic.composeapp.generated.resources.info_download_queued
import navic.composeapp.generated.resources.info_downloaded
import navic.composeapp.generated.resources.info_not_available_offline
import org.jetbrains.compose.resources.stringResource
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.icons.Icons
import paige.navic.icons.filled.Star
import paige.navic.icons.outlined.Check
import paige.navic.icons.outlined.DownloadOff
import paige.navic.icons.outlined.Offline
import paige.navic.icons.outlined.Queue
import paige.navic.util.core.toHoursMinutesSeconds
import kotlin.time.Duration

/**
 * One visual language for every song row — album tracks, queue slots, song lists and
 * search results. The rows differ in what they CARRY (a track number, a drag handle),
 * not in how they look, so the shared sizes, paddings and container tints live here
 * instead of being re-picked per screen.
 *
 * Containers are deliberately translucent: a song row sits over the ONE ambient wash
 * that its screen owns (the album's cover gradient, the queue's now-playing ambient),
 * and lets it through. A row never derives a colour from its own cover art — a queue of
 * mixed albums would turn into a patchwork of clashing cards.
 */
object SongRowDefaults {
	val ContentPadding = PaddingValues(14.dp)
	val CoverSize = 48.dp
	val CoverShape = ContinuousRoundedRectangle(10.dp)
	val TrailingSpacing = 8.dp
	val StatusIconSize = 16.dp
	val OfflineIconSize = 20.dp

	/** Enough wash shows through to tie the row to the page; enough tint to stay a card. */
	private const val ContainerAlpha = 0.6f

	@Composable
	fun containerColor(isCurrentTrack: Boolean): Color =
		if (isCurrentTrack) MaterialTheme.colorScheme.surfaceContainerHighest.copy(alpha = ContainerAlpha)
		else MaterialTheme.colorScheme.surfaceContainer.copy(alpha = ContainerAlpha)

	@Composable
	fun contentColor(isCurrentTrack: Boolean): Color =
		if (isCurrentTrack) MaterialTheme.colorScheme.primary
		else MaterialTheme.colorScheme.onSurface

	@Composable
	fun supportingContentColor(isCurrentTrack: Boolean): Color =
		if (isCurrentTrack) MaterialTheme.colorScheme.primary.copy(alpha = 0.7f)
		else MaterialTheme.colorScheme.onSurfaceVariant
}

/**
 * The trailing status cluster shared by every song row: starred, unavailable-offline,
 * download state, the playing waveform, and the duration — in that order, so the same
 * information sits in the same place whichever list you are looking at.
 *
 * [trailing] appends a row-specific control (the queue's drag handle) after the cluster,
 * keeping its position stable no matter which status icons happen to be showing.
 */
@Composable
fun SongRowStatus(
	modifier: Modifier = Modifier,
	isStarred: Boolean = false,
	canPlay: Boolean = true,
	downloadStatus: DownloadStatus? = null,
	downloadProgress: Float = 0f,
	isCurrentTrack: Boolean = false,
	isPlaying: Boolean = false,
	duration: Duration? = null,
	durationColor: Color = MaterialTheme.colorScheme.onSurfaceVariant,
	trailing: (@Composable () -> Unit)? = null
) {
	Row(
		modifier = modifier,
		horizontalArrangement = Arrangement.spacedBy(SongRowDefaults.TrailingSpacing),
		verticalAlignment = Alignment.CenterVertically
	) {
		if (isStarred) {
			Icon(
				Icons.Filled.Star,
				contentDescription = null,
				modifier = Modifier.size(SongRowDefaults.StatusIconSize)
			)
		}
		if (!canPlay) {
			Icon(
				Icons.Outlined.Offline,
				contentDescription = stringResource(Res.string.info_not_available_offline),
				modifier = Modifier.size(SongRowDefaults.OfflineIconSize)
			)
		}
		// The playing row shows a waveform in this slot instead — a download chip there
		// would just compete with it.
		if (!isCurrentTrack) {
			when (downloadStatus) {
				// Waiting on a permit, not transferring — a spinner here would claim otherwise.
				DownloadStatus.QUEUED -> Icon(
					Icons.Outlined.Queue,
					contentDescription = stringResource(Res.string.info_download_queued),
					modifier = Modifier.size(SongRowDefaults.StatusIconSize),
					tint = MaterialTheme.colorScheme.onSurfaceVariant
				)

				DownloadStatus.DOWNLOADING -> CircularProgressIndicator(
					progress = { downloadProgress },
					modifier = Modifier.size(SongRowDefaults.StatusIconSize),
					strokeWidth = 2.dp
				)

				DownloadStatus.DOWNLOADED -> Icon(
					Icons.Outlined.Check,
					contentDescription = stringResource(Res.string.info_downloaded),
					modifier = Modifier.size(SongRowDefaults.StatusIconSize),
					tint = MaterialTheme.colorScheme.primary
				)

				DownloadStatus.FAILED -> Icon(
					Icons.Outlined.DownloadOff,
					contentDescription = stringResource(Res.string.info_download_failed),
					modifier = Modifier.size(SongRowDefaults.StatusIconSize),
					tint = MaterialTheme.colorScheme.error
				)

				else -> {}
			}
		}
		if (isCurrentTrack) {
			Waveform(isPlaying = isPlaying)
		}
		if (duration != null) {
			Text(
				text = duration.toHoursMinutesSeconds(),
				style = LocalTextStyle.current.copy(fontFeatureSettings = "tnum"),
				fontWeight = FontWeight(400),
				fontSize = 13.sp,
				color = durationColor,
				maxLines = 1
			)
		}
		trailing?.invoke()
	}
}
