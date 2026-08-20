package paige.navic.ui.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.BottomSheetDefaults
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheetProperties
import androidx.compose.material3.SheetValue
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.unit.Dp
import androidx.navigation3.runtime.NavEntry
import androidx.navigation3.scene.OverlayScene
import androidx.navigation3.scene.Scene
import androidx.navigation3.scene.SceneStrategy
import androidx.navigation3.scene.SceneStrategyScope
import org.koin.compose.koinInject
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.sheets.ModalBottomSheet
import paige.navic.ui.navigation.NowPlayingSceneStrategy.Companion.bottomSheet
import paige.navic.util.ui.rememberCoverColorScheme
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import paige.navic.ui.theme.NavicTheme

/** An [OverlayScene] that renders an [entry] within a [ModalBottomSheet]. */
@OptIn(ExperimentalMaterial3Api::class)
internal class NowPlayingScene<T : Any>(
	override val key: T,
	override val previousEntries: List<NavEntry<T>>,
	override val overlaidEntries: List<NavEntry<T>>,
	private val entry: NavEntry<T>,
	private val modalBottomSheetProperties: ModalBottomSheetProperties,
	private val sheetMaxWidth: Dp,
	private val onBack: () -> Unit,
	private val isTransparent: Boolean
) : OverlayScene<T> {

	override val entries: List<NavEntry<T>> = listOf(entry)

	override val content: @Composable (() -> Unit) = {
		NavicTheme(colorSchemeForCurrentSong()) {
			val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

			// Programmatic ANIMATED dismissal (e.g. auto-minimize when playback
			// transfers to another navi-connect device): removing the backstack
			// entry directly would destroy the sheet without its hide animation.
			val hideRequested by NowPlayingSheetController.hideRequests.collectAsState()
			LaunchedEffect(hideRequested) {
				if (hideRequested) {
					try {
						sheetState.hide()
					} finally {
						NowPlayingSheetController.consume()
						onBack()
					}
				}
			}

			ModalBottomSheet(
				containerColor = if (isTransparent) {
					Color.Transparent
				} else {
					MaterialTheme.colorScheme.surface
				},
				onDismissRequest = onBack,
				properties = modalBottomSheetProperties,
				sheetState = sheetState,
				sheetMaxWidth = sheetMaxWidth,
				contentWindowInsets = { WindowInsets() },
				dragHandle = null,
				shape = if (sheetState.targetValue == SheetValue.Expanded)
					RectangleShape
				else BottomSheetDefaults.ExpandedShape
			) {
				Box(Modifier.fillMaxSize()) {
					entry.Content()
				}
			}
		}
	}
}

/**
 * A [SceneStrategy] that displays entries that have added [bottomSheet] to their [NavEntry.metadata]
 * within a [ModalBottomSheet] instance.
 *
 * This strategy should always be added before any non-overlay scene strategies.
 */
@OptIn(ExperimentalMaterial3Api::class)
class NowPlayingSceneStrategy<T : Any> : SceneStrategy<T> {

	override fun SceneStrategyScope<T>.calculateScene(entries: List<NavEntry<T>>): Scene<T>? {
		val lastEntry = entries.lastOrNull()
		val bottomSheetProperties =
			lastEntry?.metadata?.get(PROPERTIES_KEY) as? ModalBottomSheetProperties
		val sheetMaxWidth = lastEntry?.metadata?.get(MAX_WIDTH_KEY) as? Dp
		val isTransparent = lastEntry?.metadata?.get(IS_TRANSPARENT_KEY) as? Boolean ?: false
		return bottomSheetProperties?.let { properties ->
			@Suppress("UNCHECKED_CAST")
			NowPlayingScene(
				key = lastEntry.contentKey as T,
				previousEntries = entries.dropLast(1),
				overlaidEntries = entries.dropLast(1),
				entry = lastEntry,
				modalBottomSheetProperties = properties,
				sheetMaxWidth = sheetMaxWidth ?: BottomSheetDefaults.SheetMaxWidth,
				onBack = onBack,
				isTransparent = isTransparent
			)
		}
	}

	companion object {
		/**
		 * Function to be called on the [NavEntry.metadata] to mark this entry as something that
		 * should be displayed within a [ModalBottomSheet].
		 *
		 * @param modalBottomSheetProperties properties that should be passed to the containing
		 * [ModalBottomSheet].
		 */
		@OptIn(ExperimentalMaterial3Api::class)
		fun bottomSheet(
			modalBottomSheetProperties: ModalBottomSheetProperties = ModalBottomSheetProperties(),
			maxWidth: Dp = BottomSheetDefaults.SheetMaxWidth,
			isTransparent: Boolean = false
		): Map<String, Any> = mapOf(
			PROPERTIES_KEY to modalBottomSheetProperties,
			MAX_WIDTH_KEY to maxWidth,
			IS_TRANSPARENT_KEY to isTransparent
		)

		internal const val PROPERTIES_KEY = "properties"
		internal const val MAX_WIDTH_KEY = "max_width"
		internal const val IS_TRANSPARENT_KEY = "is_transparent"
	}
}

/**
 * The sheet's palette, from the SAME extractor the detail pages use — this used to
 * be a second, drifting copy of that logic.
 *
 * `followArtworkBrightness = false` keeps the sheet's always-dark look: unlike a
 * detail page, it floats over the app rather than owning the whole page.
 */
@Composable
private fun colorSchemeForCurrentSong(): ColorScheme {
	val player = koinInject<MediaPlayerViewModel>()
	val playerState by player.steadyState.collectAsState()
	return rememberCoverColorScheme(
		coverArtId = playerState.currentSong?.coverArtId,
		isDark = true,
		followArtworkBrightness = false
	).scheme
}


/**
 * Lets app code dismiss the now-playing sheet WITH its hide animation. The
 * scene observes [hideRequests]; callers use [requestHide] instead of popping
 * the backstack entry directly.
 */
object NowPlayingSheetController {
	private val _hideRequests = MutableStateFlow(false)
	val hideRequests: StateFlow<Boolean> = _hideRequests.asStateFlow()

	fun requestHide() {
		_hideRequests.value = true
	}

	fun consume() {
		_hideRequests.value = false
	}
}
