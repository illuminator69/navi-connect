package paige.navic.ui.navigation

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * A screen the app was asked to open from outside the composition — today, an album tapped on the
 * Quick Picks home-screen widget.
 *
 * It is a flow rather than a start-destination argument because the request can arrive at any
 * point in the process's life: `App()` may not be composed yet (cold start), or may have been
 * composed for minutes already (the widget re-entering a running activity through `onNewIntent`).
 * The composition drains it once it is up and the user is signed in.
 */
object AppDeepLink {

	private val _pending = MutableStateFlow<Screen?>(null)
	val pending: StateFlow<Screen?> = _pending.asStateFlow()

	/**
	 * Ask for [albumId]'s detail page.
	 *
	 * Callers name an album rather than a [Screen] on purpose: the platform entry points that
	 * raise these (`MainActivity`) sit outside the module that owns navigation, and would
	 * otherwise have to pull in navigation3 just to spell the destination.
	 */
	fun requestAlbum(albumId: String) {
		_pending.value = Screen.CollectionDetail(albumId, "")
	}

	fun consume() {
		_pending.value = null
	}
}
