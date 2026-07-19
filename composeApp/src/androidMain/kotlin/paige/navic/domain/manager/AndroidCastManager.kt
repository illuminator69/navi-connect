package paige.navic.domain.manager

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.mediarouter.media.MediaRouteSelector
import androidx.mediarouter.media.MediaRouter
import com.google.android.gms.cast.CastMediaControlIntent
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import paige.navic.util.core.Logger

/**
 * MediaRouter-based Cast discovery. Selecting a route starts a Cast session;
 * PlaybackService's SessionAvailabilityListener then swaps the MediaSession's
 * player to the CastPlayer (and back on disconnect), so the rest of the app —
 * including navi-connect position reports — keeps working unchanged through
 * the MediaController.
 *
 * All MediaRouter calls must happen on the main thread, hence the handler.
 */
class AndroidCastManager(private val context: Context) : CastManager {
	private val mainHandler = Handler(Looper.getMainLooper())

	private val _devices = MutableStateFlow<List<CastDevice>>(emptyList())
	override val devices: StateFlow<List<CastDevice>> = _devices.asStateFlow()

	private val _connectedName = MutableStateFlow<String?>(null)
	override val connectedName: StateFlow<String?> = _connectedName.asStateFlow()

	private var router: MediaRouter? = null
	private var discovering = false

	private val selector: MediaRouteSelector by lazy {
		MediaRouteSelector.Builder()
			.addControlCategory(
				CastMediaControlIntent.categoryForCast(
					CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID
				)
			)
			.build()
	}

	private val callback = object : MediaRouter.Callback() {
		override fun onRouteAdded(router: MediaRouter, route: MediaRouter.RouteInfo) =
			refreshRoutes()

		override fun onRouteChanged(router: MediaRouter, route: MediaRouter.RouteInfo) =
			refreshRoutes()

		override fun onRouteRemoved(router: MediaRouter, route: MediaRouter.RouteInfo) =
			refreshRoutes()

		override fun onRouteSelected(
			router: MediaRouter,
			route: MediaRouter.RouteInfo,
			reason: Int
		) {
			if (route.matchesSelector(selector)) _connectedName.value = route.name
		}

		override fun onRouteUnselected(
			router: MediaRouter,
			route: MediaRouter.RouteInfo,
			reason: Int
		) {
			_connectedName.value = null
		}
	}

	override fun startDiscovery() {
		mainHandler.post {
			try {
				val r = router ?: MediaRouter.getInstance(context).also { router = it }
				if (!discovering) {
					r.addCallback(
						selector, callback,
						MediaRouter.CALLBACK_FLAG_PERFORM_ACTIVE_SCAN
					)
					discovering = true
				}
				refreshRoutes()
			} catch (e: Exception) {
				// Play Services / Cast framework unavailable — keep the empty list.
				Logger.e("AndroidCastManager", "cast discovery unavailable", e)
			}
		}
	}

	override fun stopDiscovery() {
		mainHandler.post {
			if (discovering) {
				router?.removeCallback(callback)
				discovering = false
			}
			// Intentionally NOT clearing _devices: the cached list keeps the
			// picker instant on reopen; the next discovery refreshes it.
		}
	}

	override fun connect(deviceId: String) {
		mainHandler.post {
			router?.routes?.find { it.id == deviceId }?.select()
		}
	}

	override fun disconnect() {
		mainHandler.post {
			router?.unselect(MediaRouter.UNSELECT_REASON_DISCONNECTED)
		}
	}

	private fun refreshRoutes() {
		val r = router ?: return
		_devices.value = r.routes
			.filter { !it.isDefault && it.matchesSelector(selector) }
			.map { CastDevice(id = it.id, name = it.name) }
		r.selectedRoute.takeIf { it.matchesSelector(selector) }?.let {
			_connectedName.value = it.name
		}
	}
}
