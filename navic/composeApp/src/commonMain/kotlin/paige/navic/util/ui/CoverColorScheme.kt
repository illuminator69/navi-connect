package paige.navic.util.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.abs
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.kmpalette.generatePalette
import com.kmpalette.loader.rememberNetworkLoader
import com.kmpalette.palette.graphics.Palette
import com.kmpalette.palette.graphics.Palette.Swatch
import com.materialkolor.PaletteStyle
import com.materialkolor.dynamicColorScheme
import com.materialkolor.dynamiccolor.ColorSpec
import io.ktor.client.HttpClient
import io.ktor.client.plugins.HttpTimeout
import io.ktor.http.Url
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import org.koin.compose.koinInject
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.models.settings.ThemeMode
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.BlendBackground

/**
 * One client for ALL cover-art palette extraction. Built OUTSIDE composition on
 * purpose: constructing an [HttpClient] inside a `@Composable` allocated a fresh
 * client — and its connection pool + engine threads — on every recomposition, and
 * nothing ever closed them.
 */
internal val paletteHttpClient: HttpClient by lazy {
	HttpClient {
		install(HttpTimeout) {
			requestTimeoutMillis = 60_000
			connectTimeoutMillis = 60_000
			socketTimeoutMillis = 60_000
		}
	}
}

/**
 * Extracted palettes, kept for the app's lifetime and keyed by cover id.
 *
 * Without this, every composable that asks for a cover's colours re-downloads and re-quantises the
 * image — and a composable inside a lazy list gets disposed and recreated as you scroll, so its
 * colours would pop back to the neutral default and fade in again on every pass. Bounded because a
 * long session browses a lot of covers; entries are cheap (a handful of swatches each).
 *
 * Only ever touched from the main thread (composition + `LaunchedEffect` bodies).
 */
private const val PALETTE_CACHE_MAX = 128
private val paletteCache = object {
	// Insertion-ordered, so the first key is the oldest.
	private val entries = mutableMapOf<String, Palette>()

	operator fun get(id: String): Palette? = entries[id]

	operator fun set(id: String, palette: Palette) {
		entries[id] = palette
		while (entries.size > PALETTE_CACHE_MAX) {
			entries.remove(entries.keys.first())
		}
	}
}

/**
 * Derived Material schemes, keyed by the inputs that produce them.
 *
 * [paletteCache] above stops the artwork being re-downloaded and re-quantised, but the step AFTER
 * it — materialKolor's HCT/CAM16 derivation of ~30 colour roles from the seed — is a `remember`
 * inside `rememberDynamicColorScheme`, so it dies with the composition that made it. That matters
 * because `RootBottomBar` (and so the mini-player, which reads the now-playing cover) is built
 * fresh inside EVERY screen's own Scaffold: navigating anywhere tore its composition down and paid
 * for the whole derivation again on the UI thread, mid-transition, for a song that hadn't changed.
 *
 * Small, because the key is (seed, brightness, style) and a session only plays so many covers.
 * Main-thread only, like [paletteCache].
 */
private const val SCHEME_CACHE_MAX = 64
private data class SchemeKey(val seed: Color, val isDark: Boolean, val style: PaletteStyle)
private val schemeCache = object {
	private val entries = mutableMapOf<SchemeKey, ColorScheme>()

	operator fun get(key: SchemeKey): ColorScheme? = entries[key]

	operator fun set(key: SchemeKey, scheme: ColorScheme) {
		entries[key] = scheme
		while (entries.size > SCHEME_CACHE_MAX) {
			entries.remove(entries.keys.first())
		}
	}
}

/** A palette swatch's packed ARGB as a Compose [Color]. */
private fun Swatch.toColor(): Color = Color(rgb)

/** A readable Material scheme derived from artwork plus the extracted seed color. */
data class CoverColors(
	val scheme: ColorScheme,
	val seed: Color,
	/** Resolved page brightness — follows the ARTWORK's luminance, not the app theme. */
	val isDark: Boolean
)

