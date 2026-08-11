package paige.navic.androidApp.widgets.miniplayer

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.view.KeyEvent
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.action.clickable
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.actionSendBroadcast
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.appWidgetBackground
import androidx.glance.appwidget.components.CircleIconButton
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.ContentScale
import androidx.glance.layout.Row
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import paige.navic.androidApp.R
import paige.navic.androidApp.utils.appWidgetInnerCornerRadius
import paige.navic.androidApp.widgets.nowplaying.NowPlayingWidget

class MiniPlayerWidget : NowPlayingWidget() {

	override val sizeMode = SizeMode.Exact
	override val stateDefinition = PreferencesGlanceStateDefinition

	@SuppressLint("RestrictedApi")
	@Composable
	override fun Content(
		context: Context,
		isPlaying: Boolean,
		title: String,
		artist: String,
		bitmap: Bitmap?,
		ambientWash: Bitmap?
	) {
		// White reads on the wash (a 40%-scrimmed cover) whatever the album is; the themed
		// colours only make sense on the flat background used when nothing is playing.
		val onWash = ambientWash != null
		val contentColor =
			if (onWash) ColorProvider(Color.White) else GlanceTheme.colors.onPrimaryContainer

		// `appWidgetBackground()` stamps `android.R.id.background` on the view, and a RemoteViews
		// tree may only carry that id once — putting it on both the wash and the row is what made
		// the launcher give up with "can't show content". It belongs on the outer box alone.
		Box(
			modifier = GlanceModifier
				.fillMaxSize()
				.height(88.dp)
				.background(GlanceTheme.colors.widgetBackground)
				.clickable(actionStartActivity(launchIntent(context)))
				.appWidgetInnerCornerRadius(0.dp)
				.appWidgetBackground(),
			contentAlignment = Alignment.Center
		) {
			ambientWash?.let {
				Image(
					provider = ImageProvider(it),
					contentDescription = null,
					contentScale = ContentScale.Crop,
					modifier = GlanceModifier
						.fillMaxSize()
						.appWidgetInnerCornerRadius(0.dp)
				)
			}

			Row(
				modifier = GlanceModifier
					.fillMaxSize()
					.padding(12.dp),
				verticalAlignment = Alignment.CenterVertically
			) {
				Image(
					provider = bitmap?.let { ImageProvider(it) }
						?: ImageProvider(R.drawable.ic_note),
					contentDescription = null,
					contentScale = ContentScale.Crop,
					modifier = GlanceModifier
						.size(64.dp)
						.background(GlanceTheme.colors.primaryContainer)
						.appWidgetInnerCornerRadius(12.dp)
				)

				Column(modifier = GlanceModifier.defaultWeight().padding(horizontal = 12.dp)) {
					Text(
						text = title,
						style = TextStyle(color = contentColor, fontSize = 16.sp),
						maxLines = 1
					)
					Text(
						text = artist,
						style = TextStyle(
							color = ColorProvider(
								(if (onWash) Color.White
								else GlanceTheme.colors.onPrimaryContainer.getColor(context))
									.copy(alpha = .8f)
							),
							fontSize = 14.sp
						),
						maxLines = 1
					)
				}

				CircleIconButton(
					imageProvider = ImageProvider(R.drawable.ic_previous),
					contentDescription = "Previous",
					contentColor = contentColor,
					onClick = actionSendBroadcast(
						createMediaIntent(
							context,
							KeyEvent.KEYCODE_MEDIA_PREVIOUS
						)
					),
					backgroundColor = null
				)
				CircleIconButton(
					imageProvider = ImageProvider(if (isPlaying) R.drawable.ic_pause else R.drawable.ic_play),
					contentDescription = if (isPlaying) "Pause" else "Play",
					contentColor = contentColor,
					onClick = actionSendBroadcast(
						createMediaIntent(
							context,
							KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
						)
					),
					backgroundColor = null
				)
				CircleIconButton(
					imageProvider = ImageProvider(R.drawable.ic_next),
					contentDescription = "Next",
					contentColor = contentColor,
					onClick = actionSendBroadcast(
						createMediaIntent(
							context,
							KeyEvent.KEYCODE_MEDIA_NEXT
						)
					),
					backgroundColor = null
				)
			}
		}
	}
}
