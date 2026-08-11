package paige.navic.ui.theme

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.ShapeDefaults
import androidx.compose.material3.Shapes
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.remember
import androidx.compose.ui.graphics.Color
import com.kyant.capsule.ContinuousRoundedRectangle
import org.koin.compose.koinInject
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.settings.AnimationStyle

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun NavicTheme(
	colorScheme: ColorScheme? = null,
	contentColor: Color? = null,
	content: @Composable () -> Unit
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val chosenTheme = preferenceManager.theme
	val chosenScheme = chosenTheme.colorScheme()
	val motionScheme = remember(preferenceManager.animationStyle) {
		when (preferenceManager.animationStyle) {
			AnimationStyle.Expressive -> MotionScheme.expressive()
			AnimationStyle.Standard -> MotionScheme.standard()
		}
	}
	MaterialExpressiveTheme(
		colorScheme = colorScheme
			?: chosenScheme,
		motionScheme = motionScheme,
		typography = typography(),
		shapes = Shapes(
			extraSmall = ContinuousRoundedRectangle(ShapeDefaults.ExtraSmall.topStart),
			small = ContinuousRoundedRectangle(ShapeDefaults.Small.topStart),
			medium = ContinuousRoundedRectangle(ShapeDefaults.Medium.topStart),
			large = ContinuousRoundedRectangle(ShapeDefaults.Large.topStart),
			extraLarge = ContinuousRoundedRectangle(ShapeDefaults.ExtraLarge.topStart),
			largeIncreased = ContinuousRoundedRectangle(ShapeDefaults.LargeIncreased.topStart),
			extraLargeIncreased = ContinuousRoundedRectangle(ShapeDefaults.ExtraLargeIncreased.topStart),
			extraExtraLarge = ContinuousRoundedRectangle(ShapeDefaults.ExtraExtraLarge.topStart)
		),
		// When a cover scheme is supplied, also drive the default content colour from
		// it. MaterialExpressiveTheme only sets the colorScheme, not LocalContentColor,
		// so text/icons with no explicit colour would otherwise keep the parent
		// (system) content colour and never adapt to the cover.
		content = if (colorScheme != null) {
			{
				CompositionLocalProvider(
					LocalContentColor provides (contentColor ?: colorScheme.onSurface),
					content = content
				)
			}
		} else {
			content
		}
	)
}
