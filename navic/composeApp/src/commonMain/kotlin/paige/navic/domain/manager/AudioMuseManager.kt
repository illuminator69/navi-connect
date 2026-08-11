package paige.navic.domain.manager

import com.russhwolf.settings.Settings
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.UserAgent
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlin.time.Clock
import paige.navic.util.core.Logger

/**
 * Tier-2 AudioMuse-AI core API client, unlocking features the Navidrome plugin
 * (Tier 1) can't expose — Sonic Fingerprint, adaptive Mood Flow, CLAP mood search.
 *
 * TWO ROUTES, hub preferred (see DESIGN-hub-audiomuse-proxy.md):
 *  1. **Through the hub** — `<hub>/sonic/<route>` with `Authorization: Bearer <HUB_TOKEN>`.
 *     The hub holds the AudioMuse URL, the AudioMuse token and the Navidrome
 *     password server-side, so this device carries none of them, and Tier 2 works
 *     anywhere the hub is reachable without exposing the core API.
 *  2. **Direct** — the legacy `<audioMuseUrl>/api/<route>` with the per-device AudioMuse
 *     token. Kept as an explicit fallback for a LAN-only setup with no hub.
 *
 * FAIL-SOFT by design: every call returns empty on missing config / error /
 * timeout, so callers transparently fall back to Tier 1. See
 * DESIGN-adaptive-audiomuse.md.
 */
