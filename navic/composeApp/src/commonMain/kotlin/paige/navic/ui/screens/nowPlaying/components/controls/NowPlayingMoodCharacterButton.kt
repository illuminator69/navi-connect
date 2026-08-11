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
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
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
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.models.settings.AutoplayMode
import paige.navic.domain.models.settings.MoodCharacter
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Speed

/**
 * Small circular frosted button that sits next to [NowPlayingAutoplaySelector]
 * ONLY while Mood Flow (Adaptive) is the active autoplay mode. Tapping it springs
 * open a popup (same frosted style as the autoplay selector) to pick the Mood
 * Flow tuning preset ([MoodCharacter]) without going to Settings. Writes
 * `preferenceManager.moodCharacter`, which RadioManager reads fresh on each
 * Adaptive top-up — no extra wiring.
 */
@Composable
fun NowPlayingMoodCharacterButton(modifier: Modifier = Modifier, hazeState: HazeState? = null) {
	val radioManager = koinInject<RadioManager>()
	val preferenceManager = koinInject<PreferenceManager>()

	val mode by radioManager.autoplayMode.collectAsState()
	if (mode != AutoplayMode.Adaptive) return

	val character = preferenceManager.moodCharacter

	var expanded by remember { mutableStateOf(false) }
	// Same reopen-race guard as the autoplay selector.
	var justDismissed by remember { mutableStateOf(false) }

	LaunchedEffect(justDismissed) {
		if (justDismissed) {
			delay(250)
			justDismissed = false
		}
	}

	val popupShape = RoundedCornerShape(20.dp)
	val frost = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.82f)
	val frostBorder = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.14f)

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

	val origin = TransformOrigin(0.5f, 0f)
	val visibleState = remember { MutableTransitionState(false) }
	visibleState.targetState = expanded

	Box(modifier) {
		Box(
			modifier = Modifier
				.size(38.dp)
				.clip(CircleShape)
				.then(
					if (hazeState != null) {
						Modifier.hazeEffect(state = hazeState) { blurEffect { blurRadius = 20.dp } }
					} else {
						Modifier
					}
				)
				.background(if (hazeState != null) frost.copy(alpha = 0.5f) else frost)
				.border(1.dp, frostBorder, CircleShape)
				.clickable {
					if (!justDismissed) expanded = !expanded
				},
			contentAlignment = Alignment.Center
		) {
			Icon(
				imageVector = Icons.Outlined.Speed,
				contentDescription = "Mood Flow character",
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
							.clip(popupShape)
							.background(frost)
							.border(1.dp, frostBorder, popupShape)
							.padding(vertical = 6.dp),
						horizontalAlignment = Alignment.CenterHorizontally
					) {
						MoodCharacter.entries.forEach { option ->
							val selected = option == character
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
										preferenceManager.moodCharacter = option
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
