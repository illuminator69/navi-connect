package paige.navic.domain.manager.cast

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import paige.navic.util.core.Logger
import java.net.Inet4Address

private const val TAG = "CastDiscovery"

private const val SERVICE_TYPE = "_googlecast._tcp."

// A Chromecast announces itself only every couple of minutes, so a single lost query — the Wi-Fi
// interface not up yet, a switch dropping the first multicast — used to cost ~90s before the
// speaker appeared. (Feishin hit exactly this; see its REQUERY_* constants.) Re-query fast while
// we are likely still missing devices, then slowly, which also picks up anything powered on later.
private const val REQUERY_FAST_MS = 3_000L
private const val REQUERY_FAST_TRIES = 10
private const val REQUERY_SLOW_MS = 60_000L

/**
 * How long a speaker stays in the list after it was last announced.
 *
 * There is no removal path worth relying on. `onServiceLost` only fires from a live listener, and
 * every loss emitted while a listener is being torn down is discarded on purpose (see
 * [CastDiscovery.endDiscovery]) — otherwise the 60 s re-query ladder wiped the picker every minute.
 * The result was a list that only ever grew: a speaker seen once on a friend's Wi-Fi stayed in it
 * for the life of the process. Generous on purpose — a Chromecast announces every couple of
 * minutes and we re-query every 60 s, so ten minutes of total silence really does mean gone.
 */
private const val DEVICE_TTL_MS = 10 * 60 * 1_000L

/** A Chromecast seen on the LAN. [id] is the mDNS TXT `id`, stable across address changes. */
data class DiscoveredCast(
	val id: String,
	val name: String,
	val host: String,
	val port: Int,
	/**
	 * The mDNS instance name (`Nest-Audio-<hex>._googlecast._tcp`).
	 *
	 * Kept because it is the ONLY identifier a lost-service callback carries — it has no TXT
	 * record, so [id] and [name] are both unavailable at that point.
	 */
	val serviceName: String? = null
)

/**
 * mDNS discovery of Chromecasts, via Android's own [NsdManager].
 *
 * Deliberately not the Cast SDK: MediaRouter reports nothing on the target device for reasons we
 * could not pin down, and NsdManager has no Play Services dependency, needs no multicast lock, and
 * adds no library. Deliberately not jmDNS either, for the same "no new dependency" reason.
 *
 * Emits [devices] continuously; the manager above owns what to do about them.
 */
