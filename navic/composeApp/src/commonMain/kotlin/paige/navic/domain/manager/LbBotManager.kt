package paige.navic.domain.manager

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.UserAgent
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.plugins.timeout
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlin.time.Clock
import paige.navic.util.core.Logger

/**
 * lb-bot: what the library is *missing*.
 *
 * lb-bot keeps a per-artist MusicBrainz discography index and a Soulseek
 * acquisition pipeline that can fill a gap — a release the library doesn't have
 * at all, or the three tracks missing from one it does.
 *
 * HUB ONLY, unlike [AudioMuseManager] next door. That one keeps a direct-LAN
 * fallback because AudioMuse has a bearer token of its own; lb-bot's Flask API has
 * no authentication whatsoever and exposes delete-file and trash routes, so the
 * hub's route whitelist is the only gate that exists. There is deliberately no
 * direct route here, and no lb-bot address is ever stored on the device: no hub
 * means the feature is simply off.
 *
 * FAIL-SOFT everywhere. A missing hub, an unset LBBOT_URL upstream, an unreachable
 * lb-bot and a never-indexed artist all resolve to "render nothing" — the artist
 * page must look exactly as it does today whenever this layer is absent.
 *
 * NOTE for future edits: do not write a route path containing a slash-star
 * sequence inside a comment. Kotlin block comments nest, and one of those swallows
 * the rest of the file behind an "Unclosed comment" at EOF.
 */
