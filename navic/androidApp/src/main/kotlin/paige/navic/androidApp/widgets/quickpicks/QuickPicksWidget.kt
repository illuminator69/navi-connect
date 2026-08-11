package paige.navic.androidApp.widgets.quickpicks

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.view.KeyEvent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.glance.ColorFilter
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalSize
import androidx.glance.action.clickable
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.actionSendBroadcast
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.appWidgetBackground
import androidx.glance.appwidget.components.CircleIconButton
import androidx.glance.appwidget.cornerRadius
import androidx.glance.background
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.ContentScale
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxHeight
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import paige.navic.androidApp.MainActivity
import paige.navic.androidApp.R
import paige.navic.androidApp.utils.appWidgetInnerCornerRadius
import paige.navic.androidApp.widgets.nowplaying.CoverArtCache
import paige.navic.androidApp.widgets.nowplaying.NowPlayingKeys
import paige.navic.androidApp.widgets.nowplaying.NowPlayingWidget

/**
 * 4×2 widget: a transport row over five album shortcuts, on a wash of the now-playing cover.
 *
 * The wash is why this widget exists in the shape it does — see `AmbientWash` for why the effect
 * is baked into a bitmap rather than composed, and why the play button below is a hardcoded white
 * circle instead of a themed [CircleIconButton]: the surface behind it is an arbitrary album
 * cover, so no theme colour can be relied on to contrast with it.
 */
class QuickPicksWidget : NowPlayingWidget() {

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
		val prefs = currentState<Preferences>()
		val coverKey = CoverArtCache.keyFor(prefs[NowPlayingKeys.artUrlKey])

		// Keyed on the track so the random slots reshuffle as you listen, and seeded from the
		// process cache for the same reason the cover is: every widget update is a fresh
		// composition, so without it the tiles emptied and re-fetched on an unrelated redraw.
		val tiles by produceState(QuickPickTilesCache.get(coverKey), coverKey) {
			if (value.isNotEmpty()) return@produceState
			value = loadQuickPickTiles(context).also { QuickPickTilesCache.put(coverKey, it) }
		}

		// Nothing playing means no cover to derive from, and inventing an ambient colour would
		// be a lie about what's on screen — fall back to the plain widget background, like
		// MiniPlayerWidget falls back to ic_note.
		val onWash = ambientWash != null
		val primaryText =
			if (onWash) ColorProvider(Color.White) else GlanceTheme.colors.onPrimaryContainer
		val secondaryText = ColorProvider(
			(if (onWash) Color.White else GlanceTheme.colors.onPrimaryContainer.getColor(context))
				.copy(alpha = .75f)
		)

		val size = LocalSize.current
		// The five tiles share the row's width minus the widget padding and four gaps; the
		// transport panel then takes ALL the height they leave. Sizing the panel off the
		// remainder rather than its own contents is what closes the dead space at the bottom —
		// a 4×2 cell is much taller than a control row needs.
		val tileSize = ((size.width - (WIDGET_PADDING * 2) - (TILE_GAP * 4)) / QUICK_PICK_TILE_COUNT)
			.coerceIn(40.dp, 76.dp)
		val panelHeight = (size.height - (WIDGET_PADDING * 2) - tileSize - TILE_GAP)
			.coerceAtLeast(72.dp)

