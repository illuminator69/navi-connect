package paige.navic.ui.components.sheets

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.BottomSheetDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheetProperties
import androidx.compose.material3.SheetState
import androidx.compose.material3.contentColorFor
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import paige.navic.ui.theme.NavicTheme
import paige.navic.util.ui.CoverAmbient
import paige.navic.util.ui.SheetHideMotionSpec
import paige.navic.util.ui.SheetShowMotionSpec

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ModalBottomSheet(
	onDismissRequest: () -> Unit,
	modifier: Modifier = Modifier,
	sheetState: SheetState = rememberModalBottomSheetState(),
	sheetMaxWidth: Dp = BottomSheetDefaults.SheetMaxWidth,
	sheetGesturesEnabled: Boolean = true,
	shape: Shape = BottomSheetDefaults.ExpandedShape,
	containerColor: Color = BottomSheetDefaults.ContainerColor,
	contentColor: Color = contentColorFor(containerColor),
	tonalElevation: Dp = 0.dp,
	scrimColor: Color = BottomSheetDefaults.ScrimColor,
	dragHandle: @Composable (() -> Unit)? = { BottomSheetDefaults.DragHandle() },
	contentWindowInsets: @Composable () -> WindowInsets = { BottomSheetDefaults.modalWindowInsets },
	properties: ModalBottomSheetProperties = ModalBottomSheetProperties(),
	// When set, the sheet carries an Apple-Music-style dominant-colour wash
	// (Haze can't backdrop a ModalBottomSheet — separate window). The container is
	// the opaque muted top colour so the WHOLE surface (incl. the drag-handle
	// notch) blends; a top→bottom gradient is drawn over the content starting from
	// that same top colour (seamless), and the content is themed with the cover
	// scheme so its text/cards adapt. The Material surface still clips it to [shape].
	ambient: CoverAmbient? = null,
	content: @Composable ColumnScope.() -> Unit,
) {
	androidx.compose.material3.ModalBottomSheet(
		onDismissRequest = onDismissRequest,
		modifier = modifier,
		sheetState = sheetState,
		sheetMaxWidth = sheetMaxWidth,
		sheetGesturesEnabled = sheetGesturesEnabled,
		shape = shape,
		containerColor = ambient?.top ?: containerColor,
		// Drive the implicit content colour from the cover scheme too, so elements
		// that rely on LocalContentColor (not an explicit MaterialTheme colour) stay
		// legible on the tinted surface.
		contentColor = ambient?.onAmbient ?: contentColor,
		tonalElevation = tonalElevation,
		scrimColor = scrimColor,
		dragHandle = dragHandle,
		// For the ambient wash, don't let M3 inset the content (which would leave the
		// container colour showing as a lighter frame along the bottom/sides); the gradient
		// fills edge-to-edge and the insets are re-applied INSIDE it below.
		contentWindowInsets = if (ambient != null) ({ WindowInsets(0.dp, 0.dp, 0.dp, 0.dp) }) else contentWindowInsets,
		properties = properties,
		content = {
			if (ambient != null) {
				val insets = contentWindowInsets()
				NavicTheme(ambient.scheme, contentColor = ambient.onAmbient) {
					Column(
						modifier = Modifier
							.fillMaxWidth()
							.background(
								Brush.verticalGradient(
									listOf(ambient.top, ambient.top, ambient.bottom)
								)
							)
							// Apply the real content insets here, so the gradient reaches the true
							// bottom/side edges and no container-coloured border shows.
							.windowInsetsPadding(insets),
						content = content
					)
				}
			} else {
				content()
			}
		},
	)

	@Suppress("INVISIBLE_REFERENCE")
	LaunchedEffect(Unit) {
		sheetState.showMotionSpec = SheetShowMotionSpec
		sheetState.hideMotionSpec = SheetHideMotionSpec
	}
}