class LbBotManager(
	private val preferenceManager: PreferenceManager
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

	private val json = Json {
		ignoreUnknownKeys = true
		isLenient = true
		coerceInputValues = true
		encodeDefaults = false
	}

	private val client by lazy {
		HttpClient {
			install(ContentNegotiation) { json(json) }
			install(UserAgent) { agent = "Navic" }
			// The discography read is an instant SQLite lookup, but `album/releases`,
			// `album/tracklist` and the download POST all sit on rate-limited
			// MusicBrainz calls upstream, and a gap rescan walks a folder. The hub
			// gives those its slow timeout; match it here rather than failing a call
			// the hub is still happily servicing.
			install(HttpTimeout) {
				connectTimeoutMillis = 8_000
				requestTimeoutMillis = 60_000
				socketTimeoutMillis = 60_000
			}
		}
	}

	/**
	 * `ws://host:4790` -> `http://host:4790`. Same scheme swap AudioMuseManager does,
	 * plus one difference: the hub toggle is honoured. AudioMuse can tolerate ignoring
	 * it because a direct route exists; here the hub is the only route, so a user who
	 * switched the hub off should see no lb-bot traffic at all.
	 */
	private fun hubBase(): String? {
		if (!preferenceManager.hubEnabled) return null
		val raw = preferenceManager.hubUrl.trim()
		if (raw.isBlank() || preferenceManager.hubToken.isBlank()) return null
		val http = when {
			raw.startsWith("wss://") -> "https://" + raw.removePrefix("wss://")
			raw.startsWith("ws://") -> "http://" + raw.removePrefix("ws://")
			raw.startsWith("http://") || raw.startsWith("https://") -> raw
			else -> "http://$raw"
		}
		return http.trimEnd('/').removeSuffix("/connect").trimEnd('/')
	}

	/**
	 * Changes whenever the route configuration does, so a `LaunchedEffect` keyed on it
	 * re-probes instead of caching "unavailable" for the life of the composition.
	 * Preferences here are plain delegated properties with no Flow behind them.
	 */
	val routeSignature: String
		get() = "${preferenceManager.hubEnabled}|${preferenceManager.hubUrl}|" +
			"${preferenceManager.hubToken.isNotBlank()}"

	/** Bumped whenever something lands in the library. Screens re-read on a change. */
	private val _libraryRevision = MutableStateFlow(0L)
	val libraryRevision: StateFlow<Long> = _libraryRevision.asStateFlow()

	/** Latest poll for each watched album fill, keyed by release-group mbid. */
	private val _fills = MutableStateFlow<Map<String, LbFillStatus>>(emptyMap())
	val fills: StateFlow<Map<String, LbFillStatus>> = _fills.asStateFlow()

	/** Latest poll for each watched gap fill, keyed by lb-bot review group id. */
	private val _gaps = MutableStateFlow<Map<String, LbGap>>(emptyMap())
	val gaps: StateFlow<Map<String, LbGap>> = _gaps.asStateFlow()

	/**
	 * Every fill this device has started, newest first — in flight *and* finished.
	 *
	 * The watch map used to be pruned the moment a fill settled, which meant a failure
	 * was indistinguishable from never having happened: the sheet was long closed, the
	 * artist page showed the album as still missing, and there was nowhere to ask why.
	 * Keeping the terminal rows is what makes a downloads view, a retry button and a
	 * completion notice possible without any of them growing state of their own.
	 *
	 * It also covers a case nothing else can. lb-bot's own fill ledger is in memory and
	 * capped, so restarting it mid-fill makes `album/status` answer `unknown` forever;
	 * this is then the only record that the download was ever asked for.
	 */
	private val _ledger = MutableStateFlow<List<LbFillEntry>>(emptyList())
	val ledger: StateFlow<List<LbFillEntry>> = _ledger.asStateFlow()

	/**
	 * One event per fill reaching a terminal outcome, for whoever wants to announce it.
	 *
	 * Emitted from [settle] and nowhere else, so a sheet and a page both watching the
	 * same fill cannot produce two notifications. `extraBufferCapacity` because there is
	 * no subscriber at all until something mounts a collector, and a fill that lands
	 * while the app is backgrounded must not block the poll loop.
	 */
	private val _fillEvents = MutableSharedFlow<LbFillEvent>(extraBufferCapacity = 8)
	val fillEvents: SharedFlow<LbFillEvent> = _fillEvents.asSharedFlow()

	/** The user's sticky quality preference. Empty means "lb-bot's own default". */
	var preferredQuality: String
		get() = preferenceManager.lbBotQuality
		set(value) {
			preferenceManager.lbBotQuality = value
		}

	// ----- HTTP ------------------------------------------------------------- //

	/**
	 * Why a call failed, in terms a user can act on.
	 *
	 * This exists because the first version returned bare nulls and booleans and
	 * logged only *exceptions* — and ktor's default `expectSuccess = false` means a
	 * 404 or a 503 is an ordinary response, not an exception. A hub that didn't know
	 * a route therefore produced a button that did nothing, logged nothing, and left
	 * no way to tell "rejected" from "unreachable" from "lb-bot said no".
	 */
	sealed interface LbError {
		/** No hub configured, or the hub toggle is off. The surface should be hidden. */
		data object NotConfigured : LbError

		/** The hub proxies no such route: it is older than this client. */
		data object RouteUnknown : LbError

		/**
		 * lb-bot is alive but wouldn't answer in time — almost always because it is searching.
		 *
		 * Every lb-bot route takes one process-wide `_review_lock`, so while a source search runs
		 * an ordinary poll times out and the hub answers `{"error": "lb-bot unreachable"}`. Shown
		 * verbatim that is simply wrong, and it sends you to investigate a service that is working.
		 * Its own variant rather than a string so callers can decide it isn't worth surfacing.
		 */
		data object Busy : LbError

		/** Reached the hub; it or lb-bot refused. [message] is upstream's own words. */
		data class Rejected(val status: Int, val message: String) : LbError

		/** Never got an answer. */
		data class Unreachable(val message: String) : LbError
	}

	/** Success carries the parsed body; failure carries something to show the user. */
	sealed interface LbResult<out T> {
		data class Ok<T>(val value: T) : LbResult<T>
		data class Failed(val error: LbError) : LbResult<Nothing>
	}

	private fun <T> LbResult<T>.valueOrNull(): T? = (this as? LbResult.Ok)?.value

	/** lb-bot and the hub both answer errors as `{"error": "..."}`. */
	@Serializable
	private data class LbErrorBody(val error: String = "")

	private fun failureFor(status: Int, path: String, raw: String): LbResult.Failed {
		val message = try {
			json.decodeFromString<LbErrorBody>(raw).error
		} catch (e: Exception) {
			""
		}
		Logger.w("LbBotManager", "$path -> HTTP $status ${message.ifBlank { raw.take(200) }}")
		return LbResult.Failed(
			when (status) {
				404 -> LbError.RouteUnknown
				502, 504 -> LbError.Busy
				else -> LbError.Rejected(status, message)
			}
		)
	}

	/**
	 * @param timeoutMs overrides the client's 60 s for one call. Only the two polled
	 *   routes pass it: they run every five seconds, so a tick that hasn't answered in
	 *   twelve has already missed its slot and the next one will do — whereas holding
	 *   the connection for a full minute stalls every other watch behind it. The hub's
	 *   own timeouts (20 s for `album/status`, 45 s for the gap) sit above this on
	 *   purpose; the client is the side that has to give up first.
	 */
	private suspend inline fun <reified T> getJson(
		path: String,
		params: List<Pair<String, String>>,
		timeoutMs: Long? = null
	): LbResult<T> {
		val base = hubBase() ?: return LbResult.Failed(LbError.NotConfigured)
		return try {
			val response = client.get(base + path) {
				header("Authorization", "Bearer ${preferenceManager.hubToken}")
				params.forEach { (key, value) -> if (value.isNotBlank()) parameter(key, value) }
				if (timeoutMs != null) timeout {
					requestTimeoutMillis = timeoutMs
					socketTimeoutMillis = timeoutMs
				}
			}
			if (response.status.isSuccess()) LbResult.Ok(response.body<T>())
			else failureFor(response.status.value, "GET $path", response.bodyAsText())
		} catch (e: Exception) {
			Logger.e("LbBotManager", "GET $path failed", e)
			LbResult.Failed(LbError.Unreachable(e.message.orEmpty()))
		}
	}

	private suspend inline fun <reified B, reified T> postJson(
		path: String,
		body: B
	): LbResult<T> {
		val base = hubBase() ?: return LbResult.Failed(LbError.NotConfigured)
		return try {
			val response = client.post(base + path) {
				header("Authorization", "Bearer ${preferenceManager.hubToken}")
				contentType(ContentType.Application.Json)
				setBody(body)
			}
			if (!response.status.isSuccess()) {
				return failureFor(response.status.value, "POST $path", response.bodyAsText())
			}
			val parsed = response.body<T>()
			// lb-bot answers 200 with `{"ok": false}` for some refusals, so the
			// status code alone is not the verdict.
			if (parsed is LbOk && !parsed.ok) {
				LbResult.Failed(LbError.Rejected(200, parsed.error))
			} else {
				LbResult.Ok(parsed)
			}
		} catch (e: Exception) {
			Logger.e("LbBotManager", "POST $path failed", e)
			LbResult.Failed(LbError.Unreachable(e.message.orEmpty()))
		}
	}

	// ----- reads ------------------------------------------------------------ //

	/**
	 * Whether the lb-bot layer is usable at all. The hub answers this route even with
	 * no LBBOT_URL configured — that is the whole point of routing the prefix
	 * unconditionally, so "not configured" arrives as JSON rather than as the
	 * WebSocket upgrade's 426 text/plain.
	 */
	suspend fun probeAvailable(): Boolean {
		val probe = getJson<LbStatusProbe>("/lb/status", emptyList()).valueOrNull() ?: return false
		_hubRoutes.value = probe.routes
		return probe.configured && probe.upstreamReachable
	}

	/**
	 * Route names the hub advertised on the last probe, or empty if it is old enough
	 * not to advertise any. Used to tell "this hub can't do that" apart from "that
	 * failed", which otherwise look identical from here.
	 */
	private val _hubRoutes = MutableStateFlow<List<String>>(emptyList())

	/** False only when the hub answered a route list that doesn't include gap filling. */
	val supportsGapFilling: Boolean
		get() = _hubRoutes.value.isEmpty() || _hubRoutes.value.any { it == "POST /lb/gap/auto" }

	/** As [supportsGapFilling], for the source picker. */
	val supportsSourcePicker: Boolean
		get() = _hubRoutes.value.isEmpty() || _hubRoutes.value.any { it == "GET /lb/album/sources" }

	/**
	 * One artist's stored discography. An instant SQLite read upstream keyed by the
	 * Navidrome artist id this page already holds, so it is safe on every page open —
	 * the expensive MusicBrainz walk is only ever [indexArtist].
	 *
	 * `indexed == false` means "never scanned", which the UI offers to fix. It is not
	 * an error, and neither is a null return.
	 */
	suspend fun discography(ndId: String, mbid: String?): LbDiscography? {
		if (ndId.isBlank() && mbid.isNullOrBlank()) return null
		val raw = getJson<LbDiscography>(
			"/lb/artist/discography",
			listOf("nd_id" to ndId, "mbid" to (mbid ?: ""))
		).valueOrNull() ?: return null
		return if (raw.indexed) raw else LbDiscography(indexed = false)
	}

	/**
	 * Start the MusicBrainz walk for one artist. Slow (one request a second upstream,
	 * 10-60s for a real discography) and always an explicit user action.
	 *
	 * Returns whether the scan was accepted. The task id it answers with is
	 * deliberately discarded: polling lb-bot's task API deep-copies its entire review
	 * state under a process-wide lock, and it is not whitelisted on the hub. Re-read
	 * the (instant) discography instead and let the section appear when it appears.
	 */
	suspend fun indexArtist(mbid: String, name: String, ndId: String): Boolean {
		if (mbid.isBlank() || name.isBlank()) return false
		return postJson<_, LbOk>(
			"/lb/artist/discography",
			LbIndexRequest(mbid, name, ndId)
		) is LbResult.Ok
	}

	/** Editions of one release-group. Rate-limited MusicBrainz upstream — show a skeleton. */
	suspend fun albumReleases(rgid: String): LbReleaseDetail? {
		if (rgid.isBlank()) return null
		return getJson<LbReleaseDetail>("/lb/album/releases", listOf("rgid" to rgid)).valueOrNull()
	}

	/**
	 * Ranked Soulseek folders for a release-group — what a download would actually
	 * fetch, before fetching it.
	 *
	 * This is the answer to "did it grab the right record". Coverage here is matched
	 * against the canonical MusicBrainz tracklist rather than counted, so a folder
	 * holding a different album with the right number of files no longer reads as a
	 * complete match — which is precisely how a self-titled album goes wrong.
	 *
	 * Slow: a live slskd search fanning out to peers, tens of seconds. The hub caches
	 * it briefly so re-opening the sheet doesn't start another one.
	 */
	suspend fun albumSources(
		rgid: String,
		edition: LbResolvedEdition? = null
	): LbResult<LbAlbumSources> {
		if (rgid.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		return getJson("/lb/album/sources", buildList {
			add("rgid" to rgid)
			edition?.let {
				add("release_mbid" to it.releaseMbid)
				add("artist" to it.artist)
				add("album" to it.title)
				// Sizes lb-bot's slskd folder search; a wrong number costs match quality, so
				// send nothing rather than a zero we haven't actually counted.
				if (it.totalTracks > 0) add("total" to it.totalTracks.toString())
			}
		})
	}

	/**
	 * Canonical tracklist for one release. With [albumIds] or [groupId] every track
	 * also carries its own `present` flag; without them `presenceKnown` is false,
	 * which means "the library holds none of this" — not an error, and not `0/12`.
	 */
	suspend fun tracklist(
		releaseMbid: String,
		albumIds: List<String> = emptyList(),
		groupId: String = ""
	): LbTracklist? {
		if (releaseMbid.isBlank()) return null
		return getJson<LbTracklist>(
			"/lb/album/tracklist",
			listOf(
				"release_mbid" to releaseMbid,
				"album_ids" to albumIds.joinToString(","),
				"group_id" to groupId
			)
		).valueOrNull()
	}

	/**
	 * Ask lb-bot to acquire a whole release-group.
	 *
	 * Not fast: it resolves the release-group against MusicBrainz before answering, so
	 * the button needs a spinner. Idempotent upstream — a second tap (or a tap from
	 * another device) comes back `existing`, rather than starting a second search, a
	 * second set of transfers and a second placement pass over one folder.
	 */
	suspend fun download(
		rgid: String,
		quality: String,
		source: LbGapSource? = null,
		edition: LbResolvedEdition? = null
	): LbResult<LbDownloadResult> {
		// Never `release_mbid` without `rgid`: lb-bot would happily download it, but the task then
		// carries no release-group, so placement can't flip the index row to `present` and the
		// filled album double-lists as missing forever.
		if (rgid.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		// An empty quality is omitted rather than sent: lb-bot reads a missing key as
		// "use the global Source preference", while an explicit "" would make this
		// download look like an override in its own status view. Same for the source.
		val res = postJson<_, LbDownloadResponse>(
			"/lb/album/download",
			LbDownloadRequest(
				rgid = rgid,
				releaseMbid = edition?.releaseMbid?.ifBlank { null },
				artist = edition?.artist?.ifBlank { null },
				title = edition?.title?.ifBlank { null },
				totalTracks = edition?.totalTracks?.takeIf { it > 0 },
				quality = quality.ifBlank { null },
				sourceUsername = source?.peer?.ifBlank { null },
				sourceFolder = source?.folder?.ifBlank { null }
			)
		)
		return when (res) {
			is LbResult.Failed -> res
			is LbResult.Ok -> LbResult.Ok(
				LbDownloadResult(
					ok = res.value.ok,
					existing = res.value.existing,
					releaseMbid = res.value.resolved?.releaseMbid.orEmpty()
				)
			)
		}
	}

	/**
	 * Where one release's fill has got to, failure and all.
	 *
	 * The poll path must use this rather than [fillStatus]. Collapsing a failure into a
	 * default `LbFillStatus` makes it arrive as `state = "unknown"`, which is a real
	 * lb-bot answer meaning "nothing is filling this" — so a transient 502 (on this
	 * route almost always "lb-bot is busy holding its lock", not "lb-bot is gone") both
	 * blanked a downloading tile for five seconds and spent one of the eighteen
	 * `unknown` credits that exist to detect a fill which never started.
	 */
	suspend fun fillStatusResult(releaseMbid: String): LbResult<LbFillStatus> =
		getJson(
			"/lb/album/status",
			listOf("release_mbid" to releaseMbid),
			timeoutMs = POLL_TIMEOUT_MS
		)

	/** Where one release's fill has got to. For one-shot reads, where "no answer" and
	 *  "no fill" are equally uninteresting; polls want [fillStatusResult]. */
	suspend fun fillStatus(releaseMbid: String): LbFillStatus =
		fillStatusResult(releaseMbid).valueOrNull() ?: LbFillStatus()

	/**
	 * Widen the accepted formats for one album's searches. lb-bot's global policy stays
	 * flac/opus; this only adds mp3, ranked last, for this album. Offered only when a
	 * status or a gap reported `mp3WouldHelp`.
	 */
	suspend fun allowMp3(groupId: String, allow: Boolean = true): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		return postJson("/lb/album/allow-mp3", LbAllowMp3Request(groupId, allow))
	}

	// ----- gaps: albums the library already has, partly -------------------- //

	/**
	 * One partly-owned album's Fill-gaps record: which tracks are missing, which
	 * sources were found for them, and how the last attempt ended.
	 *
	 * The `group_id` this takes comes straight off an `incomplete` discography row.
	 * lb-bot's discography scan does not merely label such a release — it builds the
	 * review group at the same time — so the handle is live without any separate scan.
	 */
	suspend fun gapDetail(
		groupId: String,
		sourcePage: Int = 0,
		polling: Boolean = false
	): LbResult<LbGap> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		return getJson(
			"/lb/gap",
			listOf("group_id" to groupId, "sourcePage" to sourcePage.toString()),
			timeoutMs = if (polling) POLL_TIMEOUT_MS else null
		)
	}

	/**
	 * One gap source's real folder listing, each file tagged with the track it would
	 * fill.
	 *
	 * Fetched on demand rather than ridden along with the poll: the hub strips these
	 * from `/lb/gap` because that runs every five seconds, and upstream expands the
	 * peer's directory for real, which is slow. This is what answers "does this peer
	 * have my seventeen missing tracks, or twelve from a different pressing" — the
	 * question coverage counts alone were never going to settle.
	 */
	suspend fun gapSourceFiles(groupId: String, sourceIndex: Int): LbResult<LbSourceFiles> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		return getJson(
			"/lb/gap/source-files",
			listOf("group_id" to groupId, "source_index" to sourceIndex.toString())
		)
	}

	/**
	 * Search, rank and download the best source for one album's missing tracks without
	 * a manual pick.
	 *
	 * Upstream this walks the whole ranked list rather than trusting rank 1: search-time
	 * peer state goes stale within seconds, so an immediate rejection means "try the
	 * next one", not "give up". It answers with a task id, which is discarded for the
	 * same reason as in [indexArtist] — the gap detail carries a `sourceTask` field
	 * precisely so a client can watch a source search without touching the task API.
	 */
	/**
	 * Find sources for one album's missing tracks without committing to any.
	 *
	 * The review half of [gapAuto]. Splitting them is the point: a gap fill drops
	 * tracks *into* a record the user already owns, so a different pressing
	 * contaminates the album rather than merely disappointing — which is how
	 * seventeen missing tracks came back as twelve from another edition, with
	 * nothing on screen to say so beforehand.
	 *
	 * The search runs as a background task upstream; watch `sourceTask` on the gap
	 * detail rather than the task id it returns.
	 */
	suspend fun gapSearch(groupId: String, force: Boolean = false): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		val res = postJson<_, LbOk>("/lb/gap/search", LbSearchRequest(groupId, force))
		if (res is LbResult.Ok) startGapWatch(groupId)
		return res
	}

	suspend fun gapAuto(groupId: String): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		val res = postJson<_, LbOk>("/lb/gap/auto", LbGroupRequest(groupId))
		if (res is LbResult.Ok) startGapWatch(groupId)
		return res
	}

	/** Queue one hand-picked source (its `id` from the gap's `sources` list). */
	suspend fun gapFetch(groupId: String, sourceId: Int): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		val res = postJson<_, LbOk>("/lb/gap/fetch", LbFetchRequest(groupId, sourceId))
		if (res is LbResult.Ok) startGapWatch(groupId)
		return res
	}

	/** Cancel every in-flight transfer belonging to one album's gap fill. */
	suspend fun gapCancel(groupId: String): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		val res = postJson<_, LbOk>("/lb/gap/cancel", LbGroupRequest(groupId))
		if (res is LbResult.Ok) {
			settle(groupId, OUTCOME_CANCELLED, state = "cancelled")
			refreshGap(groupId)
		}
		return res
	}

	/**
	 * Re-check one album against Navidrome and its own folder, without a full library
	 * scan. This is the manual reconcile after a fill: it also picks up files put there
	 * by hand that Navidrome hasn't indexed yet.
	 */
	suspend fun gapRescan(groupId: String): LbResult<LbOk> {
		if (groupId.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
		val res = postJson<_, LbOk>("/lb/gap/rescan", LbGroupRequest(groupId))
		if (res is LbResult.Ok) {
			refreshGap(groupId)
			_libraryRevision.value += 1
		}
		return res
	}

	/**
	 * Read one gap and publish it, without starting a watch. Opening the sheet to look
	 * at an album is not a reason to begin polling it — only acting on it is.
	 */
	suspend fun refreshGap(groupId: String): LbResult<LbGap> {
		val result = gapDetail(groupId)
		if (result is LbResult.Ok) _gaps.value = _gaps.value + (groupId to result.value)
		return result
	}

	// ----- watches ---------------------------------------------------------- //

	/**
	 * Register a fill so it keeps being watched after the sheet closes.
	 *
	 * Deliberately here and not in the ViewModel: `ArtistDetailViewModel` is keyed per
	 * artist and disposed on navigate-away, and a fill takes minutes. The map is
	 * persisted for the same reason one step further out — the app is routinely killed
	 * inside that window.
	 */
	fun startAlbumFill(
		rgid: String,
		releaseMbid: String,
		quality: String,
		artist: String = "",
		album: String = "",
		source: LbGapSource? = null
	) {
		if (rgid.isBlank() || releaseMbid.isBlank()) return
		putWatch(
			LbWatch(
				kind = KIND_ALBUM,
				key = rgid,
				releaseMbid = releaseMbid,
				quality = quality,
				startedAt = nowMs(),
				// Carried purely so the downloads view and a retry need no second read.
				// The artist page has all of this at the moment of the tap; a ledger row
				// re-read hours later, possibly after lb-bot forgot the fill, has none.
				artist = artist,
				album = album,
				sourcePeer = source?.peer.orEmpty(),
				sourceFolder = source?.folder.orEmpty()
			)
		)
	}

	fun startGapWatch(groupId: String, artist: String = "", album: String = "") {
		if (groupId.isBlank()) return
		putWatch(
			LbWatch(
				kind = KIND_GAP,
				key = groupId,
				startedAt = nowMs(),
				artist = artist,
				album = album
			)
		)
	}

	/**
	 * Re-issue a fill the ledger remembers, with the options it was started with.
	 *
	 * Not automatic, and that is the point: lb-bot already walks its entire ranked source
	 * list before it reports a failure, so an unattended retry would re-run the identical
	 * search against the identical peers. The user asking again is new information — the
	 * swarm has moved on, or they have just allowed mp3.
	 *
	 * A gap retries as `search`, never `auto`: `auto` is what failed, and `search` stops
	 * after ranking so the candidates can be judged.
	 */
	suspend fun retry(key: String): LbResult<LbOk> {
		val watch = loadWatches()[key] ?: return LbResult.Failed(LbError.Rejected(404, ""))
		return when (watch.kind) {
			KIND_GAP -> gapSearch(watch.key, force = true)
			else -> {
				val res = download(
					rgid = watch.key,
					quality = watch.quality,
					source = watch.sourcePeer.takeIf { it.isNotBlank() }?.let {
						LbGapSource(peer = it, folder = watch.sourceFolder)
					}
				)
				when (res) {
					is LbResult.Failed -> LbResult.Failed(res.error)
					is LbResult.Ok -> {
						// Re-open the same row rather than adding a second one, so the
						// history stays one line per album rather than one per attempt.
						reopen(watch.key, res.value.releaseMbid)
						LbResult.Ok(LbOk(ok = true))
					}
				}
			}
		}
	}

	/** Forget one finished row. Only ever offered on a settled fill. */
	fun dismiss(key: String) {
		scope.launch {
			watchLock.withLock { saveWatches(loadWatches() - key) }
		}
	}

	/** Whether this release-group has a fill we're still watching. */
	fun isFilling(rgid: String): Boolean = loadWatches()[rgid]?.settled == false

	/**
	 * The hub says something landed in the library — possibly from another client.
	 * Only ever "re-read what you can already read", so it needs no validation beyond
	 * the socket's, and missing it costs nothing: the index flip upstream is durable,
	 * so the next read is right regardless.
	 */
	fun onLibraryChanged() {
		_libraryRevision.value += 1
	}

	private val watchLock = Mutex()
	private var pollJob: Job? = null

	/**
	 * Two retention rules, because a running row and a finished one are different things.
	 *
	 * A row still being polled is dropped at [WATCH_TIMEOUT_MS] — twenty minutes, the
	 * wall clock that exists because lb-bot's verifier gives up and leaves a fill on
	 * `placed` forever. A settled one is *history* and is kept for [LEDGER_RETAIN_MS],
	 * capped at [LEDGER_MAX] rows so a heavy week can't grow the preference without
	 * bound. Before this, settling and deleting were the same act.
	 */
	private fun loadWatches(): Map<String, LbWatch> = try {
		val now = nowMs()
		val all = json.decodeFromString<Map<String, LbWatch>>(preferenceManager.lbBotWatches)
			.filterValues { watch ->
				if (watch.settled) now - watch.finishedAtOrStart() < LEDGER_RETAIN_MS
				else now - watch.startedAt < WATCH_TIMEOUT_MS
			}
		if (all.size <= LEDGER_MAX) all
		else all.entries
			.sortedByDescending { it.value.finishedAtOrStart() }
			.take(LEDGER_MAX)
			.associate { it.key to it.value }
	} catch (e: Exception) {
		Logger.w("LbBotManager", "could not read persisted fills, starting empty", e)
		emptyMap()
	}

	/** The single write point, so publishing the ledger cannot be forgotten anywhere. */
	private fun saveWatches(watches: Map<String, LbWatch>) {
		preferenceManager.lbBotWatches = try {
			json.encodeToString(watches)
		} catch (e: Exception) {
			Logger.e("LbBotManager", "could not persist fills", e)
			"{}"
		}
		_ledger.value = watches.values
			.sortedWith(
				// In flight first — that is what someone opening this view came to see —
				// then most recent. `startedAt` for a live row, `finishedAt` for a dead
				// one, so a fill that ran for ten minutes doesn't sort above one that
				// failed since.
				compareBy<LbWatch> { it.settled }.thenByDescending { it.finishedAtOrStart() }
			)
			.map { it.toEntry() }
	}

	private fun putWatch(watch: LbWatch) {
		scope.launch {
			watchLock.withLock {
				val existing = loadWatches()[watch.key]
				// Keep whatever the previous attempt knew about the album; a retry or a
				// second tap must not blank the title the row is displayed under.
				saveWatches(
					loadWatches() + (watch.key to watch.copy(
						artist = watch.artist.ifBlank { existing?.artist.orEmpty() },
						album = watch.album.ifBlank { existing?.album.orEmpty() },
						quality = watch.quality.ifBlank { existing?.quality.orEmpty() }
					))
				)
			}
			ensurePolling()
		}
	}

	/** Re-open a settled row for another attempt, keeping its display fields. */
	private suspend fun reopen(key: String, releaseMbid: String) {
		watchLock.withLock {
			val watches = loadWatches()
			val watch = watches[key] ?: return@withLock
			saveWatches(
				watches + (key to watch.copy(
					settled = false,
					outcome = OUTCOME_RUNNING,
					reason = "",
					state = "",
					unknownPolls = 0,
					quietTicks = 0,
					fingerprint = "",
					nextPollAt = 0L,
					finishedAt = 0L,
					startedAt = nowMs(),
					releaseMbid = releaseMbid.ifBlank { watch.releaseMbid }
				))
			)
		}
		ensurePolling()
	}

	/**
	 * Close a watch and record how it ended.
	 *
	 * The only place [fillEvents] is emitted, so however many screens are watching a
	 * fill it is announced exactly once.
	 */
	private suspend fun settle(
		key: String,
		outcome: String = OUTCOME_GAVE_UP,
		state: String = "",
		reason: String = ""
	) {
		val settledWatch = watchLock.withLock {
			val watches = loadWatches()
			val watch = watches[key] ?: return@withLock null
			if (watch.settled) return@withLock null
			val next = watch.copy(
				settled = true,
				finishedAt = nowMs(),
				outcome = outcome,
				state = state.ifBlank { watch.state },
				reason = reason.ifBlank { watch.reason }
			)
			saveWatches(watches + (key to next))
			next
		} ?: return
		_fillEvents.emit(
			LbFillEvent(
				key = settledWatch.key,
				artist = settledWatch.artist,
				album = settledWatch.album,
				outcome = settledWatch.outcome,
				reason = settledWatch.reason
			)
		)
	}

	private fun ensurePolling() {
		if (pollJob?.isActive == true) return
		pollJob = scope.launch { pollLoop() }
	}

	/**
	 * The watch loop. Five seconds, and no tighter: lb-bot is a single Python process
	 * behind a process-wide lock, with its own 2s-polling web UI already on it, and the
	 * hub deliberately does not cache the two progress routes. A faster poll here buys
	 * nothing but load. The loop only exists while something is live.
	 */
	private suspend fun pollLoop() {
		while (true) {
			val startedTick = nowMs()
			val live = watchLock.withLock { loadWatches().values.filter { !it.settled } }
			if (live.isEmpty()) return

			// Time out the stale ones first, without spending a slot on them.
			val expired = live.filter { startedTick - it.startedAt > WATCH_TIMEOUT_MS }
			expired.forEach { settle(it.key, OUTCOME_GAVE_UP) }

			// Each watch carries its own next slot (see `intervalFor`), so a fill that
			// has looked identical for a minute steps back to a slower cadence while one
			// that is actually moving keeps the full five seconds. Oldest slot first, so
			// capping the fan-out delays a watch by one tick rather than starving it.
			val due = (live - expired.toSet())
				.filter { startedTick >= it.nextPollAt }
				.sortedBy { it.nextPollAt }
				.take(MAX_POLLS_PER_TICK)

			// Concurrent, not sequential: the old loop delayed *after* walking every
			// watch in turn, so three fills meant the interval was five seconds plus
			// three round trips, and each tick hit lb-bot's process-wide lock in a burst.
			// Bounded by the same take() above, at the hub's own in-flight limit.
			coroutineScope {
				due.map { watch ->
					async {
						when (watch.kind) {
							KIND_ALBUM -> pollAlbum(watch)
							KIND_GAP -> pollGap(watch)
						}
					}
				}.awaitAll()
			}

			// Subtract the work from the wait, so the cadence is the interval rather
			// than the interval plus however long lb-bot took.
			val elapsed = nowMs() - startedTick
			delay((POLL_INTERVAL_MS - elapsed).coerceIn(MIN_POLL_GAP_MS, POLL_INTERVAL_MS))
		}
	}

	/**
	 * How long before this watch is polled again.
	 *
	 * Five seconds is the floor and stays the answer for anything that is moving. But
	 * lb-bot only refreshes slskd's transfer state every sixty seconds and
	 * `album/status` just reads the counters that loop last wrote, so a long download
	 * spends eleven ticks in twelve returning a byte-identical body — each one taking
	 * the process-wide review lock that lb-bot's own 2s-polling web UI is already on.
	 * An unchanged payload is free information: back off, and snap straight back the
	 * moment anything at all differs.
	 */
	private fun intervalFor(quietTicks: Int): Long = when {
		quietTicks < QUIET_TICKS_FIRST -> POLL_INTERVAL_MS
		quietTicks < QUIET_TICKS_SECOND -> POLL_INTERVAL_MS * 2
		else -> POLL_INTERVAL_MS * 4
	}

	/**
	 * Record a tick's outcome: the fingerprint that drives the backoff, the display
	 * fields the ledger shows, and the consecutive-unknown count.
	 *
	 * [unknown] is null when the answer was neither progress nor an ambiguous idle —
	 * i.e. a genuine state — which resets the give-up counter. That reset is the fix
	 * for a fill that answered `unknown` intermittently over twenty minutes and was
	 * given up on while it was still alive: the eighteen only ever meant *consecutive*.
	 */
	private suspend fun recordTick(
		key: String,
		fingerprint: String,
		unknown: Boolean,
		state: String,
		reason: String = "",
		percent: Int = 0,
		done: Int = 0,
		total: Int = 0,
		artist: String = "",
		album: String = "",
		mp3WouldHelp: Boolean = false,
		groupId: String = ""
	): Int {
		return watchLock.withLock {
			val watches = loadWatches()
			val current = watches[key] ?: return@withLock UNKNOWN_POLL_LIMIT
			val quiet = if (fingerprint == current.fingerprint) current.quietTicks + 1 else 0
			val next = current.copy(
				fingerprint = fingerprint,
				quietTicks = quiet,
				nextPollAt = nowMs() + intervalFor(quiet),
				unknownPolls = if (unknown) current.unknownPolls + 1 else 0,
				state = state,
				reason = reason.ifBlank { current.reason },
				percent = percent,
				done = done,
				total = total,
				artist = artist.ifBlank { current.artist },
				album = album.ifBlank { current.album },
				mp3WouldHelp = mp3WouldHelp || current.mp3WouldHelp,
				groupId = groupId.ifBlank { current.groupId }
			)
			saveWatches(watches + (key to next))
			next.unknownPolls
		}
	}

	/** A failed tick is not an answer: leave everything as it was and try again later. */
	private suspend fun deferWatch(key: String) {
		watchLock.withLock {
			val watches = loadWatches()
			val current = watches[key] ?: return@withLock
			saveWatches(watches + (key to current.copy(nextPollAt = nowMs() + POLL_INTERVAL_MS)))
		}
	}

	private suspend fun pollAlbum(watch: LbWatch) {
		// The result form, not `fillStatus`: a failure there arrives as the default
		// `state = "unknown"`, which is indistinguishable from lb-bot saying "nothing is
		// filling this". A 502 on this route usually means lb-bot is *busy* holding its
		// lock rather than gone, and treating that as an answer both blanked a
		// downloading tile for five seconds and burned one of the eighteen credits.
		val status = when (val result = fillStatusResult(watch.releaseMbid)) {
			is LbResult.Ok -> result.value
			is LbResult.Failed -> {
				deferWatch(watch.key)
				return
			}
		}
		_fills.value = _fills.value + (watch.key to status)

		// `placed` says the files are in the library folder. Navidrome may not have
		// indexed them yet — that is what `verified` is for — but refreshing now is
		// what makes the album appear the moment its scan finishes, rather than a
		// minute later.
		if (status.state == "placed" || status.state == "verified") _libraryRevision.value += 1

		val seen = recordTick(
			key = watch.key,
			fingerprint = "${status.state}|${status.done}/${status.total}|${status.failed}",
			// `unknown` is ambiguous: both "nothing is filling this" and "the POST
			// returned but lb-bot's worker hasn't written its first ledger row", which is
			// the normal first second or two. Treating it as terminal stopped the poll
			// before the fill began; treating it as live forever polls a release nobody
			// is filling.
			unknown = status.state == "unknown",
			state = status.state,
			reason = status.reason,
			percent = status.percent,
			done = status.done,
			total = status.total,
			artist = status.artist,
			album = status.album,
			mp3WouldHelp = status.mp3WouldHelp,
			groupId = status.groupId
		)
		if (status.state == "unknown") {
			if (seen >= UNKNOWN_POLL_LIMIT) settle(watch.key, OUTCOME_GAVE_UP, status.state)
			return
		}
		// A backwards step is normal, not an error: lb-bot reports `downloading` for as
		// long as a transfer group is pending, which can follow `placing`.
		if (status.state in TERMINAL_FILL_STATES) {
			settle(
				watch.key,
				outcome = if (status.state == "verified") OUTCOME_DONE else OUTCOME_FAILED,
				state = status.state,
				reason = status.reason
			)
		}
	}

	/**
	 * Why a gap fill ended, in lb-bot's own words, most specific first.
	 *
	 * `noSourceReason` is the attributable one ("103 peers offered 2,047 files, but none
	 * in FLAC…") and is what makes the Allow-MP3 offer make sense; `failReason` names the
	 * failure; the task error is the fallback for a search that itself broke.
	 */
	private fun gapReason(gap: LbGap): String = when {
		gap.failReason.isNotBlank() -> gap.failReason
		gap.noSourceReason.isNotBlank() -> gap.noSourceReason
		else -> gap.sourceTask?.error.orEmpty()
	}

	private suspend fun pollGap(watch: LbWatch) {
		val gap = when (val result = gapDetail(watch.key, polling = true)) {
			is LbResult.Ok -> result.value
			is LbResult.Failed -> {
				deferWatch(watch.key)
				return
			}
		}
		_gaps.value = _gaps.value + (watch.key to gap)

		if (gap.status == "complete") _libraryRevision.value += 1

		val filling = gap.tracks.filter { it.state != "present" }
		val filled = filling.count { it.state == "done" || it.state == "downloaded" }
		val searching = gap.sourceTask?.status.orEmpty() in SEARCH_IN_FLIGHT
		// `ready` after an auto run means the search found nothing, or every source
		// rejected the enqueue. Give it the same bounded patience `unknown` gets on the
		// album path, but don't count polls where a source search is visibly running.
		val idle = gap.status == "ready" && gap.sourceTask?.status != "running"
		val seen = recordTick(
			key = watch.key,
			fingerprint = "${gap.status}|${gap.sourceTask?.status}|$filled/${filling.size}",
			unknown = idle,
			state = gap.status,
			reason = gapReason(gap),
			percent = if (filling.isEmpty()) 0 else filled * 100 / filling.size,
			done = filled,
			total = filling.size,
			artist = gap.artist,
			album = gap.album,
			mp3WouldHelp = gap.mp3WouldHelp
		)

		// A source search in flight is never a reason to stop. Asking for one flips
		// the group to `picking` *immediately* — the POST approves the pending tracks
		// before the search has found anything — so treating `picking` as terminal
		// settled the watch on the first poll and the results, arriving 30s later,
		// were never read. The sheet sat on "asking slskd" until a second press.
		if (searching) return

		if (idle) {
			if (seen >= UNKNOWN_POLL_LIMIT) settle(watch.key, OUTCOME_GAVE_UP, gap.status)
			return
		}
		// `picking` is not a failure, and — since the picker exists here — usually not
		// even a hand-off: with the search finished it means "the candidates are on
		// screen, waiting for you". Nothing left to poll either way.
		if (gap.status in TERMINAL_GAP_STATES) {
			settle(
				watch.key,
				outcome = when (gap.status) {
					"complete" -> OUTCOME_DONE
					"failed" -> OUTCOME_FAILED
					else -> OUTCOME_NEEDS_PICK
				},
				state = gap.status,
				reason = gap.sourceTask?.error.orEmpty()
			)
		}
	}

	/**
	 * Resume any fill that outlived the process, and publish the ledger. Called once, at
	 * startup — the downloads view must show yesterday's history without a fill running.
	 */
	fun resumeWatches() {
		scope.launch {
			val live = watchLock.withLock {
				val watches = loadWatches()
				// Re-save rather than just reading: this is what applies the retention
				// rules and, being the single write point, publishes the ledger.
				saveWatches(watches)
				watches.values.count { !it.settled }
			}
			if (live > 0) ensurePolling()
		}
	}

	private fun nowMs(): Long = Clock.System.now().toEpochMilliseconds()

	companion object {
		/** Which copy to prefer. Mirrors lb-bot's QUALITY_PREFERENCES; an unknown value
		 *  is rejected upstream with a 400, so drift fails loudly. These rank sources,
		 *  they do not filter them — word them "prefer", never "only". */
		val QUALITY_OPTIONS = listOf(
			"" to "lb-bot's default",
			"flac-any" to "Best FLAC available",
			"flac-16-44" to "CD quality (16-bit/44.1kHz)",
			"highest-bitrate" to "Highest bitrate",
			"prefer-opus" to "Prefer Opus"
		)

		/** Cover Art Archive front cover for a release-group.
		 *
		 *  Built here rather than proxied: lb-bot's own cover route serves *Navidrome*
		 *  art keyed by a Navidrome album id, which by definition does not exist for a
		 *  release the library doesn't have. The Archive is public, so the client
		 *  fetches it directly — which also keeps multi-megabyte images out of the
		 *  hub's entry-counted cache. Must be loaded WITHOUT the user's Navidrome
		 *  custom headers; see RemoteCoverArt. */
		fun caaCoverUrl(rgid: String, size: Int = 250): String =
			if (rgid.isBlank()) "" else
				"https://coverartarchive.org/release-group/$rgid/front-$size"

		private const val KIND_ALBUM = "album"
		private const val KIND_GAP = "gap"
		private const val POLL_INTERVAL_MS = 5_000L

		/** Never let the loop spin: a tick that overran its own interval still waits. */
		private const val MIN_POLL_GAP_MS = 1_000L

		/** Requests in flight per tick, matching the hub's own PROXY_MAX_INFLIGHT ceiling
		 *  minus one, so a burst of watches can't crowd out an interactive call. */
		private const val MAX_POLLS_PER_TICK = 3

		/** How long the two poll routes get before the client gives up on the tick —
		 *  well under the client's general 60 s, which is sized for `gap/search`. */
		private const val POLL_TIMEOUT_MS = 12_000L

		/** Identical answers before the interval doubles, then doubles again. */
		private const val QUIET_TICKS_FIRST = 4
		private const val QUIET_TICKS_SECOND = 10

		/** lb-bot's verifier gives up after ten minutes and leaves a fill on `placed`
		 *  forever, so the client needs a wall clock of its own or it polls with no end. */
		private const val WATCH_TIMEOUT_MS = 20 * 60 * 1000L
		private const val UNKNOWN_POLL_LIMIT = 18

		/** How long a *finished* row stays readable, and how many are kept at all.
		 *  Running rows are still bounded by WATCH_TIMEOUT_MS — different question. */
		private const val LEDGER_RETAIN_MS = 7 * 24 * 60 * 60 * 1000L
		private const val LEDGER_MAX = 50

		const val OUTCOME_RUNNING = "running"
		const val OUTCOME_DONE = "done"
		const val OUTCOME_FAILED = "failed"
		/** The gap picker has candidates and wants a choice — not a failure. */
		const val OUTCOME_NEEDS_PICK = "needs_pick"
		const val OUTCOME_CANCELLED = "cancelled"
		/** Nothing ever acted on it: lb-bot never wrote a ledger row, or forgot it. */
		const val OUTCOME_GAVE_UP = "gave_up"

		/** `placed` is deliberately absent: the interesting transition is
		 *  placed -> verified, which is Navidrome confirming the files really landed. */
		private val TERMINAL_FILL_STATES = setOf("failed", "needs_match", "verified")
		private val TERMINAL_GAP_STATES = setOf("complete", "failed", "picking")

		/** A source search that hasn't answered yet. Every gap state is provisional
		 *  while one of these is true, because asking for sources changes the group's
		 *  status before it changes its contents. */
		val SEARCH_IN_FLIGHT = setOf("queued", "running")
	}
}