		Box(
			modifier = GlanceModifier
				.fillMaxSize()
				.background(GlanceTheme.colors.widgetBackground)
				.appWidgetInnerCornerRadius(0.dp)
				.appWidgetBackground()
				.clickable(actionStartActivity(launchIntent(context)))
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

			Column(modifier = GlanceModifier.fillMaxSize().padding(WIDGET_PADDING)) {
				TransportPanel(
					context = context,
					isPlaying = isPlaying,
					title = title,
					artist = artist,
					bitmap = bitmap,
					height = panelHeight,
					framed = onWash,
					primaryText = primaryText,
					secondaryText = secondaryText
				)
				Spacer(modifier = GlanceModifier.height(TILE_GAP))
				TileRow(
					context = context,
					tiles = tiles,
					tileSize = tileSize
				)
			}
		}
	}

	/**
	 * Cover on the left, title/artist and the transport controls stacked to its right, filling
	 * whatever height the tile row leaves. [framed] draws the panel a shade lighter than the wash
	 * behind it, which is what separates it from the tiles without a divider; with no wash there
	 * is nothing to separate it from, so it is left flat.
	 */
	@Composable
	private fun TransportPanel(
		context: Context,
		isPlaying: Boolean,
		title: String,
		artist: String,
		bitmap: Bitmap?,
		height: Dp,
		framed: Boolean,
		primaryText: ColorProvider,
		secondaryText: ColorProvider
	) {
		val coverSize = (height - (PANEL_PADDING * 2)).coerceIn(48.dp, 96.dp)

		Row(
			modifier = GlanceModifier
				.fillMaxWidth()
				.height(height)
				.let { if (framed) it.background(ColorProvider(PANEL_TINT)) else it }
				.cornerRadius(20.dp)
				.padding(PANEL_PADDING),
			verticalAlignment = Alignment.CenterVertically
		) {
			Image(
				provider = bitmap?.let { ImageProvider(it) } ?: ImageProvider(R.drawable.ic_note),
				contentDescription = null,
				contentScale = ContentScale.Crop,
				modifier = GlanceModifier
					.size(coverSize)
					.background(GlanceTheme.colors.primaryContainer)
					.appWidgetInnerCornerRadius(12.dp)
			)

			Column(
				modifier = GlanceModifier
					.defaultWeight()
					.fillMaxHeight()
					.padding(start = 12.dp),
				verticalAlignment = Alignment.CenterVertically
			) {
				Text(
					text = title,
					style = TextStyle(
						color = primaryText,
						fontSize = 15.sp,
						fontWeight = FontWeight.Medium
					),
					maxLines = 1
				)
				Text(
					text = artist,
					style = TextStyle(color = secondaryText, fontSize = 13.sp),
					maxLines = 1
				)
				Spacer(modifier = GlanceModifier.height(6.dp))
				Row(
					modifier = GlanceModifier.fillMaxWidth(),
					horizontalAlignment = Alignment.CenterHorizontally,
					verticalAlignment = Alignment.CenterVertically
				) {
					CircleIconButton(
						imageProvider = ImageProvider(R.drawable.ic_previous),
						contentDescription = "Previous",
						contentColor = primaryText,
						onClick = actionSendBroadcast(
							createMediaIntent(context, KeyEvent.KEYCODE_MEDIA_PREVIOUS)
						),
						backgroundColor = null,
						modifier = GlanceModifier.size(38.dp)
					)
					Spacer(modifier = GlanceModifier.width(8.dp))
					PlayPauseButton(context, isPlaying)
					Spacer(modifier = GlanceModifier.width(8.dp))
					CircleIconButton(
						imageProvider = ImageProvider(R.drawable.ic_next),
						contentDescription = "Next",
						contentColor = primaryText,
						onClick = actionSendBroadcast(
							createMediaIntent(context, KeyEvent.KEYCODE_MEDIA_NEXT)
						),
						backgroundColor = null,
						modifier = GlanceModifier.size(38.dp)
					)
				}
			}
		}
	}

	/**
	 * Fixed white circle, dark glyph. Deliberately not [CircleIconButton] — its themed colours
	 * are picked against the app's surfaces, and this one sits on a blurred album cover that can
	 * be any hue or brightness. White-on-black is the only pair that stays readable over all of
	 * them, so it is hardcoded rather than themed.
	 */
	@Composable
	private fun PlayPauseButton(context: Context, isPlaying: Boolean) {
		Box(
			modifier = GlanceModifier
				.size(34.dp)
				.cornerRadius(17.dp)
				.background(ColorProvider(Color.White))
				.clickable(
					actionSendBroadcast(
						createMediaIntent(context, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE)
					)
				),
			contentAlignment = Alignment.Center
		) {
			Image(
				provider = ImageProvider(
					if (isPlaying) R.drawable.ic_pause else R.drawable.ic_play
				),
				contentDescription = if (isPlaying) "Pause" else "Play",
				colorFilter = ColorFilter.tint(ColorProvider(GLYPH_ON_WHITE)),
				modifier = GlanceModifier.size(18.dp)
			)
		}
	}

	/**
	 * Bare covers, no captions. The album name lived under each tile until it turned out that at
	 * a fifth of the widget's width it ellipsised almost every title into nonsense
	 * ("100% Electro…", "Stranger in t…") while eating the height that left the widget looking
	 * half-empty. The cover is the recognisable part; the name is one tap away.
	 */
	@Composable
	private fun TileRow(
		context: Context,
		tiles: List<QuickPickTile>,
		tileSize: Dp
	) {
		Row(
			modifier = GlanceModifier.fillMaxWidth(),
			verticalAlignment = Alignment.CenterVertically
		) {
			tiles.forEachIndexed { index, tile ->
				if (index > 0) Spacer(modifier = GlanceModifier.width(TILE_GAP))
				Box(
					modifier = GlanceModifier.defaultWeight(),
					contentAlignment = Alignment.Center
				) {
					Image(
						provider = tile.cover?.let { ImageProvider(it) }
							?: ImageProvider(R.drawable.ic_note),
						contentDescription = tile.title,
						contentScale = ContentScale.Crop,
						modifier = GlanceModifier
							.size(tileSize)
							.background(GlanceTheme.colors.primaryContainer)
							.appWidgetInnerCornerRadius(14.dp)
							.clickable(actionStartActivity(albumIntent(context, tile.albumId)))
					)
				}
			}
		}
	}

	/**
	 * Opens the album behind a tile.
	 *
	 * The `data` uri is not decoration: `PendingIntent` equality ignores extras, so five intents
	 * differing only by an album-id extra would collapse into one and every tile would open the
	 * same album. `MainActivity` reads it back in `onCreate`/`onNewIntent`.
	 */
	private fun albumIntent(context: Context, albumId: String) =
		Intent(context, MainActivity::class.java).apply {
			action = Intent.ACTION_VIEW
			data = Uri.parse("navic://album/$albumId")
			addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
		}

	private companion object {
		val WIDGET_PADDING = 12.dp
		val PANEL_PADDING = 10.dp
		val TILE_GAP = 8.dp
		val PANEL_TINT = Color.White.copy(alpha = .12f)
		val GLYPH_ON_WHITE = Color(0xFF141218)
	}
}
