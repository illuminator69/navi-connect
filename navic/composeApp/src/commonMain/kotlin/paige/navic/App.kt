package paige.navic

import androidx.compose.animation.ContentTransform
import androidx.compose.animation.SharedTransitionLayout
import androidx.compose.animation.SharedTransitionScope
import androidx.compose.animation.core.EaseOutQuart
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.calculateEndPadding
import androidx.compose.foundation.layout.calculateStartPadding
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Snackbar
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.adaptive.ExperimentalMaterial3AdaptiveApi
import androidx.compose.material3.adaptive.navigation3.ListDetailSceneStrategy.Companion.detailPane
import androidx.compose.material3.adaptive.navigation3.ListDetailSceneStrategy.Companion.listPane
import androidx.compose.material3.adaptive.navigation3.rememberListDetailSceneStrategy
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavEntry
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.ui.NavDisplay
import androidx.navigation3.ui.NavDisplay.popTransitionSpec
import androidx.navigation3.ui.NavDisplay.predictivePopTransitionSpec
import androidx.navigation3.ui.NavDisplay.transitionSpec
import androidx.savedstate.serialization.SavedStateConfiguration
import coil3.compose.setSingletonImageLoaderFactory
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.modules.SerializersModule
import kotlinx.serialization.modules.polymorphic
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.lbbot_fill_landed
import navic.composeapp.generated.resources.lbbot_fill_lost
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.di.initializeSingletonImageLoader
import paige.navic.domain.manager.BottomBarScrollManager
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.SessionManager
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.blur.LocalExpressiveBlur
import paige.navic.ui.components.common.blur.expressiveBlurSource
import paige.navic.ui.components.common.blur.rememberExpressiveBlur
import paige.navic.ui.components.dialogs.SideloadingDialog
import paige.navic.ui.components.sheets.ChangelogSheet
import paige.navic.ui.navigation.BottomSheetSceneStrategy
import paige.navic.ui.navigation.NowPlayingSceneStrategy
import paige.navic.ui.navigation.AppDeepLink
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.album.AlbumListScreen
import paige.navic.ui.screens.artist.ArtistDetailScreen
import paige.navic.ui.screens.artist.ArtistListScreen
import paige.navic.ui.screens.collection.CollectionDetailScreen
import paige.navic.ui.screens.genre.GenreListScreen
import paige.navic.ui.screens.library.LibraryScreen
import paige.navic.ui.screens.login.LoginScreen
import paige.navic.ui.screens.lyrics.LyricsScreen
import paige.navic.ui.screens.nowPlaying.NowPlayingScreen
import paige.navic.ui.screens.nowPlaying.PlaybackSpeedScreen
import paige.navic.ui.screens.playlist.PlaylistListScreen
import paige.navic.ui.screens.queue.QueueScreen
import paige.navic.ui.screens.radio.RadioListScreen
import paige.navic.ui.screens.search.SearchScreen
import paige.navic.ui.screens.settings.BottomBarScreen
import paige.navic.ui.screens.settings.FontsScreen
import paige.navic.ui.screens.playlist.SmartPlaylistEditorScreen
import paige.navic.ui.screens.settings.NaviConnectScreen
import paige.navic.ui.screens.settings.SettingsAboutScreen
import paige.navic.ui.screens.settings.SettingsAcknowledgementsScreen
import paige.navic.ui.screens.settings.SettingsAppearanceScreen
import paige.navic.ui.screens.settings.SettingsCustomHeadersScreen
import paige.navic.ui.screens.settings.DownloadCenterScreen
import paige.navic.ui.screens.settings.SettingsDataStorageScreen
import paige.navic.ui.screens.settings.SettingsDeveloperScreen
import paige.navic.ui.screens.settings.SettingsLogsScreen
import paige.navic.ui.screens.settings.SettingsNowPlayingScreen
import paige.navic.ui.screens.settings.SettingsPlaybackScreen
import paige.navic.ui.screens.settings.SettingsScreen
import paige.navic.ui.screens.settings.SettingsStreamingQualityScreen
import paige.navic.ui.screens.savedqueues.SavedQueuesScreen
import paige.navic.ui.screens.share.ShareListScreen
import paige.navic.ui.screens.song.SongDetailScreen
import paige.navic.ui.screens.song.SongListScreen
import paige.navic.ui.screens.starred.StarredScreen
import paige.navic.ui.theme.NavicTheme
import paige.navic.util.core.PlatformContext
import paige.navic.util.core.PlatformType
import paige.navic.util.core.rememberPlatformContext
import paige.navic.util.ui.Material3Transitions
import paige.navic.util.ui.rememberLibraryTabBackground