// ---------------------------------------------------------------------------
// Wire shapes. lb-bot speaks snake_case for the index rows and camelCase for the
// screen-shaped views; both are mirrored verbatim rather than normalized, so a
// field here can be checked against its Python source by name.
// ---------------------------------------------------------------------------

@Serializable
data class LbStatusProbe(
	val configured: Boolean = false,
	val upstreamReachable: Boolean = false,
	/** Routes this hub can proxy. Empty from a hub too old to advertise them. */
	val routes: List<String> = emptyList()
)

@Serializable
data class LbOk(
	val ok: Boolean = false,
	/** Upstream's own sentence when it refuses with a 200. Shown verbatim. */
	val error: String = "",
	/** lb-bot already had this in flight; the second request was a no-op. */
	val alreadyActive: Boolean = false
)

/** Ranked Soulseek folders for one release-group, with the album they were sought for. */
@Serializable
data class LbAlbumSources(
	val artist: String = "",
	val album: String = "",
	val query: String = "",
	val sources: List<LbGapSource> = emptyList()
)

@Serializable
private data class LbIndexRequest(
	val mbid: String,
	val name: String,
	@SerialName("nd_id") val ndId: String
)

@Serializable
private data class LbGroupRequest(@SerialName("group_id") val groupId: String)

@Serializable
private data class LbSearchRequest(
	@SerialName("group_id") val groupId: String,
	/** Re-search rather than reusing results inside lb-bot's own TTL. */
	val force: Boolean = false
)