/**
 * Builds a readable Material 3 [ColorScheme] from a cover-art / artist image's
 * dominant color (Apple-Music-style art theming). kmpalette extracts the seed,
 * materialKolor derives the full scheme — so on-colors stay legible over
 * unpredictable artwork. Generalizes the now-playing sheet's `colorSchemeForCurrentSong`.
 *
 * Extraction is async: [CoverColors.seed]/[scheme] start at a neutral default and
 * update once the (downsized) image loads. Caches by [coverArtId] via remember.
 */
@Composable
fun rememberCoverColorScheme(
	coverArtId: String?,
	isDark: Boolean = true,
	initialSeed: Color? = null,
	// When false, the scheme keeps the passed [isDark] (app) brightness instead of following
	// the artwork's luminance. Used by the library home, whose page brightness should stay put
	// (only the accent hues adapt to the now-playing song) rather than flipping per cover.
	followArtworkBrightness: Boolean = true
): CoverColors {
	val sessionManager = koinInject<SessionManager>()
	val coverUri = remember(coverArtId) {
		coverArtId?.let { sessionManager.getCoverArtUrl(it) }
	}
	val networkLoader = rememberNetworkLoader(paletteHttpClient)
	// ONE extraction per cover, and every colour below is read off that one palette — so a cover
	// resolves in a single step. (Running the wash and the accent as two separate
	// `DominantColorState`s made covers visibly change colour TWICE; kmpalette's `PaletteState`
	// wrapper never resolved at all. Loading the bitmap and building the palette directly is both
	// simpler and the thing we can actually reason about.)
	//
	// NOT keyed on the cover id: the previous palette stays put until the new one is ready, so
	// changing songs eases from the old colour instead of flashing through the neutral default.
	var palette by remember { mutableStateOf(coverArtId?.let { paletteCache[it] }) }

	LaunchedEffect(coverUri, coverArtId) {
		val id = coverArtId ?: return@LaunchedEffect
		val uri = coverUri ?: return@LaunchedEffect
		paletteCache[id]?.let {
			palette = it
			return@LaunchedEffect
		}
		// LaunchedEffect bodies run on the composition dispatcher (main). The Ktor fetch suspends
		// off it on its own, but the quantisation afterwards is plain CPU work, and it lands
		// exactly when the artwork changes — i.e. the moment the colour adaptation is visible.
		val result = runCatching {
			withContext(Dispatchers.Default) {
				networkLoader.load(Url("$uri&size=128")).generatePalette()
			}
		}.getOrNull() ?: return@LaunchedEffect
		paletteCache[id] = result
		palette = result
	}

	val fallback = MaterialTheme.colorScheme.surface
	// The most POPULOUS colour. It decides page BRIGHTNESS — a mostly-black cover has to give a
	// dark page — but on its own it's a poor accent: on this artwork it's the black hair, not the
	// yellow that the cover actually reads as.
	val dominant = palette?.dominantSwatch?.toColor() ?: initialSeed ?: fallback
	// The most populous COLOURFUL swatch: washed-out and near-black/near-white swatches are
	// rejected, so what survives is the colour a human would name the cover by (the yellow).
	// A genuinely greyscale cover has no survivor and falls back to the dominant, staying neutral
	// rather than having a hue invented for it.
	val vivid = remember(palette) {
		palette?.swatches
			?.filter { it.hsl[1] >= 0.25f && it.hsl[2] in 0.25f..0.85f }
			?.maxByOrNull { it.population }
			?.toColor()
	}
	// The WASH is the dominant colour pulled a third of the way toward the vivid one: enough for
	// the page to actually read as the cover's colour, not so much that a saturated cover turns
	// the background into a glare. This is what "expressive" means here — a black-and-yellow cover
	// should give a dark page that is unmistakably YELLOW-dark, not a flat grey one.
	val seed = if (vivid != null) lerp(dominant, vivid, 0.35f) else dominant
	// The ACCENT (buttons, primary) is the vivid swatch, saturation-boosted so even muted artwork
	// yields an obvious cover colour rather than a washed near-grey `primary`.
	val accentSeed = (vivid ?: dominant).boostedAccent()
	// Page brightness FOLLOWS THE ARTWORK (Apple-Music style): a dark cover gives a dark
	// immersive page (light text), a light cover a light page (dark text) — independent of
	// the app theme mode (a dark album on a light page was the "doesn't match" mismatch).
	// Keyed on the DOMINANT colour, never the vivid one: a bright accent on a dark cover must not
	// flip the whole page to light.
	val coverIsDark = if (!followArtworkBrightness) isDark
		else if (palette != null) dominant.luminance() < 0.5f else isDark
	// Content keeps colourful covers faithful AND leaves a true-greyscale cover naturally
	// grey — it doesn't amplify chroma like Vibrant (which invented a teal from a B&W cover),
	// so no Monochrome special-case is needed; a dark scheme already yields a bright `primary`.
	val style = if (coverUri != null) PaletteStyle.Content else PaletteStyle.Monochrome
	// Seed the SCHEME from the vivid swatch (saturation-boosted) so muted/dark artwork still
	// yields an OBVIOUS cover accent (Symfonium-like) rather than a washed near-grey `primary`.
	//
	// The non-composable `dynamicColorScheme` behind our own [schemeCache], rather than
	// `rememberDynamicColorScheme`: its remember dies with the composition, and the mini-player's
	// composition is destroyed on every navigation, so the HCT derivation was being repeated on
	// the UI thread mid-transition for a cover that hadn't changed. The trailing `.copy` used to
	// sit outside any remember too — and since ColorScheme has identity equality, that alone
	// re-published LocalColorScheme (and so invalidated the whole subtree) at every
	// `NavicTheme(coverColors.scheme)` call site, on every recomposition.
	//
	// materialKolor gives LIGHT schemes a near-neutral (almost black) onSurface, so light
	// pages read as "just black text". Tint the content colours toward the cover's `primary`
	// so text carries the artwork's hue on light AND dark pages (matching the queue's look).
	val scheme = remember(accentSeed, coverIsDark, style) {
		val key = SchemeKey(accentSeed, coverIsDark, style)
		schemeCache[key] ?: run {
			val raw = dynamicColorScheme(
				seedColor = accentSeed,
				isDark = coverIsDark,
				style = style,
				specVersion = ColorSpec.SpecVersion.SPEC_2021
			)
			raw.copy(
				onSurface = lerp(raw.onSurface, raw.primary, 0.14f),
				onSurfaceVariant = lerp(raw.onSurfaceVariant, raw.primary, 0.18f)
			).also { schemeCache[key] = it }
		}
	}

	return CoverColors(
		scheme = scheme,
		seed = seed,
		isDark = coverIsDark
	)
}