@OptIn(ExperimentalSerializationApi::class)
private val config = SavedStateConfiguration {
	serializersModule = SerializersModule {
		polymorphic(NavKey::class) {
			subclassesOfSealed<Screen>()
		}
	}
}

val LocalPlatformContext = staticCompositionLocalOf<PlatformContext> { error("no platform context") }
val LocalNavStack = staticCompositionLocalOf<NavBackStack<NavKey>> { error("no backstack") }
val LocalSnackbarState = staticCompositionLocalOf<SnackbarHostState> { error("no snackbar state") }
val LocalSharedTransitionScope =
	staticCompositionLocalOf<SharedTransitionScope> { error("no shared transition scope") }

val LocalBottomBarScrollManager = staticCompositionLocalOf<BottomBarScrollManager> {
	error("No BottomBarScrollManager provided")
}

// Coil's singleton factory may only be installed once per process. When the
// playback service keeps the process alive (e.g. while casting) and the UI is
// reopened, App() recomposes from scratch — installing again after the loader
// exists throws. Install once, skip on re-entry.
private var imageLoaderFactoryInstalled = false

// Whether App() should install Coil's singleton factory. FALSE on Android — the
// Application implements SingletonImageLoader.Factory, so the loader is created
// with our config no matter who calls SingletonImageLoader.get() first (the
// playback service can load album art before the Activity attaches after an
// in-place update; the composable install here would then throw "already
// created"). TRUE on iOS, which has no Application-level factory.
internal expect val installComposeSingletonImageLoader: Boolean