class AudioMuseManager(
	private val settings: Settings,
	private val preferenceManager: PreferenceManager
) {
	/** Which service a call is going to, and how it authenticates. */
	private data class Endpoint(val base: String, val token: String, val viaHub: Boolean)

	/**
	 * When the hub told us its proxy isn't configured (`configured:false` from the
	 * probe), so we stop preferring a route that can't work and fall through to any
	 * direct config. Expires after [HUB_DEMOTE_TTL_MS] — the hub may gain an
	 * AUDIOMUSE_URL without this app restarting. Also cleared by a hub-settings change.
	 */
	private var hubDemotedAtMs = 0L
	private var lastHubConfig = ""

	private val hubProxyUsable: Boolean
		get() = hubDemotedAtMs == 0L ||
			Clock.System.now().toEpochMilliseconds() - hubDemotedAtMs > HUB_DEMOTE_TTL_MS

	/**
	 * `ws://host:4790` → `http://host:4790`. The proxy is plain HTTP on the hub's
	 * WebSocket port, so the base is the same URL with the scheme swapped and the
	 * `/connect` path (if any) dropped.
	 */
	private fun hubProxyBase(): String? {
		val raw = preferenceManager.hubUrl.trim()
		if (raw.isBlank() || preferenceManager.hubToken.isBlank()) return null
		// A hub URL/token change invalidates what we learned about the old hub.
		val sig = "$raw|${preferenceManager.hubToken}"
		if (sig != lastHubConfig) {
			lastHubConfig = sig
			hubDemotedAtMs = 0L
		}
		val http = when {
			raw.startsWith("wss://") -> "https://" + raw.removePrefix("wss://")
			raw.startsWith("ws://") -> "http://" + raw.removePrefix("ws://")
			raw.startsWith("http://") || raw.startsWith("https://") -> raw
			else -> "http://$raw"
		}
		return http.trimEnd('/').removeSuffix("/connect").trimEnd('/')
	}

	private fun directEndpoint(): Endpoint? {
		val base = preferenceManager.audioMuseUrl.trim().trimEnd('/')
		val token = preferenceManager.audioMuseToken
		return if (base.isNotBlank() && token.isNotBlank()) Endpoint(base, token, false) else null
	}

	/** The route the next call should take, or null when Tier 2 isn't configured at all. */
	private fun endpoint(): Endpoint? {
		val hub = hubProxyBase()
		if (hub != null && hubProxyUsable) return Endpoint(hub, preferenceManager.hubToken, true)
		return directEndpoint()
	}

	/** Hub path when routed through the proxy, upstream path when going direct. */
	private fun Endpoint.url(hubPath: String, directPath: String): String =
		base + (if (viaHub) hubPath else directPath)

	val isConfigured: Boolean
		get() = endpoint() != null

	/**
	 * Changes whenever the Tier-2 route configuration does. Preferences here are plain
	 * delegated properties with no Flow behind them, so a probe can't *observe* a settings
	 * change — but keying a `LaunchedEffect` on this re-probes as soon as the screen next
	 * recomposes or is re-entered, instead of caching a stale "unavailable" for the
	 * lifetime of the composition.
	 */
	val routeSignature: String
		get() = "${preferenceManager.hubUrl}|${preferenceManager.hubToken.isNotBlank()}|" +
			"${preferenceManager.audioMuseUrl}|${preferenceManager.audioMuseToken.isNotBlank()}"

	// The 2D mood centroid from the last Mood Flow alchemy call — drives the
	// adaptive visualizer's palette. Null until the first alchemy mix.
	private val _lastMoodCentroid = MutableStateFlow<List<Float>?>(null)
	val lastMoodCentroid: StateFlow<List<Float>?> = _lastMoodCentroid.asStateFlow()

	// One client reused for the manager's lifetime. The old per-call new/close
	// built and tore down a full HttpClient on the hot autoplay top-up path.
	private val client by lazy {
		HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
			// Tier-2 endpoints are synchronous in-memory lookups; keep timeouts tight
			// so a slow/unreachable core fails fast and we fall back to Tier 1.
			install(HttpTimeout) {
				connectTimeoutMillis = 8_000
				requestTimeoutMillis = 30_000
				socketTimeoutMillis = 30_000
			}
		}
	}

	/**
	 * Sonic Fingerprint — ids of tracks recommended from the user's listening
	 * habits (GET /sonic/fingerprint, or /api/sonic_fingerprint/generate direct).
	 * The core needs Navidrome creds to read play history: through the hub they are
	 * injected server-side from HUB_ND_USER/HUB_ND_PASS and this device sends none;
	 * on the direct route they still come from local settings. [] on any failure.
	 */
	suspend fun fetchSonicFingerprintIds(count: Int = 50): List<String> {
		val ep = endpoint() ?: return emptyList()

		return try {
			val results: List<FingerprintItem> =
				client.get(ep.url("/sonic/fingerprint", "/api/sonic_fingerprint/generate")) {
					header("Authorization", "Bearer ${ep.token}")
					parameter("n", count)
					if (!ep.viaHub) {
						parameter("navidrome_user", settings.getString("username", ""))
						parameter("navidrome_password", settings.getString("password", ""))
					}
				}.body()
			results.map { it.itemId }
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "sonic fingerprint failed", e)
			emptyList()
		}
	}

	/**
	 * Song Alchemy (POST /api/alchemy) — blend [addIds] (pull toward) and
	 * [subtractIds] (push away) into a centroid and return the nearest tracks.
	 * This is the engine behind adaptive "Mood Flow": ADD = liked/played-through,
	 * SUBTRACT = skipped. At least one ADD is required. [] on any failure.
	 */
	suspend fun fetchAlchemyMixIds(
		addIds: List<String>,
		subtractIds: List<String>,
		count: Int = 50,
		temperature: Float? = null,
		subtractDistance: Float? = null
	): List<String> {
		if (addIds.isEmpty()) return emptyList()
		val ep = endpoint() ?: return emptyList()

		val items = addIds.map { AlchemyItem(op = "ADD", id = it) } +
			subtractIds.map { AlchemyItem(op = "SUBTRACT", id = it) }

		return try {
			val resp: AlchemyResponse = client.post(ep.url("/sonic/alchemy", "/api/alchemy")) {
				header("Authorization", "Bearer ${ep.token}")
				contentType(ContentType.Application.Json)
				setBody(
					AlchemyRequest(
						items = items,
						n = count,
						temperature = temperature,
						subtractDistance = subtractDistance
					)
				)
			}.body()
			resp.centroid2d?.let { if (it.size >= 2) _lastMoodCentroid.value = it }
			resp.results.map { it.itemId }
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "alchemy mix failed", e)
			emptyList()
		}
	}

	/**
	 * CLAP text→audio search (POST /api/clap/search) — ids of tracks whose audio
	 * embedding best matches a free-text mood query. [] on any failure; a disabled
	 * (400) or not-yet-loaded (503) server returns an error body whose `results`
	 * defaults to empty, so it fails soft just like a network error.
	 */
	suspend fun fetchClapSearchIds(query: String, limit: Int = 100): List<String> {
		if (query.isBlank()) return emptyList()
		val ep = endpoint() ?: return emptyList()

		return try {
			val url = ep.url("/sonic/clap/search", "/api/clap/search")
			val resp: ClapSearchResponse = client.post(url) {
				header("Authorization", "Bearer ${ep.token}")
				contentType(ContentType.Application.Json)
				setBody(ClapSearchRequest(query = query.trim(), limit = limit))
			}.body()
			resp.results.map { it.itemId }
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "clap search failed", e)
			emptyList()
		}
	}

	/**
	 * Whether CLAP search is usable on the server (enabled + embeddings loaded).
	 * Used to gate the Mood Search UI. False on any failure.
	 *
	 * Doubles as the Tier-2 route probe: the hub answers with its own
	 * `configured`/`upstreamReachable` alongside the upstream stats, so a hub with
	 * no AUDIOMUSE_URL demotes us to the direct route (if any) instead of leaving
	 * Tier 2 silently dead.
	 */
	suspend fun isClapAvailable(): Boolean = clapAvailability().usable

	/**
	 * [isClapAvailable] plus *why*, so the UI can say what's wrong instead of silently
	 * hiding the entry point (a missing feature and a misconfigured one look identical
	 * otherwise, which is exactly how a broken hub route went unnoticed).
	 */
	suspend fun clapAvailability(): ClapAvailability {
		val ep = endpoint() ?: return ClapAvailability.NOT_CONFIGURED
		val stats = fetchClapStats(ep)
		if (ep.viaHub) {
			// Two ways the hub route can be useless: it reports no AudioMuse configured,
			// or it doesn't answer with anything parseable at all — unreachable, or too
			// old to route /sonic/* (in which case the WebSocket handshake answers a
			// 426 text/plain, which is not JSON). Both mean "try the direct route".
			// Only handling the first left a device with a perfectly good direct config
			// with Tier 2 silently dead, because endpoint() always prefers the hub.
			if (stats == null || stats.configured == false) {
				hubDemotedAtMs = Clock.System.now().toEpochMilliseconds()
				val direct = directEndpoint()
					?: return if (stats == null) ClapAvailability.HUB_UNREACHABLE
					else ClapAvailability.NOT_CONFIGURED
				val fallback = fetchClapStats(direct) ?: return ClapAvailability.UNREACHABLE
				return fallback.toAvailability()
			}
			if (stats.upstreamReachable == false) return ClapAvailability.UNREACHABLE
			if (stats.configured == true) hubDemotedAtMs = 0L
		}
		if (stats == null) return ClapAvailability.UNREACHABLE
		return stats.toAvailability()
	}

	private fun ClapStats.toAvailability(): ClapAvailability = when {
		!clapEnabled -> ClapAvailability.DISABLED_ON_SERVER
		!(loaded || songCount > 0) -> ClapAvailability.NOT_ANALYZED
		else -> ClapAvailability.AVAILABLE
	}

	private companion object {
		/** How long a "hub proxy not configured" answer keeps us on the direct route. */
		const val HUB_DEMOTE_TTL_MS = 10 * 60 * 1000L
	}

	private suspend fun fetchClapStats(ep: Endpoint): ClapStats? = try {
		client.get(ep.url("/sonic/clap/stats", "/api/clap/stats")) {
			header("Authorization", "Bearer ${ep.token}")
		}.body()
	} catch (e: Exception) {
		Logger.e("AudioMuseManager", "clap stats failed", e)
		null
	}
}

