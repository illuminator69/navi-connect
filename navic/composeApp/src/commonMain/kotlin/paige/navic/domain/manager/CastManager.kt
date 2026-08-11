package paige.navic.domain.manager

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

data class CastDevice(
	val id: String,
	val name: String
)

/**
 * Chromecast discovery + session control for the navi-connect device picker.
 *
 * Discovery is deliberately on-demand: the picker calls [startDiscovery] when
 * it opens and [stopDiscovery] when it closes (active scanning is battery
 * hungry). [devices] retains the last results after discovery stops, so a
 * reopened picker shows cached devices instantly while a fresh scan refreshes
 * them in the background.
 *
 * Android implements this with MediaRouter + the Cast framework; other
 * platforms use [NoopCastManager].
 */
interface CastManager {
	val devices: StateFlow<List<CastDevice>>

	/** Name of the connected Cast device, or null when not casting. */
	val connectedName: StateFlow<String?>

	fun startDiscovery()
	fun stopDiscovery()
	fun connect(deviceId: String)
	fun disconnect()
}

class NoopCastManager : CastManager {
	override val devices: StateFlow<List<CastDevice>> =
		MutableStateFlow(emptyList<CastDevice>()).asStateFlow()
	override val connectedName: StateFlow<String?> =
		MutableStateFlow<String?>(null).asStateFlow()

	override fun startDiscovery() {}
	override fun stopDiscovery() {}
	override fun connect(deviceId: String) {}
	override fun disconnect() {}
}
