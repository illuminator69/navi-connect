package paige.navic.domain.manager

import kotlinx.coroutines.flow.StateFlow

expect class ConnectivityManager {
	val isCellular: StateFlow<Boolean>
	val isOnline: StateFlow<Boolean>

	/** Whether the device is currently charging (AC/USB/wireless). Used to gate charging-only downloads. */
	val isCharging: StateFlow<Boolean>
}
