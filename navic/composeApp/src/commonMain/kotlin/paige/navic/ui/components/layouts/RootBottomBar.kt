package paige.navic.ui.components.layouts

import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.graphicsLayer
import org.koin.compose.koinInject
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.settings.BottomBarCollapseMode
import paige.navic.domain.models.settings.MiniPlayerStyle
import paige.navic.ui.components.common.blur.LocalExpressiveBlur
import paige.navic.ui.components.common.blur.expressiveBlurEffect
import paige.navic.util.ui.easedVerticalGradient

@Composable
fun RootBottomBar(
	scrolled: Boolean,
	modifier: Modifier = Modifier,
	shadows: Boolean = true,
	hideMiniPlayer: Boolean = false,
	bottomBarWindowInsets: WindowInsets = NavigationBarDefaults.windowInsets,
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val expressiveBlur = LocalExpressiveBlur.current
	val detached = preferenceManager.miniPlayerStyle == MiniPlayerStyle.Detached
	val scrolled =
		scrolled && preferenceManager.bottomBarCollapseMode == BottomBarCollapseMode.OnScroll
	val progress by animateFloatAsState(
		targetValue = if (scrolled) 0f else 1f,
		animationSpec = spring(
			dampingRatio = Spring.DampingRatioLowBouncy,
			stiffness = Spring.StiffnessMediumLow
		)
	)
	// NOT `by`: this is read in the DRAW phase below, not at composition.
	//
	// Read as a plain value here, it recomposed this whole function — mini-player included, and
	// with Expressive blur on that means rebuilding the pill's blur node — on every frame of a
	// 600 ms tween, which fires at every scroll start and every scroll stop. `progress` above is
	// already deferred correctly into its `graphicsLayer` lambdas; this was the one that leaked.
	val shadowFadeProgress = animateFloatAsState(
		targetValue = if (scrolled || !shadows) 0f else 1f,
		animationSpec = tween(durationMillis = 600)
	)
	val surfaceColor = MaterialTheme.colorScheme.surface
	Column(
		// Frost the backdrop (the screen content marked as the app's blur source) behind the
		// bar when Expressive blur is on — but ONLY for the docked/full-bleed style. When
		// detached, the mini-player pill (and the floating capsule nav) frost their own shapes,
		// so a full-width band here would defeat the floating look.
		modifier = modifier
			.then(if (detached) Modifier else Modifier.expressiveBlurEffect(expressiveBlur))
			.then(
				if (detached)
					Modifier.drawBehind {
						drawRect(
							Brush.easedVerticalGradient(
								color = surfaceColor.copy(alpha = shadowFadeProgress.value)
							)
						)
					}
				else Modifier
			)
	) {
		// The single MiniPlayer mirrors the remote session when another
		// navi-connect device is active (see MiniPlayer / MediaPlayerViewModel).
		if (!hideMiniPlayer) MiniPlayer(
			modifier = Modifier.graphicsLayer {
				alpha = progress.coerceIn(0f..1f)
				translationY = ((1f - progress) * (size.height * 2)).coerceAtLeast(
					if (preferenceManager.miniPlayerStyle == MiniPlayerStyle.Detached) -2048f else 0f
				)
			},
			enabled = !scrolled
		)
		BottomBar(
			containerColor = when {
				preferenceManager.miniPlayerStyle == MiniPlayerStyle.Detached ->
					NavigationBarDefaults.containerColor.copy(alpha = 0f)
				// Let the frosted backdrop show through the nav bar when blur is on.
				expressiveBlur.enabled -> NavigationBarDefaults.containerColor.copy(alpha = 0.55f)
				else -> NavigationBarDefaults.containerColor
			},
			windowInsets = bottomBarWindowInsets,
			modifier = Modifier.graphicsLayer {
				alpha = progress.coerceIn(0f..1f)
				translationY = ((1f - progress) * size.height).coerceAtLeast(
					if (preferenceManager.miniPlayerStyle == MiniPlayerStyle.Detached) -2048f else 0f
				)
			},
			enabled = !scrolled
		)
	}
}
