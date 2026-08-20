package paige.navic.ui.components.layouts

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.dropShadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.graphics.shadow.Shadow
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.dropUnlessResumed
import coil3.compose.AsyncImage
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import coil3.request.crossfade
import com.kyant.capsule.ContinuousRoundedRectangle
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.info_not_playing
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinViewModel
import paige.navic.LocalNavStack
import paige.navic.LocalPlatformContext
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.models.settings.MiniPlayerProgressStyle
import paige.navic.domain.models.settings.MiniPlayerStyle
import paige.navic.domain.models.settings.NavbarConfig
import paige.navic.icons.Icons
import paige.navic.icons.filled.Note
import paige.navic.icons.filled.Pause
import paige.navic.icons.filled.Play
import paige.navic.icons.filled.SkipNext
import paige.navic.icons.outlined.Radio
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.MarqueeText
import paige.navic.ui.components.common.blur.LocalExpressiveBlur
import paige.navic.ui.components.common.blur.expressiveBlurEffect
import paige.navic.ui.components.common.playPauseIconPainter
import paige.navic.ui.core.UiState
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.settings.viewmodels.NavtabsViewModel
import paige.navic.util.ui.rememberNowPlayingCoverAmbient
import coil3.compose.LocalPlatformContext as LocalCoilPlatformContext

