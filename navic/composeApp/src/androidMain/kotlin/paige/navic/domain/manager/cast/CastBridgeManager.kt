package paige.navic.domain.manager.cast

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import paige.navic.domain.manager.CastBridgeState
import paige.navic.domain.manager.CastBridgeStatus
import paige.navic.domain.manager.CastSpeaker
import paige.navic.domain.manager.HubDevice
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.util.core.Logger
import kotlin.random.Random

private const val TAG = "CastBridgeManager"

/**
 * How long to stay out of the way after the hub supersedes one of our bridges.
 *
 * Long on purpose. Being superseded means another client (Feishin) claimed the same `cast-<id>`,
 * and a quick retry is how you get two clients kicking each other off forever.
 */
private const val STAND_DOWN_MS = 5 * 60 * 1_000L

/** Random pre-claim delay, so two clients starting together don't both register at once. */
private const val CLAIM_JITTER_MAX_MS = 3_000L

/**
 * How long a bridged speaker may be absent from mDNS before we even consider it gone.
 *
 * Generous because the common cause of an absence is not the speaker. Wi-Fi power save drops
 * multicast when the screen goes off, so a locked phone stops hearing announcements from a speaker
 * that is sitting there playing perfectly well.
 */
private const val MISSING_GRACE_MS = 90_000L

/**
 * Re-run [CastBridgeManager.reconcile] on a timer as well as on events.
 *
 * Every deadline in here — the stand-down after being superseded, the grace above — expires with
 * nothing to announce it. Driven purely by the device flows, they would only be noticed the next
 * time something else changed, which in a quiet house is never.
 */
private const val RECONCILE_TICK_MS = 30_000L

/**
 * Owns Chromecast discovery and decides which speakers *this* device bridges into the hub.
 *
 * Feishin does the same job (`cast/index.ts`'s module-level `bridges` map), with one difference
 * that matters: on a desktop there is only ever one bridging process, whereas now both Navic and
 * Feishin can see the same speaker. They would register the same `cast-<id>` and the hub would
 * close the older socket with 4003 (`hub.py:1354`) — then the loser reconnects and kicks the
 * winner, forever. Everything about arbitration here exists to make that impossible.
 */