@Serializable
private data class LbFetchRequest(
	@SerialName("group_id") val groupId: String,
	val sourceId: Int
)

@Serializable
private data class LbAllowMp3Request(
	@SerialName("group_id") val groupId: String,
	val allow: Boolean
)

@Serializable
private data class LbDownloadRequest(
	val rgid: String,
	/**
	 * The pressing the user picked.
	 *
	 * lb-bot honours this over re-resolving the release-group, which fixes two things at once. The
	 * edition picker used to be decorative — `mbz_resolve_album` chose "official, earliest" on its
	 * own and nobody was told. And a resolve that hits a MusicBrainz 503 parks the whole
	 * release-group in a five-minute failure cooldown that answers every retry instantly with the
	 * same 400, so one hiccup took the album's download out for minutes with no way forward.
	 */
	@SerialName("release_mbid") val releaseMbid: String? = null,
	val artist: String? = null,
	val title: String? = null,
	@SerialName("total_tracks") val totalTracks: Int? = null,
	val quality: String? = null,
	/** A hand-picked source. lb-bot floats this peer to the front of its own ranked
	 *  list and keeps the rest as failover, so it is a strong preference rather than
	 *  a guarantee — if the peer has gone by transfer time, the best ranked folder
	 *  wins instead. */
	val sourceUsername: String? = null,
	val sourceFolder: String? = null
)