@OptIn(ExperimentalMaterial3ExpressiveApi::class, ExperimentalMaterial3Api::class)
@Composable
fun MiniPlayer(
	modifier: Modifier = Modifier,
	enabled: Boolean = true
) {
	val platformContext = LocalPlatformContext.current
	val player = koinInject<MediaPlayerViewModel>()
	val hubManager = koinInject<HubManager>()
	val isRemoteActive by hubManager.isRemoteActive.collectAsState()
	// Active device name for the Spotify-style "Playing on <device>" cue.
	val hubDevices by hubManager.devices.collectAsState()
	val hubActiveId by hubManager.activeDeviceId.collectAsState()
	val remoteDeviceName = hubDevices.firstOrNull { it.id == hubActiveId }?.name ?: "remote device"
	val preferenceManager = koinInject<PreferenceManager>()
	// When Expressive blur is on, make the mini-player container translucent so the
	// frosted backdrop (RootBottomBar's hazeEffect over the screen content) shows
	// through the card; otherwise keep the opaque surface.
	val expressiveBlur = LocalExpressiveBlur.current
	val navtabsViewModel = koinViewModel<NavtabsViewModel>()
	val navtabsState by navtabsViewModel.state.collectAsState()
	val tabs = remember(navtabsState) {
		((navtabsState as? UiState.Success)?.data ?: NavbarConfig.default)
			.tabs.filter { tab -> tab.visible }
	}
	val backStack = LocalNavStack.current
	val haptics = LocalHapticFeedback.current
	val navBarPadding = if (tabs.size < 2)
		with(LocalDensity.current) { WindowInsets.navigationBars.getBottom(this).toDp() }
	else 0.dp

	// steadyState: the mini-player is on screen almost everywhere, and the only thing here that
	// follows the playhead is [MiniPlayerProgressBar], which collects it in its own scope.
	val playerState by player.steadyState.collectAsState()
	val song = playerState.currentSong

	val coilPlatformContext = LocalCoilPlatformContext.current
	val sessionManager = koinInject<SessionManager>()
	val radioManager = koinInject<RadioManager>()
	val model = remember(song?.coverArtId) {
		ImageRequest.Builder(coilPlatformContext)
			.data(song?.coverArtId?.let { sessionManager.getCoverArtUrl(it) })
			.memoryCacheKey(song?.coverArtId)
			.diskCacheKey(song?.coverArtId)
			.diskCachePolicy(CachePolicy.ENABLED)
			.memoryCachePolicy(CachePolicy.ENABLED)
			// Cross-fade the thumbnail on track change so the swap eases in instead of
			// hard-cutting — matches Now Playing and CoverArt.
			.crossfade(400)
			.build()
	}

	val detached = preferenceManager.miniPlayerStyle == MiniPlayerStyle.Detached

	val outerPadding = if (detached) 12.dp else 0.dp
	val coverRounding by animateDpAsState(
		if (playerState.isLoading)
			46.dp
		else 8.dp
	)
	val iconSize = if (detached) 24.dp else 32.dp

	val shape = ContinuousRoundedRectangle(
		if (detached) 16.dp else 0.dp
	)

	// Same now-playing extraction the library hero and (for the same song) the detail page read,
	// so all three agree on hue. `seed` is not eased inside CoverAmbient — only `top`/`bottom`
	// are — so ease it here, or the ring/shadow would pop on track change.
	val ambient = rememberNowPlayingCoverAmbient()
	val ringColor by animateColorAsState(ambient.seed, tween(450))

	val onClick = dropUnlessResumed {
		if (!backStack.contains(Screen.NowPlaying)) {
			backStack.add(Screen.NowPlaying)
		}
	}

	val hasSong = song != null
	val isRadio = song?.id?.startsWith("radio_") == true
	val isInteractive = enabled && hasSong

	Swiper(
		onSwipeLeft = {
			if (isInteractive) {
				if (isRemoteActive) hubManager.actNext() else player.next()
			}
		},
		onSwipeRight = {
			if (isInteractive) {
				if (isRemoteActive) hubManager.actPrevious() else player.previous()
			}
		},
		modifier = modifier,
		enabled = isInteractive
	) {
		Box(
			modifier = Modifier
				.widthIn(max = if (detached) 600.dp else Dp.Unspecified)
				.padding(
					bottom = if (detached) outerPadding + navBarPadding else 0.dp,
					start = outerPadding,
					end = outerPadding
				)
				.align(Alignment.Center)
		) {
			ListItem(
				modifier = Modifier
					.dropShadow(
						shape,
						Shadow(
							radius = if (detached) 10.dp else 8.dp,
							color = ringColor,
							alpha = 0.25f
						)
					)
					// Only when detached: the full-bleed variant has no free edges, so a border
					// would just draw hairlines along the screen edges and across the top.
					.then(
						if (detached) Modifier.border(1.dp, ringColor.copy(alpha = 0.32f), shape)
						else Modifier
					)
					// Frost the floating pill itself (clipped to its shape) so it reads as a
					// self-contained frosted-glass card over the content — instead of relying on
					// a full-width haze band behind it. No-op when Expressive blur is off.
					.then(
						if (detached && expressiveBlur.enabled)
							Modifier.clip(shape).expressiveBlurEffect(expressiveBlur)
						else Modifier
					)
					.pointerInput(isInteractive) {
						if (!isInteractive) return@pointerInput
						var totalDrag = 0f
						detectVerticalDragGestures(
							onVerticalDrag = { _, dragAmount ->
								totalDrag += dragAmount
							},
							onDragEnd = {
								if (totalDrag < -150f) {
									onClick()
								}
								totalDrag = 0f
							}
						)
					},
				contentPadding = PaddingValues(
					start = if (detached) 10.dp else 16.dp,
					end = if (detached) 10.dp else 16.dp,
					top = if (detached) 10.dp else 16.dp,
					bottom = (if (detached) 10.dp else 12.dp) + if (detached) 0.dp else navBarPadding
				),
				verticalAlignment = Alignment.CenterVertically,
				colors = ListItemDefaults.colors(
					containerColor = if (expressiveBlur.enabled)
						NavigationBarDefaults.containerColor.copy(alpha = 0.6f)
					else NavigationBarDefaults.containerColor
				),
				shapes = ListItemDefaults.shapes(
					shape = shape,
					selectedShape = shape,
					pressedShape = shape,
					focusedShape = shape,
					hoveredShape = shape,
					draggedShape = shape
				),
				onClick = {
					platformContext.clickSound()
					onClick()
				},
				onLongClick = {
					haptics.performHapticFeedback(HapticFeedbackType.GestureThresholdActivate)
					onClick()
				},
				leadingContent = {
					Box(contentAlignment = Alignment.Center) {
						AsyncImage(
							model = model,
							contentDescription = null,
							contentScale = ContentScale.Crop,
							modifier = Modifier
								.size(if (detached) 48.dp else 50.dp)
								.padding(if (playerState.isLoading) 8.dp else 0.dp)
								.clip(
									ContinuousRoundedRectangle(coverRounding)
								)
								.background(MaterialTheme.colorScheme.surfaceVariant)
						)
						if (song?.coverArtId.isNullOrEmpty()) {
							Icon(
								imageVector = if (isRadio) Icons.Outlined.Radio else Icons.Filled.Note,
								contentDescription = null,
								tint = MaterialTheme.colorScheme.onSurface.copy(alpha = .38f)
							)
						}
						AnimatedVisibility(
							playerState.isLoading,
							modifier = Modifier.matchParentSize(),
							enter = scaleIn(MaterialTheme.motionScheme.defaultSpatialSpec())
								+ fadeIn(MaterialTheme.motionScheme.defaultEffectsSpec()),
							exit = scaleOut(MaterialTheme.motionScheme.defaultSpatialSpec())
								+ fadeOut(MaterialTheme.motionScheme.defaultEffectsSpec())
						) {
							CircularProgressIndicator(
								Modifier.matchParentSize(),
								trackColor = MaterialTheme.colorScheme.primaryContainer
							)
						}
					}
				},
				trailingContent = {
					Row(
						horizontalArrangement = Arrangement.spacedBy(
							if (detached) 8.dp else 12.dp
						)
					) {
						val colors = IconButtonDefaults.iconButtonVibrantColors()
						IconButton(
							onClick = {
								platformContext.clickSound()
								if (isRemoteActive) {
									hubManager.actPlayPause()
								} else if (playerState.isPaused) {
									player.resume()
								} else {
									player.pause()
								}
							},
							enabled = isInteractive,
							colors = colors
						) {
							val painter = playPauseIconPainter(playerState.isPaused)
							if (painter != null) {
								Icon(
									painter = painter,
									contentDescription = null,
									modifier = Modifier.size(iconSize)
								)
							} else {
								Icon(
									imageVector = if (playerState.isPaused)
										Icons.Filled.Play
									else Icons.Filled.Pause,
									contentDescription = null,
									modifier = Modifier.size(iconSize)
								)
							}
						}
						IconButton(
							onClick = {
								platformContext.clickSound()
								if (isRemoteActive) hubManager.actNext() else player.next()
							},
							enabled = isInteractive,
							colors = colors
						) {
							Icon(
								imageVector = Icons.Filled.SkipNext,
								contentDescription = null,
								modifier = Modifier.size(iconSize)
							)
						}
					}
				},
				content = {
					song?.title?.let { title ->
						MarqueeText(title)
					}
				},
				supportingContent = {
					if (isRemoteActive) {
						// Spotify-style accent cue that playback is on another device.
						MarqueeText(
							"Playing on $remoteDeviceName",
							style = LocalTextStyle.current.copy(
								color = MaterialTheme.colorScheme.primary
							)
						)
					} else if (song != null) {
						MarqueeText(song.artistName)
					} else {
						MarqueeText(stringResource(Res.string.info_not_playing))
					}
				},
				enabled = enabled
			)
			if (preferenceManager.miniPlayerProgressStyle == MiniPlayerProgressStyle.Visible
				|| preferenceManager.miniPlayerProgressStyle == MiniPlayerProgressStyle.Seekable
			) {
				MiniPlayerProgressBar(
					hasSong = hasSong,
					durationMs = song?.duration?.inWholeMilliseconds ?: 0L,
					detached = detached,
					shape = shape,
					isInteractive = isInteractive,
					isRemoteActive = isRemoteActive
				)
			}
		}
	}
}

