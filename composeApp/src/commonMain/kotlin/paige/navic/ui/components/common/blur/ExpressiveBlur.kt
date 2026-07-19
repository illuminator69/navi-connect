package paige.navic.ui.components.common.blur

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.chrisbanes.haze.HazeState
import dev.chrisbanes.haze.blur.blurEffect
import dev.chrisbanes.haze.hazeEffect
import dev.chrisbanes.haze.hazeSource

/**
 * Thin, swappable wrapper over Haze for "expressive" frosted-glass chrome. The
 * blur engine and its gating live here so call sites stay uniform: an overlay
 * ([expressiveBlurEffect]) blurs the live content marked as the backdrop
 * ([expressiveBlurSource]). Both no-op when [ExpressiveBlurState.enabled] is
 * false (the Appearance "Expressive blur" toggle / unsupported platform), so
 * callers transparently fall back to their existing translucent surface.
 *
 * Self-blur ("blur this element's own background") is NOT this — keep using
 * native `Modifier.blur` for that (album art / mood aurora).
 */
class ExpressiveBlurState internal constructor(
	val hazeState: HazeState,
	val enabled: Boolean
)

/**
 * A shared blur state provided once near the app root so the (per-screen) bottom
 * bar can sample the same backdrop as the screen content without prop-threading.
 * Defaults to a disabled state, so reading it outside a provider is a safe no-op.
 */
val LocalExpressiveBlur = staticCompositionLocalOf { ExpressiveBlurState(HazeState(), false) }

/** Remembers a blur state bound to [enabled] (typically PreferenceManager.expressiveBlur). */
@Composable
fun rememberExpressiveBlur(enabled: Boolean): ExpressiveBlurState {
	val hazeState = remember { HazeState() }
	return remember(enabled, hazeState) { ExpressiveBlurState(hazeState, enabled) }
}

/** Marks this content as the backdrop sampled by [expressiveBlurEffect]. No-op when disabled. */
fun Modifier.expressiveBlurSource(blur: ExpressiveBlurState): Modifier =
	if (blur.enabled) this.hazeSource(blur.hazeState) else this

/** Frosts this overlay by blurring the [expressiveBlurSource] behind it. No-op when disabled. */
fun Modifier.expressiveBlurEffect(blur: ExpressiveBlurState, radius: Dp = 24.dp): Modifier =
	if (blur.enabled) {
		this.hazeEffect(state = blur.hazeState) { blurEffect { blurRadius = radius } }
	} else {
		this
	}
