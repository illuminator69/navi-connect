package paige.navic.ui.screens.nowPlaying.components.controls

import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.interaction.DragInteraction
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import org.koin.compose.koinInject
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.screens.nowPlaying.components.NowPlayingArtwork
import kotlin.time.Duration.Companion.milliseconds

@Composable
fun NowPlayingArtworkPager(
	modifier: Modifier = Modifier,
	isLandscape: Boolean
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val player = koinInject<MediaPlayerViewModel>()
	val hubManager = koinInject<HubManager>()
	val isRemoteActive by hubManager.isRemoteActive.collectAsState()
	val playerState by player.steadyState.collectAsState()

	val pagerState = rememberPagerState(
		initialPage = playerState.currentIndex.coerceAtLeast(0),
		pageCount = { playerState.queue.size }
	)

	var visible by rememberSaveable { mutableStateOf(false) }
	val scale by animateFloatAsState(if (visible) 1f else 0f)
	val offset by animateDpAsState(if (visible) 0.dp else 200.dp)

	LaunchedEffect(Unit) {
		delay(50.milliseconds)
		visible = true
	}

	LaunchedEffect(playerState.currentIndex) {
		if (playerState.currentIndex != -1 && playerState.currentIndex != pagerState.currentPage) {
			pagerState.animateScrollToPage(playerState.currentIndex)
		}
	}

	// Only a real USER swipe may change the track. The pager is also scrolled
	// PROGRAMMATICALLY whenever currentIndex changes (playback advancing — local
	// or, via the remote mirror, on another device). Acting on those programmatic
	// settles caused an infinite jump/skip loop when remote (the resolved queue
	// index space can drift from the hub's, so settledPage never equals
	// currentIndex and keeps re-firing actJump). Gate on drag interactions.
	var userDragging by remember { mutableStateOf(false) }
	LaunchedEffect(pagerState) {
		pagerState.interactionSource.interactions.collect { interaction ->
			if (interaction is DragInteraction.Start) {
				userDragging = true
			}
		}
	}

	LaunchedEffect(pagerState) {
		snapshotFlow { pagerState.settledPage }.collect { page ->
			val wasUserSwipe = userDragging
			userDragging = false
			if (page == playerState.currentIndex || !wasUserSwipe) return@collect
			if (isRemoteActive) {
				// The hub's jump preserves play/pause state for the session.
				hubManager.actJump(page)
			} else {
				val wasPaused = playerState.isPaused
				player.playAt(page)
				if (wasPaused) {
					player.pause()
				}
			}
		}
	}

	HorizontalPager(
		modifier = modifier.scale(scale).offset {
			IntOffset(x = 0, y = offset.roundToPx())
		},
		state = pagerState,
		contentPadding = PaddingValues(horizontal = if (isLandscape) 0.dp else 8.dp),
		userScrollEnabled = preferenceManager.swipeToSkip,
		overscrollEffect = null
	) { page ->
		val song = playerState.queue[page]
		Box(
			modifier = Modifier.fillMaxSize(),
			contentAlignment = Alignment.Center
		) {
			NowPlayingArtwork(
				song = song,
				isLandscape = isLandscape
			)
		}
	}
}
