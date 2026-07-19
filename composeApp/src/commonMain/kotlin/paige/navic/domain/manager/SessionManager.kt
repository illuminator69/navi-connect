package paige.navic.domain.manager

import com.russhwolf.settings.Settings
import com.russhwolf.settings.set
import dev.zt64.subsonic.client.SubsonicAuth
import dev.zt64.subsonic.client.SubsonicClient
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.UserAgent
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.plugins.defaultRequest
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okio.ByteString.Companion.encodeUtf8
import kotlin.random.Random

class SessionManager(
	private val settings: Settings,
	private val preferenceManager: PreferenceManager
) {
	private val _isLoggedIn = MutableStateFlow(false)
	val isLoggedIn: StateFlow<Boolean> = _isLoggedIn.asStateFlow()

	var api: SubsonicClient = createClient(
		instanceUrl = settings.getString("instanceUrl", ""),
		username = settings.getString("username", ""),
		password = settings.getString("password", ""),
	)
		private set

	init {
		_isLoggedIn.value = settings.getStringOrNull("username") != null
	}

	private fun createClient(
		instanceUrl: String,
		username: String,
		password: String,
	) = SubsonicClient.Companion(
		baseUrl = instanceUrl,
		auth = SubsonicAuth.Token(
			username = username,
			password = password,
		),
		client = "Navic",
		clientConfig = {
			install(UserAgent) {
				agent = "Navic"
			}

			// OkHttp's default 10s read timeout aborts library syncs when the
			// server is momentarily slow (e.g. mid-scan behind a tunnel).
			install(HttpTimeout) {
				connectTimeoutMillis = 15_000
				requestTimeoutMillis = 120_000
				socketTimeoutMillis = 60_000
			}

			val customHeaders = preferenceManager.customHeadersMap()
			if (customHeaders.isNotEmpty()) {
				defaultRequest {
					customHeaders.forEach { (key, value) -> header(key, value) }
				}
			}
		}
	)

	suspend fun login(
		instanceUrl: String,
		username: String,
		password: String
	) {
		val client = createClient(instanceUrl, username, password)

		try {
			client.ping()
		} catch (e: Exception) {
			throw Exception(
				"Failed to connect to the instance. Please check your credentials and try again.",
				e
			)
		}

		settings["instanceUrl"] = instanceUrl
		settings["username"] = username
		settings["password"] = password

		api = client
		_isLoggedIn.value = true
	}

	fun logout() {
		settings["username"] = null
		settings["password"] = null
		_isLoggedIn.value = false
	}

	fun refreshClient() {
		api = createClient(
			instanceUrl = settings.getString("instanceUrl", ""),
			username = settings.getString("username", ""),
			password = settings.getString("password", ""),
		)
	}

	fun getCoverArtUrl(coverArtId: String) = api.getCoverArtUrl(
		coverArtId,
		auth = true,
		size = "${preferenceManager.coverArtQuality.value}"
	)

	/**
	 * ALBUM artists via Subsonic `getArtists` (the canonical album-artist list,
	 * matching Feishin's "Album Artists"). search3 returns every track/featured
	 * artist too (a 7k mess), and the bundled library's getArtists deserializer
	 * silently drops index groups — so this is a raw, lenient parse of all
	 * `index[].artist[]` entries.
	 */
	suspend fun fetchAlbumArtists(): List<RawArtist> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val envelope: SubsonicArtistsEnvelope =
				httpClient.get("$base/rest/getArtists.view") {
					preferenceManager.customHeadersMap().forEach { (key, value) ->
						header(key, value)
					}
					parameter("u", username)
					parameter("t", token)
					parameter("s", salt)
					parameter("v", "1.16.1")
					parameter("c", "Navic")
					parameter("f", "json")
				}.body()
			envelope.response.artists?.index?.flatMap { it.artist } ?: emptyList()
		} finally {
			httpClient.close()
		}
	}

	/**
	 * OpenSubsonic `getSimilarSongs2` — ids of songs similar to a given
	 * song/album/artist id. Vanilla Navidrome serves a heuristic mix; with the
	 * AudioMuse-AI plugin installed the SAME endpoint returns sonic
	 * similarity, so this is also the AudioMuse integration point.
	 */
	suspend fun fetchSimilarSongIds(id: String, count: Int = 50): List<String> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val envelope: SubsonicSimilarEnvelope =
				httpClient.get("$base/rest/getSimilarSongs2.view") {
					preferenceManager.customHeadersMap().forEach { (key, value) ->
						header(key, value)
					}
					parameter("u", username)
					parameter("t", token)
					parameter("s", salt)
					parameter("v", "1.16.1")
					parameter("c", "Navic")
					parameter("f", "json")
					parameter("id", id)
					parameter("count", count)
				}.body()
			envelope.response.similarSongs2?.song?.map { it.id } ?: emptyList()
		} finally {
			httpClient.close()
		}
	}

	/**
	 * OpenSubsonic `getOpenSubsonicExtensions` — the names of the extensions the
	 * server advertises. Navidrome lists `sonicSimilarity` ONLY when a sonic
	 * similarity plugin (AudioMuse) is loaded, so this is the capability probe
	 * for [fetchSonicSimilarTrackIds] / [findSonicPathIds].
	 */
	suspend fun fetchOpenSubsonicExtensions(): List<String> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val envelope: SubsonicExtensionsEnvelope =
				httpClient.get("$base/rest/getOpenSubsonicExtensions.view") {
					preferenceManager.customHeadersMap().forEach { (key, value) ->
						header(key, value)
					}
					parameter("u", username)
					parameter("t", token)
					parameter("s", salt)
					parameter("v", "1.16.1")
					parameter("c", "Navic")
					parameter("f", "json")
				}.body()
			envelope.response.openSubsonicExtensions.map { it.name }
		} finally {
			httpClient.close()
		}
	}

	/**
	 * OpenSubsonic `getSonicSimilarTracks` — ids of sonically similar tracks,
	 * ordered by similarity. Unlike `getSimilarSongs2` (which Navidrome can serve
	 * heuristically via agents), this ALWAYS routes through the sonic plugin, so
	 * it's guaranteed AudioMuse when present. The server 404s when no plugin is
	 * loaded; guard with [fetchOpenSubsonicExtensions].
	 */
	suspend fun fetchSonicSimilarTrackIds(id: String, count: Int = 50): List<String> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val envelope: SubsonicSonicMatchEnvelope =
				httpClient.get("$base/rest/getSonicSimilarTracks.view") {
					preferenceManager.customHeadersMap().forEach { (key, value) ->
						header(key, value)
					}
					parameter("u", username)
					parameter("t", token)
					parameter("s", salt)
					parameter("v", "1.16.1")
					parameter("c", "Navic")
					parameter("f", "json")
					parameter("id", id)
					parameter("count", count)
				}.body()
			envelope.response.sonicMatch.map { it.entry.id }
		} finally {
			httpClient.close()
		}
	}

	/**
	 * OpenSubsonic `findSonicPath` — a sonic "journey": the ordered tracks that
	 * bridge [startId] and [endId] in audio-feature space (AudioMuse plugin).
	 * 404s without the plugin; guard with [fetchOpenSubsonicExtensions].
	 */
	suspend fun findSonicPathIds(startId: String, endId: String, count: Int = 25): List<String> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val envelope: SubsonicSonicMatchEnvelope =
				httpClient.get("$base/rest/findSonicPath.view") {
					preferenceManager.customHeadersMap().forEach { (key, value) ->
						header(key, value)
					}
					parameter("u", username)
					parameter("t", token)
					parameter("s", salt)
					parameter("v", "1.16.1")
					parameter("c", "Navic")
					parameter("f", "json")
					parameter("startSongId", startId)
					parameter("endSongId", endId)
					parameter("count", count)
				}.body()
			envelope.response.sonicMatch.map { it.entry.id }
		} finally {
			httpClient.close()
		}
	}

	/**
	 * Fetch ALL artists by paging Subsonic `search3` with an empty query.
	 *
	 * Why not the library's `getArtists()`: that endpoint returns nested
	 * index-groups (`<index><artist/>...`), and the bundled
	 * `subsonic-client` (beta) drops whole index groups while deserializing
	 * that shape — which is why only ~450 of several thousand artists showed
	 * up. `search3` returns a flat, pageable `artist` array (like `getAlbums`,
	 * which already works), so it sidesteps the broken path and returns the
	 * full set. Auth is the standard salted-token scheme; no credentials are
	 * placed in the URL beyond the one-way token.
	 */
	suspend fun fetchAllArtists(pageSize: Int = 500): List<RawArtist> {
		val base = settings.getString("instanceUrl", "").trimEnd('/')
		val username = settings.getString("username", "")
		val password = settings.getString("password", "")

		val salt = Random.nextBytes(12)
			.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
		val token = "$password$salt".encodeUtf8().md5().hex()
		val customHeaders = preferenceManager.customHeadersMap()

		val httpClient = HttpClient {
			install(ContentNegotiation) {
				json(Json {
					ignoreUnknownKeys = true
					isLenient = true
					coerceInputValues = true
				})
			}
			install(UserAgent) { agent = "Navic" }
		}

		return try {
			val all = mutableListOf<RawArtist>()
			var offset = 0
			while (true) {
				val envelope: SubsonicSearchEnvelope =
					httpClient.get("$base/rest/search3.view") {
						customHeaders.forEach { (key, value) -> header(key, value) }
						parameter("u", username)
						parameter("t", token)
						parameter("s", salt)
						parameter("v", "1.16.1")
						parameter("c", "Navic")
						parameter("f", "json")
						parameter("query", "")
						parameter("artistCount", pageSize)
						parameter("artistOffset", offset)
						parameter("albumCount", 0)
						parameter("songCount", 0)
					}.body()

				val batch = envelope.response.searchResult3?.artist ?: emptyList()
				all += batch
				if (batch.size < pageSize) break
				offset += pageSize
			}
			all
		} finally {
			httpClient.close()
		}
	}
}