@Serializable
private data class LbDownloadResponse(
	val ok: Boolean = false,
	val existing: Boolean = false,
	val resolved: LbResolved? = null
)

@Serializable
private data class LbResolved(@SerialName("release_mbid") val releaseMbid: String = "")

data class LbDownloadResult(
	val ok: Boolean = false,
	val existing: Boolean = false,
	val releaseMbid: String = ""
)

@Serializable
data class LbDiscography(
	val indexed: Boolean = false,
	@SerialName("artist_name") val artistName: String = "",
	@SerialName("artist_mbid") val artistMbid: String = "",
	/** Index older than lb-bot's TTL, or built by an older scan version. */
	val stale: Boolean = false,
	/**
	 * When the walk last finished, epoch **seconds**. The only reliable "the rescan is done"
	 * signal — `indexed` is already true for every artist a rescan applies to.
	 *
	 * Double, not Long: lb-bot writes a `time.time()` float, and a Long here would throw on the
	 * decimal point and take the entire discography payload down with it.
	 */
	@SerialName("scanned_at") val scannedAt: Double = 0.0,
	val releases: List<LbRelease> = emptyList()
)

/**
 * One MusicBrainz release-group as lb-bot's index holds it.
 *
 * Almost everything past `status` is conditional upstream: `group_id`, `present`
 * and `total` are written only for an `incomplete` row, and `navidrome_album_ids`
 * only when the list is non-empty. Nothing here may be non-null.
 */