/** App dark/light per the user's theme preference (matches the detail screens). */
@Composable
fun rememberAppIsDark(): Boolean {
	val preferenceManager = koinInject<PreferenceManager>()
	return when (preferenceManager.themeMode) {
		ThemeMode.System -> isSystemInDarkTheme()
		ThemeMode.Dark -> true
		ThemeMode.Light -> false
	}
}

/**
 * Cover-derived theming bundle for an ambient surface (sheet / queue): the
 * readable [scheme] to drive content colours via `NavicTheme`, the raw [seed],
 * and [top] — the muted gradient-top colour (same formula as
 * [paige.navic.ui.components.common.ArtAmbientBackground]) used as the sheet's
 * opaque container colour so its drag-handle area blends with the wash.
 */
data class CoverAmbient(
	val scheme: ColorScheme,
	val seed: Color,
	val top: Color,
	val bottom: Color,
	/** Content colour guaranteed to read over [top] — drive `NavicTheme(contentColor = …)`. */
	val onAmbient: Color
)

/**
 * A more vivid, "obvious" accent seed (Symfonium-style): raises saturation while PRESERVING
 * the hue, so washed/dark artwork yields a clear cover accent instead of a near-grey `primary`.
 * A true greyscale colour (no chroma) is returned unchanged, so B&W covers stay neutral.
 */
