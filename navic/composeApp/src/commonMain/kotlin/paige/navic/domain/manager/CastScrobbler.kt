package paige.navic.domain.manager

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.IO
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import paige.navic.data.database.entities.SyncActionType
import paige.navic.util.core.Logger
import kotlin.time.Clock

private const val TAG = "CastScrobbler"

/** How often the hub's progress mirror is sampled. Matches the hub's own ~1 Hz reporting. */
private const val SAMPLE_INTERVAL_MS = 1_000L

/**
 * The largest position jump that still counts as listening.
 *
 * Anything bigger is a seek or a stall, and crediting it would let dragging the scrubber to the
 * end of a track scrobble it instantly. Generous enough to absorb a slow poll.
 */
private const val MAX_CREDITED_STEP_MS = 5_000L

/**
 * Scrobbles a Chromecast session, which nothing else can.
 *
 * In this protocol the *receiver* holds the Navidrome credentials and reports its own plays. A
 * Chromecast is a receiver that holds none and cannot be taught to, and every controller watching
 * the session is only a mirror — so an evening cast to a speaker recorded nothing at all: no play
 * counts, no recently-played, no ListenBrainz. [ScrobbleManager] can't cover it either, because it
 * is driven by the local player, which is silent for the whole session.
 *
 * **Only the bridging client scrobbles.** Scrobbling from "any controller watching a cast session"
 * double-counts the moment a second client is left open on it. Speaker ownership is already
 * arbitrated to exactly one client (PROTOCOL §12.2), so "am I the bridge for the active device"
 * designates one client and no other — that, and nothing weaker, is the gate below.
 */
class CastScrobbler(
	private val hubManager: HubManager,
	private val castBridgeStatus: CastBridgeStatus,
	private val connectivityManager: ConnectivityManager,
	private val syncManager: SyncManager,
	private val sessionManager: SessionManager,
	private val preferenceManager: PreferenceManager
) {
	// Its own scope: this outlives any screen and is not tied to the playback service, which is
	// exactly the point — the local player is stopped for the whole cast session.
	private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

	/** Queue index *and* track id: on repeat-one the id never changes, and only the index does. */
	private var currentKey: String? = null
	private var accumulatedMs = 0L
	private var lastPositionMs = 0L
	private var scrobbled = false

	fun start() {
		scope.launch {
			while (currentCoroutineContext().isActive) {
				delay(SAMPLE_INTERVAL_MS)
				runCatching { sample() }
					.onFailure { Logger.w(TAG, "sample failed: ${it.message}") }
			}
		}
	}

	private fun sample() {
		if (!bridgingActiveDevice()) {
			// Not ours to count. Reset rather than pause: if the speaker comes back to us it will
			// be a new session, and carrying a stale accumulator into it would over-credit.
			reset()
			return
		}

		val session = hubManager.remoteSession.value
		val track = session.nowPlaying ?: return reset()
		val key = "${session.index}|${track.id}"

		if (key != currentKey) {
			currentKey = key
			accumulatedMs = 0
			scrobbled = false
			lastPositionMs = interpolatedPosition(session)
			// So the server shows the speaker's track as live, rather than silently accruing a
			// play count that only appears once the track is nearly over.
			nowPlaying(track.id)
			return
		}

		val position = interpolatedPosition(session)
		val step = position - lastPositionMs
		lastPositionMs = position
		// Listening time, not playhead position. A paused speaker reports the same position every
		// tick and earns nothing; a seek jumps further than anyone can listen and earns nothing
		// either. Only real, forward, plausible progress counts.
		if (session.isPlaying && step > 0 && step <= MAX_CREDITED_STEP_MS) {
			accumulatedMs += step
		}

		maybeSubmit(track)
	}

	private fun maybeSubmit(track: RemoteTrack) {
		if (scrobbled) return
		val durationMs = track.durationMs
		if (durationMs <= 0) return
		// Guard the zero case explicitly: with a 0% threshold the accumulator satisfies `>= 0` on
		// its very first sample, which would scrobble every track the instant it started.
		if (accumulatedMs <= 0) return

		val playedEnough = accumulatedMs.toFloat() / durationMs >= preferenceManager.scrobblePercentage
		// The preference is SECONDS — the settings screen renders it as "30s". Comparing it
		// against a millisecond duration, as this and the local scrobbler both did, made the rule
		// vacuous: every track longer than 30ms passed.
		val longEnough = durationMs >= preferenceManager.minDurationToScrobble * 1000
		if (!playedEnough || !longEnough) return

		scrobbled = true
		submit(track.id)
	}

	/** True only when the hub's active device is a speaker THIS client is bridging. */
	private fun bridgingActiveDevice(): Boolean {
		val active = hubManager.activeDeviceId.value ?: return false
		return castBridgeStatus.speakers.value.any {
			"cast-${it.id}" == active && it.state == CastBridgeState.BRIDGING
		}
	}

	private fun interpolatedPosition(session: RemoteSessionState): Long {
		val elapsed = if (session.isPlaying) {
			(Clock.System.now().toEpochMilliseconds() - session.positionAtMs).coerceAtLeast(0)
		} else 0L
		return session.positionMs + elapsed
	}

	private fun reset() {
		currentKey = null
		accumulatedMs = 0
		lastPositionMs = 0
		scrobbled = false
	}

	private fun submit(songId: String) {
		if (!preferenceManager.enableScrobbling) return
		scope.launch(Dispatchers.IO) {
			// Same offline behaviour as the local scrobbler: queue it rather than lose it.
			if (connectivityManager.isOnline.value) {
				runCatching { sessionManager.api.scrobble(songId, submission = true) }
					.onFailure { syncManager.enqueueAction(SyncActionType.SCROBBLE, songId) }
			} else {
				syncManager.enqueueAction(SyncActionType.SCROBBLE, songId)
			}
		}
	}

	private fun nowPlaying(songId: String) {
		if (!preferenceManager.enableScrobbling) return
		if (!connectivityManager.isOnline.value) return
		scope.launch(Dispatchers.IO) {
			runCatching { sessionManager.api.scrobble(songId, submission = false) }
		}
	}
}