@Serializable
data class LbRelease(
	val rgid: String = "",
	val title: String = "",
	val year: String = "",
	@SerialName("primary_type") val primaryType: String = "",
	@SerialName("secondary_types") val secondaryTypes: List<String> = emptyList(),
	@SerialName("effective_type") val effectiveType: String = "",
	/** complete | incomplete | missing | untagged */
	val status: String = "missing",
	@SerialName("match_method") val matchMethod: String = "",
	@SerialName("match_score") val matchScore: Float = 0f,
	/** Present only when [status] is `incomplete` — the Fill-gaps handle. */
	@SerialName("group_id") val groupId: String? = null,
	val present: Int? = null,
	val total: Int? = null,
	@SerialName("navidrome_album_ids") val navidromeAlbumIds: List<String> = emptyList()
) {
	val isMissing: Boolean get() = status == "missing"
	val isIncomplete: Boolean get() = status == "incomplete" && !groupId.isNullOrBlank()
}

/**
 * The exact pressing a sheet has already resolved, passed to lb-bot instead of letting it guess.
 *
 * Not serialized: [LbBotManager.download] and [LbBotManager.albumSources] send these as flat body
 * keys and query params respectively, which is the shape lb-bot's override expects.
 */
data class LbResolvedEdition(
	val releaseMbid: String,
	val artist: String,
	val title: String,
	/** Prefer the loaded tracklist's size for this exact edition; fall back to the variant's. */
	val totalTracks: Int
)

