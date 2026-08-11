package paige.navic.ui.components.common

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp

/**
 * Apple-Music-style ambient background: a vertical wash from the artwork's
 * dominant [seed] colour into a darker (or lighter) shade. Meant to sit BEHIND a
 * detail screen's / sheet's content, with the cover hero drawn over its top.
 * The colour eases in as the seed resolves (kmpalette extraction is async).
 */
@Composable
fun ArtAmbientBackground(
	seed: Color,
	modifier: Modifier = Modifier,
	isDark: Boolean = true,
) {
	val animated by animateColorAsState(seed, animationSpec = tween(450))
	// Mute the (often bright) dominant colour toward dark so it reads as an ambient
	// wash with legible light text — like Apple Music — rather than the raw cover hue.
	val target = if (isDark) Color.Black else Color.White
	val top = lerp(animated, target, if (isDark) 0.32f else 0.66f)
	val bottom = lerp(animated, target, if (isDark) 0.60f else 0.50f)
	Box(
		modifier = modifier
			.fillMaxSize()
			.background(Brush.verticalGradient(listOf(top, top, bottom)))
	)
}
