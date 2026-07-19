package paige.navic.ui.screens.nowPlaying.components.controls

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.blur
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import androidx.compose.ui.util.lerp
import org.koin.compose.koinInject
import paige.navic.domain.manager.AudioMuseManager
import kotlin.math.PI
import kotlin.math.atan2

/**
 * Adaptive "Mood Flow" background for the extended player — a slow fluid aurora
 * of drifting colour blobs (Yandex-"Моя волна"-style), shown only in Adaptive
 * autoplay mode. The PALETTE follows the live mood: the AudioMuse alchemy
 * centroid's direction maps to a base hue (scale-independent), morphing smoothly
 * as the session's mood drifts. Motion is a gentle fixed drift for now.
 */
@Composable
fun AdaptiveMoodBackground(modifier: Modifier = Modifier) {
	val audioMuseManager = koinInject<AudioMuseManager>()
	val centroid by audioMuseManager.lastMoodCentroid.collectAsState()

	// Mood → base hue from the centroid's angle (independent of embedding scale).
	// No centroid yet → a calm purple-pink default.
	val targetHue = remember(centroid) {
		val c = centroid
		if (c != null && c.size >= 2) {
			((atan2(c[1], c[0]) / PI.toFloat()) * 180f + 360f) % 360f
		} else {
			300f
		}
	}
	// Morph to a new mood rather than cutting.
	val hue by animateFloatAsState(
		targetValue = targetHue,
		animationSpec = tween(durationMillis = 4000),
		label = "hue"
	)

	val transition = rememberInfiniteTransition(label = "mood")
	val a by transition.animateFloat(
		initialValue = 0f,
		targetValue = 1f,
		animationSpec = infiniteRepeatable(tween(11000, easing = LinearEasing), RepeatMode.Reverse),
		label = "a"
	)
	val b by transition.animateFloat(
		initialValue = 0f,
		targetValue = 1f,
		animationSpec = infiniteRepeatable(tween(15000, easing = LinearEasing), RepeatMode.Reverse),
		label = "b"
	)
	val c by transition.animateFloat(
		initialValue = 0f,
		targetValue = 1f,
		animationSpec = infiniteRepeatable(tween(9000, easing = LinearEasing), RepeatMode.Reverse),
		label = "c"
	)

	val base = Color.hsv(hue, 0.45f, 0.18f)
	val blob1 = Color.hsv(hue % 360f, 0.55f, 0.85f)
	val blob2 = Color.hsv((hue + 45f) % 360f, 0.6f, 0.8f)
	val blob3 = Color.hsv((hue + 315f) % 360f, 0.55f, 0.82f)

	Box(modifier.background(base)) {
		Canvas(Modifier.fillMaxSize().blur(90.dp)) {
			val w = size.width
			val h = size.height
			val r = size.maxDimension * 0.55f

			blob(blob1, Offset(lerp(w * 0.15f, w * 0.5f, a), lerp(h * 0.2f, h * 0.45f, b)), r)
			blob(blob2, Offset(lerp(w * 0.8f, w * 0.45f, b), lerp(h * 0.35f, h * 0.6f, c)), r)
			blob(blob3, Offset(lerp(w * 0.4f, w * 0.7f, c), lerp(h * 0.8f, h * 0.55f, a)), r)
		}
	}
}

private fun DrawScope.blob(color: Color, center: Offset, radius: Float) {
	drawCircle(
		brush = Brush.radialGradient(
			colors = listOf(color.copy(alpha = 0.85f), Color.Transparent),
			center = center,
			radius = radius
		),
		radius = radius,
		center = center
	)
}