@Serializable
data class LbReleaseDetail(
	val artist: String = "",
	val title: String = "",
	val coverUrl: String = "",
	val releases: List<LbVariant> = emptyList()
)

/** A variant changes the tracklist (Original / Remaster / Deluxe). */
@Serializable
data class LbVariant(
	val releaseMbid: String = "",
	val title: String = "",
	val disambiguation: String = "",
	val year: String = "",
	val trackCount: Int = 0,
	val coverUrl: String = "",
	/** Same tracklist, different pressing — each carries its own cover art. */
	val editions: List<LbEdition> = emptyList()
)

@Serializable
data class LbEdition(
	val releaseMbid: String = "",
	/** Digital | CD | Vinyl | Cassette | Other */
	val label: String = "",
	val format: String = "",
	val year: String = "",
	val coverUrl: String = ""
)

@Serializable
data class LbTracklist(
	/** False when the library holds none of this album. Every track is missing; that
	 *  is the normal case here, and it must not render as `0/12` or as an error. */
	val presenceKnown: Boolean = false,
	val tracks: List<LbTrack> = emptyList()
)

@Serializable
data class LbTrack(
	val position: Int = 0,
	val title: String = "",
	val present: Boolean = false
)

/**
 * unknown -> searching -> queued -> downloading -> placing -> placed -> verified,
 * with needs_match and failed as the two side exits.
 *
 * `unknown` is the resting state of every album nobody has asked for, so it must
 * never render as an error. Backwards steps are normal: lb-bot reports
 * `downloading` for as long as a transfer group is pending.
 */
@Serializable
data class LbFillStatus(
	val releaseMbid: String = "",
	val rgid: String = "",
	val state: String = "unknown",
	val artist: String = "",
	val album: String = "",
	val quality: String = "",
	val done: Int = 0,
	val total: Int = 0,
	val failed: Int = 0,
	val percent: Int = 0,
	/** lb-bot's own sentence for a failure. Shown verbatim. */
	val reason: String = "",
	/** The search rejected mp3s and would have found something with them. */
	val mp3WouldHelp: Boolean = false,
	/** lb-bot review group, when the album has one — required for Allow MP3. */
	val groupId: String = ""
)

/** One partly-owned album's Fill-gaps record. `status`: ready | picking | downloading | complete | failed. */
@Serializable
data class LbGap(
	val id: String = "",
	val albumId: String = "",
	val artist: String = "",
	val album: String = "",
	val present: Int = 0,
	val total: Int = 0,
	/** Files the tracklist can't account for — a fill that left a duplicate behind. */
	val extra: Int = 0,
	val missingCount: Int = 0,
	val status: String = "ready",
	val tracks: List<LbGapTrack> = emptyList(),
	val sources: List<LbGapSource> = emptyList(),
	val sourcesTotal: Int = 0,
	val sourcesPage: Int = 0,
	val sourcesPages: Int = 1,
	val sourcesFoundAt: Double = 0.0,
	/** The background source search, so the sheet can show it running and report how
	 *  it ended. This is what a client watches instead of the task API. */
	val sourceTask: LbGapTask? = null,
	val failReason: String = "",
	val failDetail: String = "",
	val allowMp3: Boolean = false,
	val noSourceReason: String = "",
	val mp3WouldHelp: Boolean = false,
	/** The MusicBrainz release the gap is measured against — it comes from the
	 *  canonical album's own tag, so a library tagged as a 17-track deluxe reports
	 *  17 slots even when every pressing on offer has 12. Naming it is what makes
	 *  that number explicable instead of looking like a miscount. */
	val canonicalMbid: String = ""
) {
	/** Progress across the tracks being filled, for a `downloading` gap. */
	val tracksDone: Int get() = tracks.count { it.state in GAP_TRACK_DONE_STATES }
	val tracksWanted: Int get() = tracks.count { it.state != "present" }
	val tracksFailed: Int get() = tracks.count { it.state == "failed" }
}

