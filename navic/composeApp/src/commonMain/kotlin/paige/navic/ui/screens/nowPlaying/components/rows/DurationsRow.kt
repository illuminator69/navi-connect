package paige.navic.ui.screens.nowPlaying.components.rows

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.unit.dp
import org.koin.compose.koinInject
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.util.core.toHoursMinutesSeconds
import kotlin.time.Duration.Companion.seconds

@Composable
fun NowPlayingDurationsRow() {
	val player = koinInject<MediaPlayerViewModel>()
	// Draws the elapsed time, so it takes the narrow [progress] flow; everything else it reads
	// comes off steadyState, which no longer emits on a playhead tick.
	val playerState by player.steadyState.collectAsState()
	val progress by player.progress.collectAsState()
	val duration = playerState.currentSong?.duration
	val style = MaterialTheme.typography.bodyMedium
		.copy(
			shadow = Shadow(
				color = MaterialTheme.colorScheme.inverseOnSurface,
				offset = Offset(0f, 4f),
				blurRadius = 10f
			)
		)
	val color = MaterialTheme.colorScheme.onSurfaceVariant
	Row(Modifier.padding(horizontal = 16.dp)) {
		when {
			duration == kotlin.time.Duration.ZERO -> {
				Text(text = "LIVE", color = color, style = style)
				Spacer(Modifier.weight(1f))
				Text(text = "∞", color = color, style = style)
			}
			duration != null -> {
				Text(
					text = ((duration.inWholeSeconds * progress).toDouble().seconds).toHoursMinutesSeconds(),
					color = color, style = style
				)
				Spacer(Modifier.weight(1f))
				Text(duration.toHoursMinutesSeconds(), color = color, style = style)
			}
			else -> {
				Text("--:--", color = color, style = style)
				Spacer(Modifier.weight(1f))
				Text("--:--", color = color, style = style)
			}
		}
	}
}