@Serializable
data class SubsonicSearchEnvelope(
	@SerialName("subsonic-response") val response: SubsonicSearchBody = SubsonicSearchBody()
)

@Serializable
data class SubsonicSearchBody(
	val status: String = "ok",
	val searchResult3: SearchResult3Dto? = null
)

@Serializable
data class SearchResult3Dto(
	val artist: List<RawArtist> = emptyList()
)

@Serializable
data class RawArtist(
	val id: String,
	val name: String = "",
	val albumCount: Int = 0,
	val coverArt: String? = null,
	val artistImageUrl: String? = null,
	val starred: String? = null,
	val userRating: Int? = null,
	val sortName: String? = null,
	val musicBrainzId: String? = null
)

@Serializable
data class SubsonicSimilarEnvelope(
	@SerialName("subsonic-response") val response: SubsonicSimilarBody = SubsonicSimilarBody()
)

@Serializable
data class SubsonicSimilarBody(
	val status: String = "ok",
	val similarSongs2: SimilarSongs2Dto? = null
)

@Serializable
data class SimilarSongs2Dto(
	val song: List<RawSongId> = emptyList()
)

@Serializable
data class RawSongId(
	val id: String
)

@Serializable
data class SubsonicExtensionsEnvelope(
	@SerialName("subsonic-response") val response: SubsonicExtensionsBody = SubsonicExtensionsBody()
)

