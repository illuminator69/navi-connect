package paige.navic.domain.manager

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * What this device is doing about a Chromecast it can see on the LAN.
 *
 * A speaker reaches the picker as an ordinary hub device (`platform: "chromecast"`) once
 * *somebody* bridges it — so these states are about who is doing the bridging, which is the part
 * the user otherwise cannot see. Casting itself is then just a normal transfer.
 */
enum class CastBridgeState {
	/** Seen, nothing decided yet. */
	IDLE,

	/** We're about to register it with the hub (after the anti-collision delay). */
	CLAIMING,

	/** This device is presenting it to the hub. */
	BRIDGING,

	/** Another client — normally Feishin — already registered it, so we stay out of the way. */
	BRIDGED_ELSEWHERE,

	/** Found, but there's no hub to publish it to. */
	HUB_DISABLED
}

/** A Chromecast found by mDNS, with what we're doing about it. */
data class CastSpeaker(
	val id: String,
	val name: String,
	val state: CastBridgeState
)

/**
 * Read-only view of Chromecast discovery for the picker.
 *
 * There is no longer a second, Play-Services path alongside this one: MediaRouter never reported
 * a single Cast route on the target device, and the framework's own local-network device-consent
 * dialog reappeared on a loop without ever granting anything. Navic speaks mDNS and castv2
 * directly instead (`domain.manager.cast`), which is also what Feishin does.
 *
 * Implemented on Android by `CastBridgeManager`; iOS gets [NoopCastBridgeStatus].
 */
interface CastBridgeStatus {
	val speakers: StateFlow<List<CastSpeaker>>
}

class NoopCastBridgeStatus : CastBridgeStatus {
	override val speakers: StateFlow<List<CastSpeaker>> =
		MutableStateFlow(emptyList<CastSpeaker>()).asStateFlow()
}