@Serializable
data class AlchemyRequest(
	val items: List<AlchemyItem>,
	val n: Int,
	// Omitted from the JSON when null (encodeDefaults=false) → server defaults apply.
	val temperature: Float? = null,
	@SerialName("subtract_distance") val subtractDistance: Float? = null
)

@Serializable
data class AlchemyItem(
	val op: String,
	val id: String,
	val type: String = "song"
)

@Serializable
data class AlchemyResponse(
	val results: List<FingerprintItem> = emptyList(),
	@SerialName("centroid_2d") val centroid2d: List<Float>? = null
)

@Serializable
data class FingerprintItem(
	@SerialName("item_id") val itemId: String = ""
)

@Serializable
data class ClapSearchRequest(
	val query: String,
	val limit: Int
)

@Serializable
data class ClapSearchResponse(
	val results: List<FingerprintItem> = emptyList()
)

/** Why CLAP mood search is or isn't usable — drives the Search entry's enabled state. */
enum class ClapAvailability(val usable: Boolean) {
	AVAILABLE(true),

	/** No Tier-2 route at all: neither a hub URL+token nor a direct AudioMuse URL+token. */
	NOT_CONFIGURED(false),

	/** The hub answered nothing usable and there's no direct config to fall back to. */
	HUB_UNREACHABLE(false),

	/** A route exists but the AudioMuse core didn't answer. */
	UNREACHABLE(false),

	/** Reached the core, but CLAP is switched off there. */
	DISABLED_ON_SERVER(false),

	/** CLAP is on, but no embeddings are loaded yet — the library hasn't been analyzed. */
	NOT_ANALYZED(false)
}

@Serializable
data class ClapStats(
	@SerialName("clap_enabled") val clapEnabled: Boolean = false,
	@SerialName("song_count") val songCount: Int = 0,
	val loaded: Boolean = false,
	// Hub-proxy only (PROTOCOL §14): whether the hub has AUDIOMUSE_URL set and
	// could reach it. Null on the direct route.
	val configured: Boolean? = null,
	val upstreamReachable: Boolean? = null
)