@Serializable
data class SubsonicExtensionsBody(
	val status: String = "ok",
	val openSubsonicExtensions: List<OpenSubsonicExtensionDto> = emptyList()
)

@Serializable
data class OpenSubsonicExtensionDto(
	val name: String = "",
	val versions: List<Int> = emptyList()
)

@Serializable
data class SubsonicSonicMatchEnvelope(
	@SerialName("subsonic-response") val response: SubsonicSonicMatchBody = SubsonicSonicMatchBody()
)

@Serializable
data class SubsonicSonicMatchBody(
	val status: String = "ok",
	val sonicMatch: List<SonicMatchDto> = emptyList()
)

@Serializable
data class SonicMatchDto(
	val entry: RawSongId = RawSongId(id = ""),
	val similarity: Double = 0.0
)

@Serializable
data class SubsonicArtistsEnvelope(
	@SerialName("subsonic-response") val response: SubsonicArtistsBody = SubsonicArtistsBody()
)

@Serializable
data class SubsonicArtistsBody(
	val status: String = "ok",
	val artists: ArtistsIndexDto? = null
)

@Serializable
data class ArtistsIndexDto(
	val index: List<ArtistIndexGroupDto> = emptyList()
)

@Serializable
data class ArtistIndexGroupDto(
	val name: String = "",
	val artist: List<RawArtist> = emptyList()
)
