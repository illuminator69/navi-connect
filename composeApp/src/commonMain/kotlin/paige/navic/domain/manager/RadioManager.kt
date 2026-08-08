package paige.navic.domain.manager

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import paige.navic.data.database.dao.SongDao
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.SavedQueueSource
import paige.navic.domain.models.settings.AutoplayMode
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger

/**
 * Similarity radio ("instant mix") via OpenSubsonic `getSimilarSongs2`. Works
 * against vanilla Navidrome; when the AudioMuse-AI plugin is installed the
 * same endpoint serves sonic similarity — making this Navic's AudioMuse
 * integration. The seed id may be a song, album, or artist.
 *
 * [isAudioMuseMix] is true while the player queue is exactly the last
 * generated mix — the UI uses it to show the AudioMuse indicator (dynamic
 * colour on the mini player / queue).
 */
class RadioManager(
	private val sessionManager: SessionManager,
	private val songDao: SongDao,
	private val mediaPlayer: MediaPlayerViewModel,
	private val preferenceManager: PreferenceManager,
	private val hubManager: HubManager,
	private val audioMuseManager: AudioMuseManager
) {
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

	private val mixSig = MutableStateFlow<String?>(null)

	// True when the server advertises the OpenSubsonic `sonicSimilarity`
	// extension (i.e. the AudioMuse plugin is loaded) — gates Song Journey and
	// makes radio/autoplay prefer guaranteed-AudioMuse results. Re-probed on login.
	private val _sonicSimilarityAvailable = MutableStateFlow(false)
	val sonicSimilarityAvailable: StateFlow<Boolean> = _sonicSimilarityAvailable.asStateFlow()

	// Observable autoplay mode so the UI (e.g. the extended player's adaptive mood
	// background) can react live. Mirrors preferenceManager.autoplayMode; write via
	// [setAutoplayMode] to keep both in sync.
	private val _autoplayMode = MutableStateFlow(preferenceManager.autoplayMode)
	val autoplayMode: StateFlow<AutoplayMode> = _autoplayMode.asStateFlow()

	fun setAutoplayMode(mode: AutoplayMode) {
		preferenceManager.autoplayMode = mode
		_autoplayMode.value = mode
	}

	// Reference-cached: uiState emits ~5x/sec during playback with the same
	// queue list instance; only recompute the signature when it changes.
	private var sigCacheQueue: List<DomainSong>? = null
	private var sigCacheValue = ""

	private fun queueSig(queue: List<DomainSong>): String {
		if (queue !== sigCacheQueue) {
			sigCacheQueue = queue
			sigCacheValue = queue.joinToString(",") { it.id }
		}
		return sigCacheValue
	}

	val isAudioMuseMix: StateFlow<Boolean> = combine(
		mixSig, mediaPlayer.uiState
	) { sig, state ->
		sig != null && state.queue.isNotEmpty() && queueSig(state.queue) == sig
	}.stateIn(scope, SharingStarted.WhileSubscribed(5_000), false)

	/**
	 * Build and play a similarity mix seeded by [seedId] (song/album/artist).
	 * [seedSong] (when the seed is a song) is played first.
	 */
	fun startRadio(seedId: String, seedSong: DomainSong? = null, count: Int = 50) {
		scope.launch {
			try {
				val ids = fetchSimilar(seedId, count)
				val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
				val similar = ids.mapNotNull { byId[it]?.toDomainModel() }
					.filter { it.id != seedSong?.id }
				// Offline / no-match fallback: build a local mix so radio still works when
				// AudioMuse (or Navidrome's similar-songs agents) is offline or returns nothing.
				val picks = similar.ifEmpty {
					localRadioSongs(seedSong, count, setOfNotNull(seedSong?.id))
				}
				val queue = (listOfNotNull(seedSong) + picks).distinctBy { it.id }
				if (queue.isEmpty()) {
					Logger.i("RadioManager", "no songs (incl. local) for $seedId")
					return@launch
				}
				playMix(queue, SavedQueueSource.RADIO, seedSong?.title?.let { "$it radio" })
			} catch (e: Exception) {
				Logger.e("RadioManager", "failed to start radio for $seedId", e)
			}
		}
	}

	/**
	 * Build and play a sonic "journey" (path) from [fromId] to [toId] via
	 * OpenSubsonic `findSonicPath` — a queue that morphs between the two tracks.
	 * Requires the AudioMuse plugin ([sonicSimilarityAvailable]); the caller is
	 * expected to gate the UI on that. Reuses [mixSig] so the AudioMuse indicator
	 * lights for journeys too.
	 */
	fun startJourney(fromId: String, toId: String) {
		scope.launch {
			try {
				val ids = sessionManager.findSonicPathIds(fromId, toId)
				val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
				val queue = ids.mapNotNull { byId[it]?.toDomainModel() }.distinctBy { it.id }
				if (queue.isEmpty()) {
					Logger.i("RadioManager", "no path from $fromId to $toId")
					return@launch
				}
				playMix(
					queue, SavedQueueSource.JOURNEY,
					"${queue.first().title} → ${queue.last().title}"
				)
			} catch (e: Exception) {
				Logger.e("RadioManager", "failed to start journey $fromId -> $toId", e)
			}
		}
	}

	/**
	 * Resolve a CLAP text→mood search (AudioMuse Tier 2) to playable songs WITHOUT
	 * playing — the UI shows them as a proposed queue (Feishin-style) so the user
	 * can review and choose to play ([playMoodMix]) or enqueue. The caller gates the
	 * UI on [AudioMuseManager.isClapAvailable].
	 */
	suspend fun fetchMoodSearchSongs(query: String, count: Int = 100): List<DomainSong> {
		return try {
			val ids = audioMuseManager.fetchClapSearchIds(query, count)
			val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
			// Preserve CLAP order (best match first); drop ids not in the local library.
			ids.mapNotNull { byId[it]?.toDomainModel() }.distinctBy { it.id }
		} catch (e: Exception) {
			Logger.e("RadioManager", "mood search failed for '$query'", e)
			emptyList()
		}
	}

	/**
	 * Songs to show in the queue sheet's "Related" tab: recommendations DERIVED FROM
	 * THE WHOLE QUEUE that are NOT already queued — a browsing surface, so this fetches
	 * without playing (like [fetchMoodSearchSongs]).
	 *
	 * Why not just "similar to the current song": autoplay top-up ([topUp]) already
	 * seeds from the current track, so that list would echo what's already in the queue.
	 * Instead we seed [fetchSimilar] from a handful of tracks spread across [queue],
	 * rank candidates by how many of those seeds returned them (songs that fit MULTIPLE
	 * queue tracks surface first — the queue's shared character), and drop everything
	 * already queued. Falls back to a same-genre local mix (also queue-excluded) when
	 * the server is offline or every similar track is already queued.
	 */
	suspend fun fetchQueueRelatedSongs(queue: List<DomainSong>, count: Int = 50): List<DomainSong> {
		if (queue.isEmpty()) return emptyList()
		return try {
			val queued = queue.mapTo(HashSet()) { it.id }
			val state = mediaPlayer.uiState.value
			val nowPlaying = state.currentSong ?: queue.firstOrNull()
			val seeds = pickRelatedSeeds(queue, state.currentIndex)

			// Fetch each seed's neighbours in parallel; a single failing seed shouldn't
			// sink the tab, so swallow per-seed errors to an empty list.
			val perSeed = coroutineScope {
				seeds.map { seed ->
					async { runCatching { fetchSimilar(seed.id, count) }.getOrDefault(emptyList()) }
				}.awaitAll()
			}

			// Rank by cross-seed agreement (int[0] = #seeds that returned this id), tie-broken
			// by first-seen order (int[1]) so a single seed's ranking is still respected.
			val ranks = LinkedHashMap<String, IntArray>()
			var order = 0
			perSeed.forEach { ids ->
				ids.forEach { id ->
					val existing = ranks[id]
					if (existing == null) ranks[id] = intArrayOf(1, order++) else existing[0]++
				}
			}
			val rankedIds = ranks.entries
				.asSequence()
				.filter { it.key !in queued }
				.sortedWith(
					compareByDescending<Map.Entry<String, IntArray>> { it.value[0] }
						.thenBy { it.value[1] }
				)
				.map { it.key }
				.toList()

			val byId = songDao.getSongsByIds(rankedIds).associateBy { it.songId }
			val resolved = rankedIds.mapNotNull { byId[it]?.toDomainModel() }
				.distinctBy { it.id }
				.take(count)

			resolved.ifEmpty { localRadioSongs(nowPlaying, count, queued) }
		} catch (e: Exception) {
			Logger.e("RadioManager", "queue-related fetch failed", e)
			emptyList()
		}
	}

	/**
	 * Up to [RELATED_SEEDS] seed tracks for [fetchQueueRelatedSongs]: the now-playing
	 * track (from [currentIndex]) first, then tracks spread evenly across the queue so
	 * the recommendations reflect the whole queue, not just its head. Deduped by id.
	 */
	private fun pickRelatedSeeds(queue: List<DomainSong>, currentIndex: Int): List<DomainSong> {
		val unique = queue.distinctBy { it.id }
		if (unique.size <= RELATED_SEEDS) return unique
		val seeds = LinkedHashMap<String, DomainSong>()
		queue.getOrNull(currentIndex)?.let { seeds[it.id] = it }
		val step = unique.size.toFloat() / RELATED_SEEDS
		var i = 0f
		while (seeds.size < RELATED_SEEDS && i.toInt() < unique.size) {
			val s = unique[i.toInt()]
			if (!seeds.containsKey(s.id)) seeds[s.id] = s
			i += step
		}
		return seeds.values.toList()
	}

	/** Play a resolved mood mix as the queue (remote-aware — see [playMix]). */
	fun playMoodMix(songs: List<DomainSong>) {
		if (songs.isEmpty()) return
		playMix(songs, SavedQueueSource.MOOD_FLOW)
	}

	/**
	 * Play a generated mix as the queue (sets [mixSig] so the AudioMuse indicator
	 * lights). When another device is the active receiver, hand the mix to the
	 * session so it plays THERE (the hub forwards a do:load to the active device)
	 * rather than starting a conflicting local playback that desyncs the session.
	 *
	 * Local mixes are saved to queue history as their [kind] (radio / journey / Mood Flow) so the
	 * saved-queues list can group them; remote mixes are the hub's session (not local history).
	 */
	private fun playMix(
		songs: List<DomainSong>,
		kind: String = SavedQueueSource.RADIO,
		name: String? = null
	) {
		mixSig.value = songs.joinToString(",") { it.id }
		if (hubManager.isRemoteActive.value) {
			// Tag the shared history record with what this mix IS. Without the kind/name the
			// hub recorded remote-started mixes as anonymous "manual" queues.
			hubManager.loadSessionQueue(
				songs,
				sourceKind = kind,
				sourceName = name ?: defaultMixName(kind)
			)
		} else {
			// The LOCAL branch needs the name just as much: this path has no source collection at
			// all, so without it every locally started radio / Mood Flow / journey was saved as an
			// unnamed row and the history read "No name".
			mediaPlayer.loadRemoteQueue(
				songs, 0, 0L, true,
				savedQueueId = mediaPlayer.newQueueSessionId(songs),
				savedQueueKind = kind,
				savedQueueName = name ?: defaultMixName(kind)
			)
		}
	}

	/** Fallback display name for a generated mix that has no more specific one. */
	private fun defaultMixName(kind: String): String? = when (kind) {
		SavedQueueSource.RADIO -> "Radio"
		SavedQueueSource.MOOD_FLOW -> "Mood Flow"
		SavedQueueSource.JOURNEY -> "Song Journey"
		else -> null
	}

	/** Append a mood mix to the queue — to the session when remote, else locally. */
	fun enqueueMoodMix(songs: List<DomainSong>) {
		if (songs.isEmpty()) return
		if (hubManager.isRemoteActive.value) {
			hubManager.enqueueSessionQueue(songs)
		} else {
			mediaPlayer.appendToQueue(songs)
		}
	}

	/**
	 * Probe the server's OpenSubsonic extensions on (re)login and flip
	 * [sonicSimilarityAvailable] when the AudioMuse sonic plugin is present.
	 */
	private fun observeCapabilities() {
		scope.launch {
			sessionManager.isLoggedIn.collect { loggedIn ->
				if (!loggedIn) {
					_sonicSimilarityAvailable.value = false
					return@collect
				}
				try {
					_sonicSimilarityAvailable.value = sessionManager.fetchOpenSubsonicExtensions()
						.any { it.equals("sonicSimilarity", ignoreCase = true) }
				} catch (e: Exception) {
					Logger.e("RadioManager", "capability probe failed", e)
				}
			}
		}
	}

	/**
	 * Similar-song ids for [seedId]. Prefers `getSonicSimilarTracks` (guaranteed
	 * AudioMuse) when the plugin is present, falling back to `getSimilarSongs2`
	 * (heuristic, or AudioMuse-as-agent) otherwise or on error.
	 */
	private suspend fun fetchSimilar(seedId: String, count: Int): List<String> {
		if (_sonicSimilarityAvailable.value) {
			try {
				val sonic = sessionManager.fetchSonicSimilarTrackIds(seedId, count)
				if (sonic.isNotEmpty()) return sonic
			} catch (e: Exception) {
				Logger.w("RadioManager", "sonic similar failed, falling back", e)
			}
		}
		return sessionManager.fetchSimilarSongIds(seedId, count)
	}

	/**
	 * Offline / no-results fallback: a local "radio" from the library — same-genre songs
	 * (shuffled), topped up with random tracks. Keeps radio + autoplay working when AudioMuse
	 * (or Navidrome's similar-songs agents) is offline or returns nothing. [seed]'s genre is
	 * read from the local DB; a null/album/artist seed just draws random tracks.
	 */
	private suspend fun localRadioSongs(
		seed: DomainSong?,
		count: Int,
		exclude: Set<String>
	): List<DomainSong> {
		val genre = seed?.let { songDao.getSongById(it.id)?.genre }?.takeIf { it.isNotBlank() }
		val fetch = (count + exclude.size) * 2   // over-fetch to survive exclusions
		val pool = buildList {
			if (genre != null) addAll(songDao.getRandomSongsByGenre(genre, fetch))
			if (size < count) addAll(songDao.getRandomSongs(fetch))
		}
		return pool.map { it.toDomainModel() }
			.filter { it.id !in exclude && it.id != seed?.id }
			.distinctBy { it.id }
			.shuffled()
			.take(count)
	}

	/**
	 * navi-connect autoplay (Tier 1): when [AutoplayMode.Similar] is selected,
	 * keep the LOCAL queue topped up with songs similar to the current track as
	 * it nears the end. Gated off while another device is the active receiver
	 * (the local player is frozen then; the active device owns top-up). Triggers
	 * only on a real change of `currentSongId:remaining` so the ~5 Hz progress
	 * emissions don't spam the server.
	 */
	private fun observeAutoplay() {
		scope.launch {
			var lastSig = ""
			mediaPlayer.localUiState.collect { state ->
				val mode = _autoplayMode.value
				// Off → nothing; reset adaptive state so a fresh session starts clean.
				if (mode == AutoplayMode.Off) {
					lastSig = ""
					resetMoodFlow()
					return@collect
				}

				// Adaptive (Mood Flow) learns from skips/plays continuously — do this
				// on every emission, before the top-up gates below.
				if (mode == AutoplayMode.Adaptive) captureMoodSignal(state)

				// repeat all/one loops or holds the queue — nothing to top up.
				if (state.isPaused || state.repeatMode != 0) return@collect
				if (hubManager.isRemoteActive.value) return@collect

				val current = state.currentSong ?: return@collect
				if (state.currentIndex < 0 || state.queue.isEmpty()) return@collect

				val remaining = state.queue.size - state.currentIndex - 1
				if (remaining >= AUTOPLAY_THRESHOLD) return@collect

				val sig = "$mode:${current.id}:$remaining"
				if (sig == lastSig) return@collect
				lastSig = sig

				topUp(mode, current, state.queue)
			}
		}
	}

	private suspend fun topUp(mode: AutoplayMode, seed: DomainSong, queue: List<DomainSong>) {
		try {
			val existing = queue.mapTo(HashSet()) { it.id }
			// Similar (Tier 1) seeds from the current track; Fingerprint (Tier 2)
			// from listening habits; Adaptive (Tier 2) from the live Mood Flow
			// centroid (ADD = played-through, SUBTRACT = skipped). Tier-2 calls are
			// fail-soft → empty when the core API isn't configured/reachable.
			val ids = when (mode) {
				AutoplayMode.Fingerprint -> audioMuseManager.fetchSonicFingerprintIds(AUTOPLAY_FETCH_COUNT)
				AutoplayMode.Adaptive -> {
					// Alchemy needs ≥1 ADD; cold-start from the current track.
					val add = if (moodAddIds.isEmpty()) listOf(seed.id) else moodAddIds.toList()
					val character = preferenceManager.moodCharacter
					audioMuseManager.fetchAlchemyMixIds(
						add,
						moodSubtractIds.toList(),
						AUTOPLAY_FETCH_COUNT,
						temperature = character.temperature,
						subtractDistance = character.subtractDistance
					)
				}
				else -> fetchSimilar(seed.id, AUTOPLAY_FETCH_COUNT)
			}.filter { it !in existing }
			if (ids.isEmpty()) {
				// AudioMuse/Navidrome offline or no results → keep autoplay alive with a
				// local genre mix so playback doesn't just stop.
				val local = localRadioSongs(seed, AUTOPLAY_ADD_COUNT, existing)
				if (local.isNotEmpty()) mediaPlayer.appendToQueue(local)
				return
			}

			val byId = songDao.getSongsByIds(ids).associateBy { it.songId }
			val additions = ids.mapNotNull { byId[it]?.toDomainModel() }
				.distinctBy { it.id }
				.take(AUTOPLAY_ADD_COUNT)

			if (additions.isNotEmpty()) mediaPlayer.appendToQueue(additions)
		} catch (e: Exception) {
			Logger.e("RadioManager", "autoplay top-up failed", e)
		}
	}

	// ---- Adaptive "Mood Flow" session state (Yandex-style implicit steering) ----
	// All mutated/read only inside the single localUiState collector coroutine, so
	// no synchronization is needed.
	private val moodAddIds = ArrayDeque<String>()       // played-through / liked
	private val moodSubtractIds = ArrayDeque<String>()  // skipped
	private var moodPrevSongId: String? = null
	private var moodPrevProgress: Float = 0f

	private fun resetMoodFlow() {
		moodAddIds.clear()
		moodSubtractIds.clear()
		moodPrevSongId = null
		moodPrevProgress = 0f
	}

	/**
	 * Turn track transitions into ADD/SUBTRACT signals: a track left near its end
	 * is a play-through (ADD), left near its start is a skip (SUBTRACT); the middle
	 * is neutral. Recency-capped so the mood keeps drifting.
	 */
	private fun captureMoodSignal(state: PlayerUiState) {
		val curId = state.currentSong?.id
		if (curId != moodPrevSongId) {
			val left = moodPrevSongId
			if (left != null) {
				when {
					moodPrevProgress >= 0.85f -> recordMood(left, add = true)
					moodPrevProgress <= 0.20f -> recordMood(left, add = false)
				}
			}
			moodPrevSongId = curId
		}
		moodPrevProgress = state.progress
	}

	private fun recordMood(id: String, add: Boolean) {
		val target = if (add) moodAddIds else moodSubtractIds
		val other = if (add) moodSubtractIds else moodAddIds
		other.remove(id)   // a track can't be both
		target.remove(id)  // re-add at the most-recent end
		target.addLast(id)
		while (target.size > MOOD_MAX) target.removeFirst()
	}

	// MUST stay after all property declarations: these launch coroutines on
	// Dispatchers.Default that read fields above (e.g. _autoplayMode), and an
	// init block higher up would race construction and read them as null.
	init {
		observeAutoplay()
		observeCapabilities()
	}

	private companion object {
		// Start fetching when this many tracks remain after the current one.
		const val AUTOPLAY_THRESHOLD = 3
		// Ask for more than we add: similar ids may already be in the queue or
		// missing from the local DB.
		const val AUTOPLAY_FETCH_COUNT = 30
		const val AUTOPLAY_ADD_COUNT = 15
		// Recency window for Mood Flow ADD/SUBTRACT sets.
		const val MOOD_MAX = 12
		// How many queue tracks to seed the "Related" tab from (see fetchQueueRelatedSongs).
		const val RELATED_SEEDS = 5
	}
}