class CastDiscovery(private val context: Context) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
	private val nsd: NsdManager? by lazy {
		runCatching { context.getSystemService(Context.NSD_SERVICE) as? NsdManager }
			.onFailure { Logger.e(TAG, "NsdManager unavailable", it) }
			.getOrNull()
	}

	private val _devices = MutableStateFlow<List<DiscoveredCast>>(emptyList())
	val devices: StateFlow<List<DiscoveredCast>> = _devices.asStateFlow()

	private val _running = MutableStateFlow(false)
	val running: StateFlow<Boolean> = _running.asStateFlow()

	private var listener: NsdManager.DiscoveryListener? = null
	private var requeryJob: Job? = null

	/** deviceId → epoch ms of the last announcement. Drives [expireStaleDevices]. */
	private val lastSeen = mutableMapOf<String, Long>()

	/**
	 * Resolves must be serialised. Below API 34 a second `resolveService` while one is in flight
	 * fails the whole thing with FAILURE_ALREADY_ACTIVE, and with several speakers on the LAN
	 * that means most of them are never resolved at all.
	 */
	private val resolveMutex = Mutex()
	private val resolveQueue = ArrayDeque<NsdServiceInfo>()
	private var draining = false

	fun start() {
		if (_running.value) return
		_running.value = true
		beginDiscovery()
		requeryJob = scope.launch {
			var tries = 0
			while (isActive) {
				delay(if (tries < REQUERY_FAST_TRIES) REQUERY_FAST_MS else REQUERY_SLOW_MS)
				tries++
				restartDiscovery()
			}
		}
	}

	fun stop() {
		_running.value = false
		requeryJob?.cancel()
		requeryJob = null
		endDiscovery()
	}

	/**
	 * Ask again, right now.
	 *
	 * Called by a bridge whose connect just failed: the usual cause is a speaker that took a new
	 * DHCP lease, and waiting up to a minute for the next scheduled tick to learn its address is
	 * the difference between a blip and a dead device.
	 */
	fun requery() {
		if (!_running.value) return
		scope.launch { restartDiscovery() }
	}

	// ------------------------------------------------------------------ internals

	/**
	 * NsdManager's discovery is one-shot per listener — it reports what it finds and then goes
	 * quiet — so "re-query" means tearing the listener down and starting a fresh one. Known
	 * devices are kept across the restart so the picker doesn't flicker.
	 */
	private fun restartDiscovery() {
		endDiscovery()
		expireStaleDevices()
		if (_running.value) beginDiscovery()
	}

	/** Drop speakers nothing has announced for [DEVICE_TTL_MS]. */
	private fun expireStaleDevices() {
		val cutoff = System.currentTimeMillis() - DEVICE_TTL_MS
		val stale = _devices.value.filter { (lastSeen[it.id] ?: 0L) < cutoff }
		if (stale.isEmpty()) return
		stale.forEach {
			Logger.i(TAG, "${it.name}: not announced for ${DEVICE_TTL_MS / 60_000} min — forgetting it")
			lastSeen.remove(it.id)
		}
		val goneIds = stale.map { it.id }.toSet()
		_devices.value = _devices.value.filterNot { it.id in goneIds }
	}

	private fun beginDiscovery() {
		val manager = nsd ?: return
		val l = object : NsdManager.DiscoveryListener {
			override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
				Logger.w(TAG, "start discovery failed ($errorCode)")
			}

			override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {
				Logger.w(TAG, "stop discovery failed ($errorCode)")
			}

			override fun onDiscoveryStarted(serviceType: String?) = Unit
			override fun onDiscoveryStopped(serviceType: String?) = Unit

			override fun onServiceFound(service: NsdServiceInfo?) {
				service ?: return
				scope.launch { enqueueResolve(service) }
			}

			override fun onServiceLost(service: NsdServiceInfo?) {
				// Only the CURRENT listener may retract devices. Stopping a listener makes the
				// framework report everything it knew as lost, so without this the periodic
				// re-query wiped the list every time it cycled — the speaker vanished from the
				// picker on a timer and looked like it had gone off the network.
				if (listener !== this) return
				val lost = service?.serviceName ?: return
				// Match on the instance name only. Matching `id` too was wrong in the case that
				// matters: when a device has no TXT `id` we fall back to the instance name, so
				// `id == lost` could hold for a device this event was not about.
				_devices.value = _devices.value.filterNot { it.serviceName == lost }
			}
		}
		listener = l
		runCatching {
			manager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
		}.onFailure {
			Logger.e(TAG, "discoverServices threw", it)
			listener = null
		}
	}

	private fun endDiscovery() {
		val manager = nsd ?: return
		val l = listener ?: return
		listener = null
		// Throws IllegalArgumentException if the listener was never successfully registered
		// (or was already stopped) — harmless, and not worth tracking a second state flag for.
		runCatching { manager.stopServiceDiscovery(l) }
	}

	private suspend fun enqueueResolve(service: NsdServiceInfo) {
		resolveMutex.withLock {
			resolveQueue.addLast(service)
			if (draining) return
			draining = true
		}
		drainResolves()
	}

	private suspend fun drainResolves() {
		while (true) {
			val next = resolveMutex.withLock {
				resolveQueue.removeFirstOrNull().also { if (it == null) draining = false }
			} ?: return
			resolveOne(next)
		}
	}

	private suspend fun resolveOne(service: NsdServiceInfo) {
		val manager = nsd ?: return
		val done = kotlinx.coroutines.CompletableDeferred<NsdServiceInfo?>()
		val cb = object : NsdManager.ResolveListener {
			override fun onResolveFailed(serviceInfo: NsdServiceInfo?, errorCode: Int) {
				done.complete(null)
			}

			override fun onServiceResolved(serviceInfo: NsdServiceInfo?) {
				done.complete(serviceInfo)
			}
		}
		runCatching { @Suppress("DEPRECATION") manager.resolveService(service, cb) }
			.onFailure { done.complete(null) }

		val resolved = done.await() ?: return
		val device = resolved.toDiscoveredCast() ?: return
		upsert(device)
	}

	/**
	 * Add or update. Updating matters as much as adding: a speaker that took a new DHCP lease
	 * keeps its TXT `id` but changes address, and skipping it because we "already know" that id
	 * is how a bridge ends up dialling a dead IP forever.
	 */
	private fun upsert(device: DiscoveredCast) {
		lastSeen[device.id] = System.currentTimeMillis()
		val current = _devices.value
		val existing = current.firstOrNull { it.id == device.id }
		if (existing == device) return
		if (existing != null && existing.host != device.host) {
			Logger.i(TAG, "${device.name}: address changed ${existing.host} → ${device.host}")
		} else if (existing == null) {
			Logger.i(TAG, "found ${device.name} @ ${device.host}:${device.port}")
		}
		_devices.value = current.filterNot { it.id == device.id } + device
	}
}

/**
 * Reads the two TXT fields the bridge needs: `id` (stable device id) and `fn` (friendly name) —
 * the same pair Feishin's `serviceToDevice()` uses, so both clients derive the same `cast-<id>`
 * and can recognise each other's registrations.
 */
private fun NsdServiceInfo.toDiscoveredCast(): DiscoveredCast? {
	@Suppress("DEPRECATION")
	val address = host ?: return null
	// IPv4 only. A Chromecast answers on both, but its v6 address is frequently link-local, which
	// a plain socket connect can't use without a scope id.
	if (address !is Inet4Address) return null
	val hostAddress = address.hostAddress ?: return null

	val txt = attributes.orEmpty()
	fun txtValue(key: String): String? =
		txt[key]?.let { String(it, Charsets.UTF_8) }?.takeIf { it.isNotBlank() }

	return DiscoveredCast(
		id = txtValue("id") ?: serviceName ?: return null,
		name = txtValue("fn") ?: serviceName ?: "Chromecast",
		host = hostAddress,
		port = if (port > 0) port else CastProtocol.CAST_PORT,
		serviceName = serviceName
	)
}
