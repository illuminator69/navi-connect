package paige.navic.ui.screens.nowPlaying

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.plus
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.dropUnlessResumed
import dev.chrisbanes.haze.HazeState
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_lyrics
import navic.composeapp.generated.resources.action_navigate_back
import navic.composeapp.generated.resources.action_queue
import navic.composeapp.generated.resources.title_now_playing
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinViewModel
import org.koin.core.parameter.parametersOf
import paige.navic.LocalNavStack
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.models.settings.AutoplayMode
import paige.navic.domain.models.settings.NowPlayingBackgroundStyle
import paige.navic.domain.models.settings.ToolbarPosition
import paige.navic.icons.Icons
import paige.navic.icons.outlined.KeyboardArrowDown
import paige.navic.icons.outlined.List
import paige.navic.icons.outlined.Lyrics
import paige.navic.icons.outlined.Radio
import paige.navic.ui.components.sheets.DevicePickerSheet
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.BlendBackground
import paige.navic.ui.components.layouts.SheetScaffold
import paige.navic.ui.components.layouts.TopBarButton
import paige.navic.ui.components.toolbars.SheetActionButton
import paige.navic.ui.components.toolbars.SheetToolbar
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.nowPlaying.components.controls.NowPlayingArtworkPager
import paige.navic.ui.screens.nowPlaying.components.rows.NowPlayingControlsRow
import paige.navic.ui.screens.nowPlaying.viewmodels.NowPlayingViewModel

@OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun NowPlayingScreen() {
	val preferenceManager = koinInject<PreferenceManager>()
	val player = koinInject<MediaPlayerViewModel>()
	val radioManager = koinInject<RadioManager>()
	val autoplayMode by radioManager.autoplayMode.collectAsStateWithLifecycle()
	val backStack = LocalNavStack.current

	// Real backdrop blur (Haze) over the Mood Flow aurora, gated by the appearance
	// toggle. moodHaze is non-null only when it should actually frost (blur on +
	// Adaptive); the autoplay chip uses it then, else its translucent frost.
	val hazeState = remember { HazeState() }
	val expressiveBlur = preferenceManager.expressiveBlur
	val moodHaze = if (expressiveBlur && autoplayMode == AutoplayMode.Adaptive) hazeState else null

	val currentScreen = backStack.lastOrNull()
	val isPlayerCurrent = currentScreen is Screen.NowPlaying
		|| currentScreen is Screen.Queue
		|| currentScreen is Screen.PlaybackSpeed

	var showDevicePicker by remember { mutableStateOf(false) }

	// The player UI mirrors the remote session directly when another
	// navi-connect device is active (MediaPlayerViewModel.uiState blends it in),
	// so this screen no longer needs to minimize itself on a handoff.
	val playerState by player.uiState.collectAsStateWithLifecycle()
	val song = playerState.currentSong

	val viewModel = koinViewModel<NowPlayingViewModel> { parametersOf(player) }
	val songIsStarred by viewModel.songIsStarred.collectAsStateWithLifecycle()
	val songRating by viewModel.songRating.collectAsStateWithLifecycle()

	SheetScaffold(
		toolbar = { windowInsets ->
			SheetToolbar(
				modifier = Modifier.alpha(if (isPlayerCurrent) 1f else 0f),
				windowInsets = windowInsets,
				title = {
					Text(stringResource(Res.string.title_now_playing))
				},
				navigationIcon = {
					TopBarButton(
						onClick = { backStack.remove(Screen.NowPlaying) },
						content = {
							Icon(
								imageVector = Icons.Outlined.KeyboardArrowDown,
								contentDescription = stringResource(Res.string.action_navigate_back)
							)
						}
					)
				},
				actions = {
					SheetActionButton(
						icon = Icons.Outlined.Lyrics,
						contentDescription = stringResource(Res.string.action_lyrics),
						onClick = dropUnlessResumed { backStack.add(Screen.Lyrics) },
						isStartRounded = true
					)
					SheetActionButton(
						icon = Icons.Outlined.List,
						contentDescription = stringResource(Res.string.action_queue),
						onClick = dropUnlessResumed { backStack.add(Screen.Queue) }
					)
					SheetActionButton(
						icon = Icons.Outlined.Radio,
						contentDescription = "Connect to a device",
						onClick = { showDevicePicker = true },
						isEndRounded = true
					)
				}
			)
		}
	) { contentPadding ->
		Box(Modifier.fillMaxSize()) {
			if (preferenceManager.nowPlayingBackgroundStyle
				== NowPlayingBackgroundStyle.Dynamic
			) {
				BlendBackground(
					coverArtId = song?.coverArtId,
					isPaused = playerState.isPaused
				)
			}
				if (!isPlayerCurrent) return@Box
			BoxWithConstraints(
				modifier = Modifier
					.padding(horizontal = 8.dp)
					.fillMaxSize()
			) {
				val isLandscape = maxWidth > maxHeight
				val toolbarPosition = preferenceManager.nowPlayingToolbarPosition
				val padding = when {
					isLandscape -> contentPadding
					toolbarPosition == ToolbarPosition.Top -> contentPadding.plus(
						PaddingValues(
							bottom = 40.dp
						)
					)

					toolbarPosition == ToolbarPosition.Bottom -> contentPadding.plus(
						PaddingValues(
							top = 40.dp
						)
					)

					else -> contentPadding
				}
				if (isLandscape) {
					Row(
						modifier = Modifier.fillMaxSize().padding(padding),
						horizontalArrangement = Arrangement.SpaceEvenly,
						verticalAlignment = Alignment.CenterVertically
					) {
						NowPlayingArtworkPager(
							modifier = Modifier.weight(1f).fillMaxHeight(),
							isLandscape = true
						)
						NowPlayingControlsRow(
							modifier = Modifier.weight(1f).fillMaxHeight(),
							isLandscape = true,
							songIsStarred = songIsStarred,
							onSetSongIsStarred = { viewModel.starSong(it) },
							songRating = songRating,
							onSetSongRating = { viewModel.rateSong(it) },
							hazeState = moodHaze
						)
					}
				} else {
					Column(
						modifier = Modifier.fillMaxSize().padding(padding),
						horizontalAlignment = Alignment.CenterHorizontally,
						verticalArrangement = Arrangement.Center
					) {
						NowPlayingArtworkPager(
							modifier = Modifier.weight(1f).fillMaxWidth(),
							isLandscape = false
						)
						NowPlayingControlsRow(
							modifier = Modifier.weight(1f),
							isLandscape = false,
							songIsStarred = songIsStarred,
							onSetSongIsStarred = { viewModel.starSong(it) },
							songRating = songRating,
							onSetSongRating = { viewModel.rateSong(it) },
							hazeState = moodHaze
						)
					}
				}
			}
		}
	}

	if (showDevicePicker) {
		DevicePickerSheet(onDismissRequest = { showDevicePicker = false })
	}
}
