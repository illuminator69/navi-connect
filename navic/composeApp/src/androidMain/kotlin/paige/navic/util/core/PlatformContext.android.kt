package paige.navic.util.core

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.view.SoundEffectConstants
import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.expressiveLightColorScheme
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.core.app.ActivityCompat.requestPermissions
import androidx.core.view.WindowCompat
import org.koin.compose.koinInject
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.settings.ThemeMode

@OptIn(
	ExperimentalMaterial3WindowSizeClassApi::class,
	ExperimentalMaterial3ExpressiveApi::class
)
@Composable
actual fun rememberPlatformContext(): PlatformContext {
	val view = LocalView.current
	val context = LocalContext.current
	val inDarkTheme = isSystemInDarkTheme()
	val preferenceManager = koinInject<PreferenceManager>()
	val isDark = remember(preferenceManager.themeMode) {
		when (preferenceManager.themeMode) {
			ThemeMode.System -> inDarkTheme
			ThemeMode.Dark -> true
			ThemeMode.Light -> false
		}
	}
	val activity = LocalActivity.current!!
	val sizeClass = calculateWindowSizeClass(activity)
	SideEffect {
		(view.context as? Activity)?.window?.let { window ->
			WindowCompat.getInsetsController(window, view)
				.isAppearanceLightStatusBars = !isDark
		}
	}
	return remember(isDark, sizeClass) {
		object : PlatformContext {
			// TODO: remove this and usages of it as compose will do it by default in alpha03
			override fun clickSound() {
				view.playSoundEffect(SoundEffectConstants.CLICK)
			}

			override fun checkLocalNetworkPermission() {
				if (Build.VERSION.SDK_INT < 37) return

				val hasPermission = context.checkSelfPermission(
					Manifest.permission.ACCESS_LOCAL_NETWORK
				) == PackageManager.PERMISSION_GRANTED
				if (hasPermission) return

				// The hosting Activity, not `view.context`: in Compose that context is usually a
				// ContextThemeWrapper, and casting it blind used to risk an NPE — now that this
				// runs on every launch rather than only on a login tap, that would be a crash.
				requestPermissions(
					activity,
					arrayOf(Manifest.permission.ACCESS_LOCAL_NETWORK),
					500
				)
			}

			override val platformType = PlatformType.Android
			override val name = "Android ${Build.VERSION.SDK_INT}"
			override val appVersion: String =
				context.packageManager
					.getPackageInfo(context.packageName, 0)
					.versionName.toString()
			override val colorScheme
				get() = if (Build.VERSION.SDK_INT >= 31)
					if (isDark)
						dynamicDarkColorScheme(context)
					else dynamicLightColorScheme(context)
				else
					if (isDark)
						darkColorScheme()
					else expressiveLightColorScheme()
			override val sizeClass = sizeClass
		}
	}
}

actual fun <T> synchronized(lock: Any, block: () -> T): T = kotlin.synchronized(lock, block)

@Composable
actual fun ForceSystemBars(isDarkBackground: Boolean) {
	val view = LocalView.current
	DisposableEffect(isDarkBackground) {
		val controller = (view.context as? Activity)?.window?.let {
			WindowCompat.getInsetsController(it, view)
		}
		val previous = controller?.isAppearanceLightStatusBars ?: true
		// Light (white) icons over a dark background, dark icons over a light one.
		controller?.isAppearanceLightStatusBars = !isDarkBackground
		onDispose {
			controller?.isAppearanceLightStatusBars = previous
		}
	}
}

actual val currentPlatformType: PlatformType = PlatformType.Android