fun Color.boostedAccent(): Color {
	val r = red; val g = green; val b = blue
	val max = maxOf(r, g, b)
	val min = minOf(r, g, b)
	val delta = max - min
	if (max <= 0f || delta < 0.04f) return this  // black or greyscale → leave neutral
	var h = 60f * when (max) {
		r -> ((g - b) / delta) % 6f
		g -> (b - r) / delta + 2f
		else -> (r - g) / delta + 4f
	}
	if (h < 0f) h += 360f
	val newS = (delta / max * 1.6f).coerceAtMost(1f)   // punchier chroma
	val newV = max.coerceIn(0.5f, 1f)                  // avoid near-black accents
	val c = newV * newS
	val x = c * (1f - abs((h / 60f) % 2f - 1f))
	val m = newV - c
	val (rr, gg, bb) = when {
		h < 60f -> Triple(c, x, 0f)
		h < 120f -> Triple(x, c, 0f)
		h < 180f -> Triple(0f, c, x)
		h < 240f -> Triple(0f, x, c)
		h < 300f -> Triple(x, 0f, c)
		else -> Triple(c, 0f, x)
	}
	return Color(rr + m, gg + m, bb + m)
}

/**
 * Page ambient wash for a cover [seed], eased toward white (light) / black (dark) but
 * keeping enough of the cover colour that the page reads as a rich tint, not washed grey.
 * Shared by the sheets and the album/artist detail screens so every surface matches.
 */
fun coverAmbientGradient(seed: Color, isDark: Boolean): Pair<Color, Color> {
	val target = if (isDark) Color.Black else Color.White
	val top = lerp(seed, target, if (isDark) 0.22f else 0.34f)
	val bottom = lerp(seed, target, if (isDark) 0.45f else 0.20f)
	return top to bottom
}

/**
 * A content colour guaranteed to read over [background]: the scheme's `onSurface` or its
 * inverse, whichever has the greater luminance distance from the painted ambient. The
 * ambient wash is computed independently of the scheme's `surface`, so trusting `onSurface`
 * alone can give low-contrast text on rich/mid-tone covers — this keeps text legible.
 */
fun onAmbientColor(background: Color, scheme: ColorScheme): Color {
	val bg = background.luminance()
	val onSurface = scheme.onSurface
	val inverse = scheme.inverseOnSurface
	return if (abs(onSurface.luminance() - bg) >= abs(inverse.luminance() - bg)) onSurface else inverse
}

@Composable
fun rememberCoverAmbient(
	coverArtId: String?,
	isDark: Boolean = rememberAppIsDark(),
	initialSeed: Color? = null
): CoverAmbient {
	val cover = rememberCoverColorScheme(coverArtId, isDark = isDark, initialSeed = initialSeed)
	// Brightness follows the artwork (cover.isDark), not the app theme passed in.
	val (rawTop, rawBottom) = coverAmbientGradient(cover.seed, cover.isDark)
	// Ease the colours in as the seed resolves (kmpalette extraction is async) so
	// the sheet wash fades from the neutral default to the cover colour instead of
	// popping instantly. Matches the detail screens' `animateColorAsState`.
	val top by animateColorAsState(rawTop, tween(450))
	val bottom by animateColorAsState(rawBottom, tween(450))
	return CoverAmbient(
		scheme = cover.scheme,
		seed = cover.seed,
		top = top,
		bottom = bottom,
		onAmbient = onAmbientColor(top, cover.scheme)
	)
}

/**
 * The CURRENTLY-PLAYING song's cover id, and nothing else. Collects ONLY the cover id,
 * distinct — so callers recompose on SONG change, not on every ~4 Hz progress tick. Critical:
 * this is read at the App root, so a full `uiState` collect there would re-theme the whole app
 * every tick (unusable lag).
 */
@Composable
fun rememberNowPlayingCoverArtId(): String? {
	val player = koinInject<MediaPlayerViewModel>()
	val coverArtId by remember(player) {
		player.uiState
			.map { it.currentSong?.coverArtId }
			.distinctUntilChanged()
	}.collectAsStateWithLifecycle(player.uiState.value.currentSong?.coverArtId)
	return coverArtId
}