class CastBridgeManager(
	context: Context,
	private val hubManager: HubManager,
	private val preferenceManager: PreferenceManager
) : CastBridgeStatus {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
	private val discovery = CastDiscovery(context)

	private val bridges = mutableMapOf<String, CastDeviceBridge>()
	private val bridgesMutex = Mutex()

	/** deviceId → epoch ms until which we must not try to bridge it again. */
	private val standDownUntil = mutableMapOf<String, Long>()

	/** Serialises [reconcile]: it suspends now (reachability probes), and the ticker races the flows. */
	private val reconcileMutex = Mutex()

	/** Last good discovery record per speaker, so one that stops announcing keeps its name/address. */
	private val lastSeen = mutableMapOf<String, DiscoveredCast>()

	/** deviceId → epoch ms discovery stopped announcing it. Absent while it is being announced. */
	private val missingSince = mutableMapOf<String, Long>()

	/**
	 * Bumped when a bridge's hub connection comes up or goes down, to re-run [reconcile].
	 *
	 * Discovery and the hub device list are not enough on their own: a bridge that exists but has
	 * never reached the hub is a state change nobody else reports, and it is exactly the state the
	 * picker most needs to show honestly.
	 */
	private val bridgeStateChanged = MutableStateFlow(0)

	private val _speakers = MutableStateFlow<List<CastSpeaker>>(emptyList())

	/** What the picker shows: every speaker we can see, and what we're doing about it. */
	override val speakers: StateFlow<List<CastSpeaker>> = _speakers.asStateFlow()

	fun start() {
		discovery.start()
		scope.launch {
			// Re-evaluate whenever the world changes: a new speaker appears, a speaker moves,
			// or the hub's device list shifts (which is how "Feishin just quit" reaches us).
			// bridgeStateChanged is in the combine deliberately: a bridge whose hub socket just
			// dropped is a state change nothing else reports, and it is precisely the state the
			// picker must show honestly. Without it the manager reported BRIDGING off the mere
			// existence of a bridge object, so a bridge looping on a refused hub connection was
			// advertised as ready to receive a transfer.
			combine(discovery.devices, hubManager.devices, bridgeStateChanged) { found, hubDevices, _ ->
				found to hubDevices
			}.collect { (found, hubDevices) -> reconcile(found, hubDevices) }
		}
		scope.launch {
			while (isActive) {
				delay(RECONCILE_TICK_MS)
				reconcile(discovery.devices.value, hubManager.devices.value)
			}
		}
	}

	fun stop() {
		discovery.stop()
		scope.launch {
			bridgesMutex.withLock {
				bridges.values.forEach { it.destroy() }
				bridges.clear()
			}
		}
	}

	private suspend fun reconcile(found: List<DiscoveredCast>, hubDevices: List<HubDevice>) =
		reconcileMutex.withLock { reconcileLocked(found, hubDevices) }

	private suspend fun reconcileLocked(found: List<DiscoveredCast>, hubDevices: List<HubDevice>) {
		val hubEnabled = preferenceManager.hubEnabled
		val hubUrl = preferenceManager.hubUrl
		val token = preferenceManager.hubToken

		if (!hubEnabled || hubUrl.isBlank()) {
			// Nothing to bridge into. Still publish what we found, so the picker can say why.
			bridgesMutex.withLock {
				bridges.values.forEach { it.destroy() }
				bridges.clear()
			}
			missingSince.clear()
			lastSeen.clear()
			_speakers.value = found.map {
				CastSpeaker(it.id, it.name, CastBridgeState.HUB_DISABLED)
			}
			return
		}

		val now = System.currentTimeMillis()
		val states = mutableMapOf<String, CastBridgeState>()
		found.forEach { lastSeen[it.id] = it; missingSince.remove(it.id) }
		val effective = found + retainMissingBridges(found.map { it.id }.toSet(), now)

		bridgesMutex.withLock {
			for (device in effective) {
				val hubId = "cast-${device.id}"
				val existing = bridges[device.id]

				if (existing != null) {
					// Keep the address current: a speaker that took a new DHCP lease keeps its
					// id, and pinning the first-seen host is how a bridge dials a dead IP forever.
					existing.updateHost(device.host)
					existing.friendlyName = device.name
					// CLAIMING until the hub says welcome. A bridge object that exists proves only
					// that we intend to bridge this speaker — CastScrobbler gates on BRIDGING to
					// decide it is the one client responsible for the play, so the difference is
					// the difference between one scrobble and none.
					states[device.id] =
						if (existing.connected.value) CastBridgeState.BRIDGING else CastBridgeState.CLAIMING
					continue
				}

				val standDown = standDownUntil[device.id]
				if (standDown != null && now < standDown) {
					states[device.id] = CastBridgeState.BRIDGED_ELSEWHERE
					continue
				}

				// Somebody else is already presenting this speaker to the hub. Stand down —
				// registering would kick them off and start the war this whole block prevents.
				val takenByOther = hubDevices.any { it.id == hubId && it.online }
				if (takenByOther) {
					states[device.id] = CastBridgeState.BRIDGED_ELSEWHERE
					continue
				}

				states[device.id] = CastBridgeState.CLAIMING
				scope.launch { claimAfterJitter(device, hubUrl, token) }
			}
		}

		_speakers.value = effective.map {
			CastSpeaker(it.id, it.name, states[it.id] ?: CastBridgeState.IDLE)
		}
	}

	/**
	 * Which bridged-but-unannounced speakers to keep, tearing down the ones that are really gone.
	 *
	 * Discovery going quiet used to be treated as proof: the bridge was destroyed, its hub socket
	 * closed, and the hub — seeing its active receiver disconnect — cleared the active slot and
	 * marked the session stopped. A speaker that was still audibly playing therefore disappeared
	 * from every client a couple of minutes after the phone was locked, because that is when the
	 * phone stops listening to multicast, not because anything happened to the speaker.
	 *
	 * So mDNS gets a grace period and then has to be corroborated: a plain TCP connect to the last
	 * known address answers the only question that matters. Only a speaker that has been quiet AND
	 * cannot be reached loses its bridge.
	 */
	private suspend fun retainMissingBridges(foundIds: Set<String>, now: Long): List<DiscoveredCast> {
		val missing = bridgesMutex.withLock { bridges.keys.toList() }.filterNot { it in foundIds }
		val retained = mutableListOf<DiscoveredCast>()
		for (id in missing) {
			val bridge = bridgesMutex.withLock { bridges[id] }
			if (bridge == null) {
				missingSince.remove(id)
				continue
			}
			val since = missingSince.getOrPut(id) { now }
			val keep = now - since < MISSING_GRACE_MS || bridge.speakerReachable()
			if (keep) {
				// Reachable means the announcement is what's missing, not the speaker: restart the
				// clock so a long session isn't torn down the moment one probe happens to fail.
				if (now - since >= MISSING_GRACE_MS) missingSince[id] = now
				lastSeen[id]?.let { retained += it }
				continue
			}
			Logger.i(TAG, "${bridge.friendlyName}: gone from mDNS and unreachable — bridge torn down")
			bridgesMutex.withLock { bridges.remove(id)?.destroy() }
			missingSince.remove(id)
			lastSeen.remove(id)
		}
		return retained
	}

	/**
	 * Wait a random moment, re-check, then register.
	 *
	 * The delay is the cheap half of arbitration: if Navic and Feishin start within the same
	 * second, whoever draws the shorter wait registers first and the other sees them in the
	 * device list and stands down — no frames exchanged, nobody kicked.
	 */
	private suspend fun claimAfterJitter(device: DiscoveredCast, hubUrl: String, token: String) {
		delay(Random.nextLong(CLAIM_JITTER_MAX_MS))
		val hubId = "cast-${device.id}"
		if (hubManager.devices.value.any { it.id == hubId && it.online }) {
			Logger.i(TAG, "${device.name}: claimed by another client while we waited")
			return
		}
		bridgesMutex.withLock {
			if (bridges.containsKey(device.id)) return
			Logger.i(TAG, "${device.name}: bridging as $hubId")
			val bridge = CastDeviceBridge(
				deviceId = device.id,
				friendlyName = device.name,
				host = device.host,
				hubUrl = hubUrl,
				token = token,
				discovery = discovery,
				ownerDeviceId = { hubManager.myDeviceId.value },
				onSuperseded = { onSuperseded(device.id) }
			)
			bridges[device.id] = bridge
			bridge.start()
			// Nudge reconcile when this bridge's hub link comes up or goes down, so the picker
			// state above tracks reality instead of intent.
			scope.launch {
				bridge.connected.collect { bridgeStateChanged.value = bridgeStateChanged.value + 1 }
			}
		}
	}

	/**
	 * The hub told one of our bridges another socket took its device id.
	 *
	 * The circuit breaker: drop the bridge and refuse to re-register for [STAND_DOWN_MS]. Without
	 * this the reconnect loop would immediately supersede the client that just superseded us.
	 */
	private fun onSuperseded(deviceId: String) {
		scope.launch {
			bridgesMutex.withLock {
				bridges.remove(deviceId)?.destroy()
				standDownUntil[deviceId] = System.currentTimeMillis() + STAND_DOWN_MS
			}
			_speakers.value = _speakers.value.map {
				if (it.id == deviceId) it.copy(state = CastBridgeState.BRIDGED_ELSEWHERE) else it
			}
		}
	}
}
