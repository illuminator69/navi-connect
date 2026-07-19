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
import paige.navic.util.core.Logger

/**
 * Tier-2 AudioMuse-AI core API client (direct HTTP, `Authorization: Bearer
 * <API_TOKEN>`). This reaches the AudioMuse core service directly (not via
 * Navidrome), unlocking features the plugin can't expose — Sonic Fingerprint
 * first; later adaptive Mood Flow, chat/mood search.
 *
 * FAIL-SOFT by design: every call returns empty on missing config / error /
 * timeout, so callers transparently fall back to Tier 1. See
 * DESIGN-adaptive-audiomuse.md.
 */
class AudioMuseManager(
	private val settings: Settings,
	private val preferenceManager: PreferenceManager
) {
	val isConfigured: Boolean
		get() = preferenceManager.audioMuseUrl.isNotBlank() &&
			preferenceManager.audioMuseToken.isNotBlank()

	// The 2D mood centroid from the last Mood Flow alchemy call — drives the
	// adaptive visualizer's palette. Null until the first alchemy mix.
	private val _lastMoodCentroid = MutableStateFlow<List<Float>?>(null)
	val lastMoodCentroid: StateFlow<List<Float>?> = _lastMoodCentroid.asStateFlow()

	private fun newClient() = HttpClient {
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

	/**
	 * Sonic Fingerprint — ids of tracks recommended from the user's listening
	 * habits (GET /api/sonic_fingerprint/generate). Navidrome creds are passed so
	 * the core can read play history (the core may also have them configured
	 * server-side, in which case the params are ignored). [] on any failure.
	 */
	suspend fun fetchSonicFingerprintIds(count: Int = 50): List<String> {
		if (!isConfigured) return emptyList()

		val base = preferenceManager.audioMuseUrl.trimEnd('/')
		val token = preferenceManager.audioMuseToken
		val ndUser = settings.getString("username", "")
		val ndPass = settings.getString("password", "")

		val client = newClient()
		return try {
			val results: List<FingerprintItem> =
				client.get("$base/api/sonic_fingerprint/generate") {
					header("Authorization", "Bearer $token")
					parameter("n", count)
					parameter("navidrome_user", ndUser)
					parameter("navidrome_password", ndPass)
				}.body()
			results.map { it.itemId }
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "sonic fingerprint failed", e)
			emptyList()
		} finally {
			client.close()
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
		if (!isConfigured || addIds.isEmpty()) return emptyList()

		val base = preferenceManager.audioMuseUrl.trimEnd('/')
		val token = preferenceManager.audioMuseToken
		val items = addIds.map { AlchemyItem(op = "ADD", id = it) } +
			subtractIds.map { AlchemyItem(op = "SUBTRACT", id = it) }

		val client = newClient()
		return try {
			val resp: AlchemyResponse = client.post("$base/api/alchemy") {
				header("Authorization", "Bearer $token")
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
		} finally {
			client.close()
		}
	}

	/**
	 * CLAP text→audio search (POST /api/clap/search) — ids of tracks whose audio
	 * embedding best matches a free-text mood query. [] on any failure; a disabled
	 * (400) or not-yet-loaded (503) server returns an error body whose `results`
	 * defaults to empty, so it fails soft just like a network error.
	 */
	suspend fun fetchClapSearchIds(query: String, limit: Int = 100): List<String> {
		if (!isConfigured || query.isBlank()) return emptyList()

		val base = preferenceManager.audioMuseUrl.trimEnd('/')
		val token = preferenceManager.audioMuseToken

		val client = newClient()
		return try {
			val resp: ClapSearchResponse = client.post("$base/api/clap/search") {
				header("Authorization", "Bearer $token")
				contentType(ContentType.Application.Json)
				setBody(ClapSearchRequest(query = query.trim(), limit = limit))
			}.body()
			resp.results.map { it.itemId }
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "clap search failed", e)
			emptyList()
		} finally {
			client.close()
		}
	}

	/**
	 * Whether CLAP search is usable on the server (enabled + embeddings loaded).
	 * Used to gate the Mood Search UI. False on any failure.
	 */
	suspend fun isClapAvailable(): Boolean {
		if (!isConfigured) return false

		val base = preferenceManager.audioMuseUrl.trimEnd('/')
		val token = preferenceManager.audioMuseToken

		val client = newClient()
		return try {
			val stats: ClapStats = client.get("$base/api/clap/stats") {
				header("Authorization", "Bearer $token")
			}.body()
			stats.clapEnabled && (stats.loaded || stats.songCount > 0)
		} catch (e: Exception) {
			Logger.e("AudioMuseManager", "clap stats failed", e)
			false
		} finally {
			client.close()
		}
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

@Serializable
data class ClapStats(
	@SerialName("clap_enabled") val clapEnabled: Boolean = false,
	@SerialName("song_count") val songCount: Int = 0,
	val loaded: Boolean = false
)
