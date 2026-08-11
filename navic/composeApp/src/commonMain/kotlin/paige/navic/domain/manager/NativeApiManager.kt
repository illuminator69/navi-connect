package paige.navic.domain.manager

import com.russhwolf.settings.Settings
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import paige.navic.util.core.Logger

/**
 * Minimal client for Navidrome's NATIVE REST API (not Subsonic) — needed for
 * features the Subsonic API doesn't expose, like smart playlists (`rules`
 * criteria JSON on `POST /api/playlist`).
 *
 * Auth: `POST {base}/auth/login {username,password}` returns a JWT in `token`;
 * subsequent requests carry `X-ND-Authorization: Bearer <token>`. The token is
 * cached and refreshed once on 401.
 */
class NativeApiManager(
	private val settings: Settings,
	private val preferenceManager: PreferenceManager
) {
	private var token: String? = null

	private val client = HttpClient {
		install(ContentNegotiation) {
			json(Json {
				ignoreUnknownKeys = true
				isLenient = true
				coerceInputValues = true
			})
		}
	}

	private fun base(): String = settings.getString("instanceUrl", "").trimEnd('/')

	private suspend fun login(): String {
		val response: JsonObject = client.post("${base()}/auth/login") {
			contentType(ContentType.Application.Json)
			preferenceManager.customHeadersMap().forEach { (key, value) -> header(key, value) }
			setBody(buildJsonObject {
				put("username", settings.getString("username", ""))
				put("password", settings.getString("password", ""))
			}.toString())
		}.body()
		val newToken = response["token"]?.jsonPrimitive?.content
			?: throw IllegalStateException("Navidrome login returned no token")
		token = newToken
		return newToken
	}

	private suspend fun authedPost(path: String, body: JsonObject): HttpResponse {
		val activeToken = token ?: login()
		var response = client.post("${base()}$path") {
			contentType(ContentType.Application.Json)
			header("X-ND-Authorization", "Bearer $activeToken")
			preferenceManager.customHeadersMap().forEach { (key, value) -> header(key, value) }
			setBody(body.toString())
		}
		if (response.status == HttpStatusCode.Unauthorized) {
			val freshToken = login()
			response = client.post("${base()}$path") {
				contentType(ContentType.Application.Json)
				header("X-ND-Authorization", "Bearer $freshToken")
				preferenceManager.customHeadersMap().forEach { (key, value) -> header(key, value) }
				setBody(body.toString())
			}
		}
		return response
	}

	/**
	 * Create a server-side smart playlist. Navidrome keeps it auto-updated;
	 * every Subsonic client (including this one) then sees it as a normal
	 * playlist with fresh contents.
	 */
	suspend fun createSmartPlaylist(
		name: String,
		comment: String,
		isPublic: Boolean,
		rules: JsonObject
	): Result<Unit> {
		return try {
			val response = authedPost("/api/playlist", buildJsonObject {
				put("name", name)
				put("comment", comment)
				put("public", isPublic)
				put("rules", rules)
			})
			if (response.status.value in 200..299) {
				Result.success(Unit)
			} else {
				val text = try {
					response.body<String>()
				} catch (_: Exception) {
					""
				}
				Result.failure(IllegalStateException("HTTP ${response.status.value} $text"))
			}
		} catch (e: Exception) {
			Logger.e("NativeApiManager", "createSmartPlaylist failed", e)
			Result.failure(e)
		}
	}
}
