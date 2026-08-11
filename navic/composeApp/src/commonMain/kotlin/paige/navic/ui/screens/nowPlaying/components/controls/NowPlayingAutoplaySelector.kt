package paige.navic.ui.screens.nowPlaying.components.controls

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.TransformOrigin
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntRect
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupPositionProvider
import dev.chrisbanes.haze.HazeState
import dev.chrisbanes.haze.blur.blurEffect
import dev.chrisbanes.haze.hazeEffect
import kotlinx.coroutines.delay
import org.koin.compose.koinInject
import paige.navic.domain.manager.AudioMuseManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.models.settings.AutoplayMode
import paige.navic.icons.Icons
import paige.navic.icons.outlined.KeyboardArrowDown
import paige.navic.icons.outlined.PlaylistPlay

/**
 * Compact autoplay-mode selector for the extended player, sitting between the
 * artwork and the progress bar. A rounded, heavily-frosted "button"; tapping it
 * springs open a popup list in the SAME frosted style, centred beneath the
 * button with centred items; tapping the button again closes it. Tier-2 modes
 * (Sonic Fingerprint, Mood Flow) only appear once the AudioMuse core API is
 * configured. Mode is read/written through RadioManager so the rest of the UI
 * (e.g. the adaptive mood background) reacts live.
 */
@Composable
fun NowPlayingAutoplaySelector(modifier: Modifier = Modifier, hazeState: HazeState? = null) {
	val radioManager = koinInject<RadioManager>()
	val audioMuseManager = koinInject<AudioMuseManager>()

	val mode by radioManager.autoplayMode.collectAsState()
	var expanded by remember { mutableStateOf(false) }
	// Guards the reopen race: tapping the button while open first fires the
	// popup's outside-tap dismiss, which would otherwise let the click re-open it.
	var justDismissed by remember { mutableStateOf(false) }

	LaunchedEffect(justDismissed) {
		if (justDismissed) {
			delay(250)
			justDismissed = false
		}
	}

	val modes = remember(audioMuseManager.isConfigured) {
		if (audioMuseManager.isConfigured) {
			listOf(
				AutoplayMode.Off,
				AutoplayMode.Similar,
				AutoplayMode.Fingerprint,
				AutoplayMode.Adaptive,
			)
		} else {
			listOf(AutoplayMode.Off, AutoplayMode.Similar)
		}
	}

	val shape = RoundedCornerShape(20.dp)
	// Heavy frost: a milky, mostly-opaque panel over the blurred background.
	val frost = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.82f)
	val frostBorder = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.14f)

	// Place the popup centred horizontally on the button, just below it.
	val belowCentered = remember {
		object : PopupPositionProvider {
			override fun calculatePosition(
				anchorBounds: IntRect,
				windowSize: IntSize,
				layoutDirection: LayoutDirection,
				popupContentSize: IntSize
			): IntOffset {
				val x = anchorBounds.left + (anchorBounds.width - popupContentSize.width) / 2
				val y = anchorBounds.bottom + 12
				return IntOffset(x.coerceAtLeast(0), y)
			}
		}
	}

	// Expressive spring for the open/close transition (origin = top centre, so it
	// grows down out of the button).
	val origin = TransformOrigin(0.5f, 0f)
	val visibleState = remember { MutableTransitionState(false) }
	visibleState.targetState = expanded

	Box(modifier) {
		Row(
			modifier = Modifier
				.clip(shape)
				// Real backdrop blur over the aurora when expressive blur is on;
				// the translucent fill stays as a tint for text contrast.
				.then(
					if (hazeState != null) {
						Modifier.hazeEffect(state = hazeState) { blurEffect { blurRadius = 20.dp } }
					} else {
						Modifier
					}
				)
				.background(if (hazeState != null) frost.copy(alpha = 0.5f) else frost)
				.border(1.dp, frostBorder, shape)
				.clickable {
					if (!justDismissed) expanded = !expanded
				}
				.padding(horizontal = 16.dp, vertical = 8.dp),
			verticalAlignment = Alignment.CenterVertically,
			horizontalArrangement = Arrangement.spacedBy(8.dp)
		) {
			Icon(
				imageVector = Icons.Outlined.PlaylistPlay,
				contentDescription = null,
				modifier = Modifier.size(18.dp)
			)
			Text(
				text = "Autoplay · ${mode.label}",
				style = MaterialTheme.typography.labelLarge,
				color = MaterialTheme.colorScheme.onSurface
			)
			Icon(
				imageVector = Icons.Outlined.KeyboardArrowDown,
				contentDescription = null,
				modifier = Modifier.size(18.dp)
			)
		}

		if (visibleState.currentState || visibleState.targetState) {
			Popup(
				popupPositionProvider = belowCentered,
				onDismissRequest = {
					expanded = false
					justDismissed = true
				}
			) {
				AnimatedVisibility(
					visibleState = visibleState,
					enter = scaleIn(
						animationSpec = spring(
							dampingRatio = Spring.DampingRatioMediumBouncy,
							stiffness = Spring.StiffnessMediumLow
						),
						transformOrigin = origin
					) + fadeIn(),
					exit = scaleOut(
						animationSpec = spring(stiffness = Spring.StiffnessMedium),
						transformOrigin = origin
					) + fadeOut()
				) {
					Column(
						modifier = Modifier
							.width(220.dp)
							.clip(shape)
							.background(frost)
							.border(1.dp, frostBorder, shape)
							.padding(vertical = 6.dp),
						horizontalAlignment = Alignment.CenterHorizontally
					) {
						modes.forEach { option ->
							val selected = option == mode
							Text(
								text = option.label,
								style = MaterialTheme.typography.labelLarge,
								color = if (selected) {
									MaterialTheme.colorScheme.primary
								} else {
									MaterialTheme.colorScheme.onSurface
								},
								textAlign = TextAlign.Center,
								modifier = Modifier
									.fillMaxWidth()
									.clip(RoundedCornerShape(14.dp))
									.clickable {
										radioManager.setAutoplayMode(option)
										expanded = false
									}
									.padding(horizontal = 24.dp, vertical = 12.dp)
							)
						}
					}
				}
			}
		}
	}
}