/**
 * [CoverAmbient] for the CURRENTLY-PLAYING song's cover — for surfaces not tied
 * to a specific item (queue, device picker, mood / sort sheets).
 */
@Composable
fun rememberNowPlayingCoverAmbient(): CoverAmbient =
	rememberCoverAmbient(rememberNowPlayingCoverArtId())

/**
 * Background brush for the home + every browsing tab: the plain themed surface. (An earlier
 * cover-tinted "dynamic home background" was removed — it was too laggy on scroll.) Kept as
 * a single helper so the home and all tabs share one background source. Home additionally paints
 * [LibraryHeroAmbient] on top of this, capped and fading back to this same surface colour.
 */
@Composable
fun rememberLibraryTabBackground(): Brush = SolidColor(MaterialTheme.colorScheme.surface)

/**
 * A capped, now-playing-keyed hero wash for the library home: the playing song's blurred cover,
 * fading to fully transparent — and so back to the page's [MaterialTheme.colorScheme.surface] —
 * before [heightCap] ends. That hard fade is the point: the album/playlist grids below show a
 * dozen covers of different hues at once, and any residual tint would visibly clash with them.
 *
 * When nothing is playing this renders nothing rather than inventing a colour.
 *
 * Performance: keyed on the cover id only (see [rememberNowPlayingCoverArtId]), so it never
 * recomposes on scroll or the playback-progress tick. Place it as a fixed-size [Box] BEHIND the
 * scrolling grid — never as a grid item, or the blur gets re-measured as the list scrolls.
 */
@Composable
fun LibraryHeroAmbient(
	modifier: Modifier = Modifier,
	heightCap: Dp = 300.dp
) {
	val coverArtId = rememberNowPlayingCoverArtId() ?: return
	val appIsDark = rememberAppIsDark()
	val surface = MaterialTheme.colorScheme.surface
	// Cover-colour wash keyed to the now-playing song, at APP brightness (matching the library
	// scheme) so text over it stays readable. Eased so it crossfades on song change, and read in
	// the draw phase so the crossfade never recomposes the scrolling grid.
	val cover = rememberCoverColorScheme(
		coverArtId,
		isDark = appIsDark,
		followArtworkBrightness = false
	)
	val (rawTop, _) = coverAmbientGradient(cover.seed, appIsDark)
	val top = animateColorAsState(rawTop, tween(450))
	Box(
		modifier = modifier
			.fillMaxWidth()
			.height(heightCap)
	) {
		// The blurred cover itself, not just its average colour — this is what gives the home
		// the depth the flat glow lacked. `isPaused = true` pins BlendBackground's rotation:
		// this sits behind a SCROLLING grid, and an always-animating 80dp blur there is exactly
		// the lag that got the old dynamic home background removed.
		//
		// Both the wash and the fade-out mask are applied in the DRAW phase: the eased colour is
		// read inside drawWithCache, so a song change redraws a gradient over the already-blurred
		// layer instead of recomposing (and re-blurring) it 60 times a second. Handing the eased
		// colour to `scrim` instead — which is read at composition — is what made this stutter.
		//
		// DstIn fades the blurred result out down the page, so the artwork is gone before the
		// album grids begin: a dozen covers of different hues start below this, and any surviving
		// tint would visibly fight them.
		BlendBackground(
			coverArtId = coverArtId,
			isPaused = true,
			scrim = SolidColor(Color.Transparent),
			modifier = Modifier
				.fillMaxSize()
				.graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }
				.drawWithCache {
					val wash = Brush.verticalGradient(
						0f to top.value.copy(alpha = 0.55f),
						0.55f to lerp(top.value, surface, 0.6f).copy(alpha = 0.85f),
						1f to surface
					)
					val fadeOut = Brush.verticalGradient(
						0f to Color.Black,
						0.7f to Color.Black,
						1f to Color.Transparent
					)
					onDrawWithContent {
						drawContent()
						drawRect(wash)
						drawRect(fadeOut, blendMode = BlendMode.DstIn)
					}
				}
		)
	}
}