@OptIn(ExperimentalMaterial3AdaptiveApi::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun App() {
	if (installComposeSingletonImageLoader && !imageLoaderFactoryInstalled) {
		imageLoaderFactoryInstalled = true
		setSingletonImageLoaderFactory { platformContext ->
	 		initializeSingletonImageLoader(platformContext)
		}
	}

	val platformContext = rememberPlatformContext()
	val sessionManager = koinInject<SessionManager>()
	val preferenceManager = koinInject<PreferenceManager>()
	val isLoggedIn by sessionManager.isLoggedIn.collectAsStateWithLifecycle()
	val backStack = rememberNavBackStack(
		config, if (isLoggedIn) {
			Screen.Library()
		} else {
			Screen.Login
		}
	)
	val snackbarState = remember { SnackbarHostState() }
	val density = LocalDensity.current
	val layoutDirection = LocalLayoutDirection.current
	val scrollManager = remember {
		BottomBarScrollManager(with(density) { 50.dp.toPx() })
	}

	// One shared backdrop-blur state for the whole app: the NavDisplay content is
	// the source, and the (per-screen) bottom bar reads it via LocalExpressiveBlur
	// to frost over it. Gated by the Appearance "Expressive blur" toggle.
	val expressiveBlur = rememberExpressiveBlur(preferenceManager.expressiveBlur)

	// Ask for local-network access here, not only on the login screen. Anyone who signed in
	// before the app started making direct LAN connections never gets asked otherwise, and the
	// platform then falls back to prompting per connection — a "choose a device to connect"
	// chooser on every launch, which grants nothing. No-op once granted.
	LaunchedEffect(Unit) { platformContext.checkLocalNetworkPermission() }

	// The nav graph, built once rather than on every navigation.
	//
	// `entryProvider` needs to know whether we're on a root tab (it picks a different transition
	// for those), and it used to read `backStack.size` itself — but that read happens DURING App's
	// composition, so it subscribed App to the backstack and every push/pop recomposed the whole
	// of App: rebuilding all ~50 entries, handing NavDisplay a brand-new lambda it therefore
	// couldn't skip, and re-running NavicTheme over the entire tree — all while the 450 ms
	// SharedXAxis transition was animating. Behind `derivedStateOf`, App is invalidated only when
	// the root/non-root boundary is actually crossed.
	val isRoot by remember(backStack) { derivedStateOf { backStack.size == 1 } }
	val entries = remember(backStack, isRoot) { entryProvider(isRoot) }

	// The individual strategies were already remembered; the enclosing list was not, so
	// NavDisplay still received a new `sceneStrategies` value on every pass.
	val nowPlayingScene = remember { NowPlayingSceneStrategy<NavKey>() }
	val bottomSheetScene = remember { BottomSheetSceneStrategy<NavKey>() }
	val listDetailScene = rememberListDetailSceneStrategy<NavKey>()
	val sceneStrategies = remember(nowPlayingScene, bottomSheetScene, listDetailScene) {
		listOf(nowPlayingScene, bottomSheetScene, listDetailScene)
	}


	// Announce a fill landing (or failing) wherever the user happens to be.
	//
	// Collected here rather than in the sheet that started it: a fill takes minutes and
	// routinely outlives that sheet, the artist page and the process. The manager emits
	// exactly once per fill, from `settle`, so however many screens are watching there is
	// only ever one snackbar. The system-notification half lives in androidMain — it needs
	// a runtime permission, and commonMain still has to compile for iOS.
	val lbBot = koinInject<LbBotManager>()
	val fillLanded = stringResource(Res.string.lbbot_fill_landed)
	val fillLost = stringResource(Res.string.lbbot_fill_lost)
	LaunchedEffect(Unit) {
		lbBot.fillEvents.collect { event ->
			val name = event.album.ifBlank { event.artist }.ifBlank { return@collect }
			// Only the two outcomes worth interrupting for. `needs_pick` is the picker
			// waiting on the user and `cancelled` is something they just did, both of
			// which announce themselves; `gave_up` means we stopped looking, not that
			// anything happened.
			val message = when (event.outcome) {
				LbBotManager.OUTCOME_DONE -> fillLanded.replace("%1\$s", name)
				LbBotManager.OUTCOME_FAILED -> fillLost.replace("%1\$s", name)
				else -> return@collect
			}
			snackbarState.showSnackbar(message)
		}
	}

	// A screen requested from outside the composition (a Quick Picks widget tile). Held until
	// there is a signed-in session to show it in — a cold start from the widget composes this
	// before the session restores, and pushing the album over the login screen would strand it.
	val deepLink by AppDeepLink.pending.collectAsStateWithLifecycle()
	LaunchedEffect(deepLink, isLoggedIn) {
		val target = deepLink ?: return@LaunchedEffect
		if (!isLoggedIn) return@LaunchedEffect
		if (backStack.lastOrNull() != target) backStack.add(target)
		AppDeepLink.consume()
	}

	SharedTransitionLayout {
		CompositionLocalProvider(
			LocalPlatformContext provides platformContext,
			LocalNavStack provides backStack,
			LocalSnackbarState provides snackbarState,
			LocalSharedTransitionScope provides this@SharedTransitionLayout,
			LocalBottomBarScrollManager provides scrollManager,
			LocalExpressiveBlur provides expressiveBlur
		) {
			NavicTheme {
				Scaffold(
					modifier = Modifier.nestedScroll(scrollManager.connection),
					snackbarHost = {
						SnackbarHost(hostState = snackbarState) { snackbarData ->
							Snackbar(
								snackbarData = snackbarData,
								shape = MaterialTheme.shapes.large
							)
						}
					}
				) { contentPadding ->
					NavDisplay(
						modifier = Modifier
							.padding(
								start = contentPadding
									.calculateStartPadding(layoutDirection),
								end = contentPadding
									.calculateEndPadding(layoutDirection)
							)
							.fillMaxSize()
							.background(rememberLibraryTabBackground())
							.expressiveBlurSource(expressiveBlur),
						backStack = backStack,
						sceneStrategies = sceneStrategies,
						onBack = {
							if (backStack.isNotEmpty()) {
								backStack.removeLastOrNull()
							}
						},
						entryProvider = entries,
						transitionSpec = {
							Material3Transitions.SharedXAxisEnterTransition(
								density
							) togetherWith Material3Transitions.SharedXAxisExitTransition(
								density
							)
						},
						popTransitionSpec = {
							Material3Transitions.SharedXAxisPopEnterTransition(
								density
							) togetherWith Material3Transitions.SharedXAxisPopExitTransition(
								density
							)
						},
						predictivePopTransitionSpec = {
							slideInHorizontally(
								animationSpec = tween(300, easing = EaseOutQuart),
								initialOffsetX = { -it }
							) togetherWith slideOutHorizontally(
								animationSpec = tween(300, easing = EaseOutQuart),
								targetOffsetX = { it }
							)
						}
					)
				}
				if (!preferenceManager.showedSideloadingWarning
					&& platformContext.name.lowercase().contains("android")
				) {
					SideloadingDialog()
				}
				// version check is annoying to do on iOS
				if (preferenceManager.checkForUpdates && platformContext.platformType == PlatformType.Android) {
					ChangelogSheet()
				}
			}
		}
	}
}

@OptIn(ExperimentalMaterial3AdaptiveApi::class, ExperimentalMaterial3Api::class)
private fun entryProvider(
	isRoot: Boolean
): (NavKey) -> (NavEntry<NavKey>) {
	val navtabMetadata = if (isRoot)
		listPane("root") + transitionSpec {
			ContentTransform(fadeIn(), fadeOut())
		} + popTransitionSpec {
			ContentTransform(fadeIn(), fadeOut())
		} + predictivePopTransitionSpec {
			ContentTransform(fadeIn(), fadeOut())
		}
	else listPane("root")
	return androidx.navigation3.runtime.entryProvider {
		// tabs
		entry<Screen.Library>(metadata = navtabMetadata) {
			LibraryScreen()
		}
		entry<Screen.Starred>(metadata = navtabMetadata) {
			StarredScreen()
		}
		entry<Screen.AlbumList>(metadata = navtabMetadata) { key ->
			AlbumListScreen(key.nested, key.listType)
		}
		entry<Screen.PlaylistList>(metadata = navtabMetadata) { key ->
			PlaylistListScreen(key.nested)
		}
		entry<Screen.ArtistList>(metadata = navtabMetadata) { key ->
			ArtistListScreen(key.nested, key.listType)
		}
		entry<Screen.GenreList>(metadata = navtabMetadata) { key ->
			GenreListScreen(key.nested)
		}
		entry<Screen.SongList>(metadata = navtabMetadata) { key ->
			SongListScreen(key.nested, key.artistId, key.artistName, key.listType)
		}

		entry<Screen.RadioList>(metadata = navtabMetadata) { key ->
			RadioListScreen(key.nested)
		}

		// misc
		entry<Screen.Login> {
			LoginScreen()
		}
		entry<Screen.NowPlaying>(
			metadata = NowPlayingSceneStrategy.bottomSheet(maxWidth = Dp.Unspecified)
		) {
			NowPlayingScreen()
		}
		entry<Screen.Lyrics>(metadata = NowPlayingSceneStrategy.bottomSheet(isTransparent = true)) {
			val player = koinInject<MediaPlayerViewModel>()
			val playerState by player.steadyState.collectAsState()
			val song = playerState.currentSong
			LyricsScreen(song)
		}
		entry<Screen.Queue>(metadata = BottomSheetSceneStrategy.bottomSheet()) {
			QueueScreen()
		}
		entry<Screen.PlaybackSpeed>(metadata = BottomSheetSceneStrategy.bottomSheet()) {
			PlaybackSpeedScreen()
		}
		entry<Screen.CollectionDetail>(metadata = detailPane("root")) { key ->
			CollectionDetailScreen(key.collectionId, key.tab)
		}
		entry<Screen.SongDetail>(metadata = detailPane("root")) { key ->
			SongDetailScreen(key.songId)
		}
		entry<Screen.Search>(metadata = navtabMetadata) { key ->
			SearchScreen(key.nested)
		}
		entry<Screen.ShareList> {
			ShareListScreen()
		}
		entry<Screen.SavedQueues> {
			SavedQueuesScreen()
		}
		entry<Screen.ArtistDetail> { key ->
			ArtistDetailScreen(key.artist)
		}

		// settings
		entry<Screen.Settings.Root>(metadata = listPane("settings")) {
			SettingsScreen()
		}
		entry<Screen.Settings.Appearance>(metadata = detailPane("settings")) {
			SettingsAppearanceScreen()
		}
		entry<Screen.Settings.BottomAppBar>(metadata = detailPane("settings")) {
			BottomBarScreen()
		}
		entry<Screen.Settings.NowPlaying>(metadata = detailPane("settings")) {
			SettingsNowPlayingScreen()
		}
		entry<Screen.Settings.Playback>(metadata = detailPane("settings")) {
			SettingsPlaybackScreen()
		}
		entry<Screen.Settings.Developer>(metadata = detailPane("settings")) {
			SettingsDeveloperScreen()
		}
		entry<Screen.Settings.About>(metadata = detailPane("settings")) {
			SettingsAboutScreen()
		}
		entry<Screen.Settings.Acknowledgements>(metadata = detailPane("settings")) {
			SettingsAcknowledgementsScreen()
		}
		entry<Screen.Settings.DataStorage>(metadata = detailPane("settings")) {
			SettingsDataStorageScreen()
		}
		entry<Screen.Settings.DownloadCenter>(metadata = detailPane("settings")) {
			DownloadCenterScreen()
		}
		entry<Screen.Settings.Fonts> {
			FontsScreen()
		}
		entry<Screen.Settings.CustomHeaders> {
			SettingsCustomHeadersScreen()
		}
		entry<Screen.Settings.StreamingQuality> {
			SettingsStreamingQualityScreen()
		}
		entry<Screen.Settings.Logs> {
			SettingsLogsScreen()
		}
		entry<Screen.Settings.NaviConnect>(metadata = detailPane("settings")) {
			NaviConnectScreen()
		}
		entry<Screen.SmartPlaylistEditor> {
			SmartPlaylistEditorScreen()
		}
	}
}