/**
 * The mini-player's own progress line, in its own composable ON PURPOSE.
 *
 * It is the one part of the mini-player that has to follow the playhead, so it collects
 * [MediaPlayerViewModel.progress] itself instead of reading it off the state the parent already
 * holds. Inlined into [MiniPlayer], that read invalidated the whole bar — cover art, marquee text,
 * buttons and (with Expressive blur on) the pill's blur node — five times a second for the whole
 * of playback. Here, only these three Boxes recompose.
 */
@Composable
private fun BoxScope.MiniPlayerProgressBar(
	hasSong: Boolean,
	durationMs: Long,
	detached: Boolean,
	shape: Shape,
	isInteractive: Boolean,
	isRemoteActive: Boolean
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val player = koinInject<MediaPlayerViewModel>()
	val hubManager = koinInject<HubManager>()
	val radioManager = koinInject<RadioManager>()
	val haptics = LocalHapticFeedback.current
	val rawProgress by player.progress.collectAsState()

	var dragging by remember { mutableStateOf(false) }
	val alpha by animateFloatAsState(
		if (dragging) 1f else .7f
	)
	val progress by animateFloatAsState(
		rawProgress.coerceIn(0f, 1f)
	)
	val alignment = if (detached) Alignment.BottomStart else Alignment.TopStart
	Box(
		modifier = Modifier
			.matchParentSize()
			.clip(shape)
			.align(alignment),
		contentAlignment = alignment
	) {
		if (!detached) {
			Box(
				Modifier
					.background(MaterialTheme.colorScheme.surfaceBright)
					.fillMaxWidth()
					.height(3.dp)
			)
		}
		// AudioMuse mix indicator: while a similarity radio plays, the
		// progress bar drifts through the AudioMuse logo palette
		// (periwinkle → pink → orange).
		val isAudioMuseMix by radioManager.isAudioMuseMix.collectAsState()
		val audioMuseColor = if (isAudioMuseMix) {
			val transition = rememberInfiniteTransition(label = "audiomuse")
			val shift by transition.animateFloat(
				initialValue = 0f,
				targetValue = 2f,
				animationSpec = infiniteRepeatable(
					animation = tween(durationMillis = 6000),
					repeatMode = RepeatMode.Reverse
				),
				label = "audiomuse-color"
			)
			val periwinkle = Color(0xFF93A2E8)
			val pink = Color(0xFFEE7B90)
			val orange = Color(0xFFF5A661)
			if (shift < 1f) lerp(periwinkle, pink, shift)
			else lerp(pink, orange, shift - 1f)
		} else null
		Box(
			Modifier
				.background(
					(audioMuseColor ?: MaterialTheme.colorScheme.primary)
						.copy(alpha = alpha)
				)
				.fillMaxWidth(if (hasSong) progress else 0f)
				.height(3.dp)
		)
		Box(
			Modifier
				.fillMaxWidth()
				.height(14.dp)
				.then(
					if (hasSong
						&& preferenceManager.miniPlayerProgressStyle == MiniPlayerProgressStyle.Seekable
						&& isInteractive
					)
						Modifier.pointerInput(Unit) {
							detectDragGestures(
								onDragStart = {
									dragging = true
									haptics.performHapticFeedback(HapticFeedbackType.GestureThresholdActivate)
								},
								onDragEnd = {
									dragging = false
									haptics.performHapticFeedback(HapticFeedbackType.GestureEnd)
								}
							) { change, _ ->
								val fraction =
									(change.position.x / size.width.toFloat())
										.coerceIn(0f, 1f)
								if (isRemoteActive) {
									hubManager.actSeek((fraction * durationMs).toLong())
								} else {
									player.seek(fraction)
								}
								change.consume()
							}
						}
					else Modifier
				)
		)
	}
}