/** Track states that count as "no longer waiting on a transfer". */
private val GAP_TRACK_DONE_STATES = setOf("downloaded", "done", "skipped")

@Serializable
data class LbGapTask(
	val id: String = "",
	val status: String = "",
	val label: String = "",
	val current: String = "",
	val summary: String = "",
	val error: String = ""
)

/** `state`: present | missing | picked | queued | downloading | downloaded | failed | skipped | done. */
@Serializable
data class LbGapTrack(
	val position: Int = 0,
	val title: String = "",
	val artist: String = "",
	val state: String = "missing",
	val downloadError: String = ""
)

@Serializable
data class LbGapSource(
	val id: Int = 0,
	val peer: String = "",
	val folder: String = "",
	val format: String = "",
	val bitrate: String = "",
	val size: String = "",
	val fileCount: Int = 0,
	val speedMbps: Double = 0.0,
	val queueLength: Int = 0,
	val freeSlot: Boolean = false,
	/** A label ("9/12 tracks" from the album picker, "full"/"partial" from a gap),
	 *  plus the counts behind it. Matched against the canonical MusicBrainz
	 *  tracklist — NOT a file count, which is what used to let a folder holding a
	 *  different album entirely report as a complete match. */
	val coverage: String = "unknown",
	val coverageFull: Boolean = false,
	val coverageDetail: LbGapCoverage = LbGapCoverage(),
	val flags: List<String> = emptyList(),
	val recommendation: String = "",
	/** How much the folder's own name reads as this album. For an ambiguous query the
	 *  peer's whole discography comes back, and peer speed is no way to tell them apart. */
	val albumMatch: Double = 0.0,
	val albumMatchOk: Boolean = false,
	val score: Double = 0.0,
	val rank: Int = 0,
	val recommended: Boolean = false,
	/** Present on the album picker (that route is not stripped); always empty on a
	 *  gap poll, where the listing is fetched separately via [LbBotManager.gapSourceFiles]. */
	val files: List<LbSourceFile> = emptyList(),
	val filesTruncated: Boolean = false
)

/**
 * One file in a peer's folder, and the tracklist slot it would fill.
 *
 * `matchedTo` being null is the interesting case: a folder whose files match no
 * slot at all is visibly the wrong album, where a file count alone would have
 * read as a full match.
 */
@Serializable
data class LbSourceFile(
	val filename: String = "",
	val ext: String = "",
	/** False when the format is outside lb-bot's accepted list (e.g. mp3 when off). */
	val accepted: Boolean = true,
	val sizeMb: Double = 0.0,
	val bitrate: Int = 0,
	val durationSec: Int = 0,
	val matchedTo: LbSourceMatch? = null
)

@Serializable
data class LbSourceMatch(
	val position: String = "",
	val title: String = "",
	/** How it was paired: recording mbid, title, duration… */
	val basis: String = ""
)

@Serializable
data class LbSourceFiles(
	val ok: Boolean = false,
	/** False when the peer is offline or the expand failed — the list is then the
	 *  original search hits, not the real folder, and the UI must not imply otherwise. */
	val expanded: Boolean = false,
	val files: List<LbSourceFile> = emptyList(),
	val filesTruncated: Boolean = false,
	val fileCount: Int = 0,
	val coverage: String = "",
	val coverageDetail: LbGapCoverage = LbGapCoverage()
)

@Serializable
data class LbGapCoverage(
	val haveTracks: Int = 0,
	val totalTracks: Int = 0,
	val unmatched: List<String> = emptyList()
)

/**
 * One fill, persisted so it survives leaving the screen and being killed — and, once
 * settled, so it survives *ending*. This is the stored form of a ledger row.
 *
 * Every field added since the first version has a default, which is what lets an older
 * stored map decode straight into the newer shape: a fill written before the ledger
 * existed reads back as a running row with no display fields, which the poll fills in on
 * its next tick. A decode failure is caught and degrades to "no history", never a crash.
 */
@Serializable
private data class LbWatch(
	val kind: String,
	val key: String,
	val releaseMbid: String = "",
	val quality: String = "",
	val startedAt: Long = 0L,
	val settled: Boolean = false,
	/** *Consecutive* ambiguous-idle answers. Reset by any real state; see `recordTick`. */
	val unknownPolls: Int = 0,

	// ----- ledger: enough to render and re-issue the fill with no second read ----- //
	val artist: String = "",
	val album: String = "",
	val sourcePeer: String = "",
	val sourceFolder: String = "",
	val finishedAt: Long = 0L,
	/** The last state lb-bot reported, kept verbatim so the row can explain itself. */
	val state: String = "",
	val outcome: String = "running",
	val reason: String = "",
	val percent: Int = 0,
	val done: Int = 0,
	val total: Int = 0,
	val mp3WouldHelp: Boolean = false,
	/** lb-bot's review group. A gap fill *is* one, so this only carries an album's —
	 *  which `album/status` reports, and which Allow MP3 needs to address the album. */
	val groupId: String = "",

	// ----- adaptive polling ------------------------------------------------------ //
	/** Digest of the last answer. Identical twice running means nothing is moving. */
	val fingerprint: String = "",
	val quietTicks: Int = 0,
	val nextPollAt: Long = 0L
) {
	/** When this row last did anything — its sort key, live or dead. */
	fun finishedAtOrStart(): Long = if (finishedAt > 0L) finishedAt else startedAt

	fun toEntry(): LbFillEntry = LbFillEntry(
		key = key,
		isGap = kind == "gap",
		rgid = if (kind == "gap") "" else key,
		groupId = if (kind == "gap") key else groupId,
		releaseMbid = releaseMbid,
		artist = artist,
		album = album,
		quality = quality,
		state = state,
		outcome = outcome,
		reason = reason,
		percent = percent,
		done = done,
		total = total,
		mp3WouldHelp = mp3WouldHelp,
		settled = settled,
		startedAt = startedAt,
		finishedAt = finishedAt
	)
}

/**
 * One row of the downloads view: a fill this device asked for, in flight or finished.
 *
 * Deliberately flat and self-contained — the view that renders it may be opened days
 * after the fill, on a screen that has no artist page behind it, and possibly after
 * lb-bot has forgotten the fill entirely (its own ledger is in memory and capped).
 */
data class LbFillEntry(
	val key: String,
	/** Gap fills (an album the library holds *partly*) take a different retry path. */
	val isGap: Boolean,
	val rgid: String,
	val groupId: String,
	val releaseMbid: String,
	val artist: String,
	val album: String,
	val quality: String,
	val state: String,
	val outcome: String,
	/** lb-bot's own sentence for a failure. Shown verbatim — it names the cause. */
	val reason: String,
	val percent: Int,
	val done: Int,
	val total: Int,
	/** The search rejected mp3s and would have found something with them. */
	val mp3WouldHelp: Boolean,
	val settled: Boolean,
	val startedAt: Long,
	val finishedAt: Long
) {
	val isRunning: Boolean get() = !settled
	val succeeded: Boolean get() = outcome == "done"
	/** Cover art for a release the library by definition does not have. */
	val coverUrl: String get() = LbBotManager.caaCoverUrl(rgid)
}

/** A fill reaching a terminal outcome. Emitted once, from `settle`. */
data class LbFillEvent(
	val key: String,
	val artist: String,
	val album: String,
	val outcome: String,
	val reason: String
)
