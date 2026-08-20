package paige.navic.shared

import android.os.Looper
import androidx.annotation.OptIn
import androidx.core.net.toUri
import androidx.media3.common.C
import androidx.media3.common.DeviceInfo
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import paige.navic.domain.manager.HubManager
import kotlin.time.Clock

/**
 * A media3 [Player] facade over the navi-connect REMOTE session.
 *
 * While another device is the active receiver, PlaybackService swaps this in as
 * the [androidx.media3.session.MediaSession]'s player, so the Android system
 * transport controls (notification, lock screen, Bluetooth, Android Auto) both
 * REFLECT and DRIVE the remote session instead of the silent local ExoPlayer.
 *
 * No audio is produced here — audio plays on the remote device. State is derived
 * from [HubManager.remoteSession]; transport is forwarded to the hub. Only the
 * transport commands are advertised (no media-item editing), so the session
 * controller can't mutate the remote queue through this facade.
 */
@OptIn(UnstableApi::class)
class RemoteSessionPlayer(
	looper: Looper,
	private val hubManager: HubManager,
	private val scope: CoroutineScope
) : SimpleBasePlayer(looper) {

	private val availableCommands = Player.Commands.Builder()
		.addAll(
			Player.COMMAND_PLAY_PAUSE,
			Player.COMMAND_SEEK_TO_NEXT,
			Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
			Player.COMMAND_SEEK_TO_PREVIOUS,
			Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
			Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
			Player.COMMAND_SEEK_TO_MEDIA_ITEM,
			Player.COMMAND_GET_CURRENT_MEDIA_ITEM,
			Player.COMMAND_GET_TIMELINE,
			Player.COMMAND_GET_METADATA,
			// Volume keys. Declaring these plus a PLAYBACK_TYPE_REMOTE DeviceInfo is
			// what makes Android route the hardware rocker to the session instead of
			// the phone's own music stream — which, while another device is playing,
			// changes nothing anyone can hear. No VolumeProviderCompat is involved:
			// SimpleBasePlayer's device-volume handlers are the modern equivalent.
			Player.COMMAND_GET_DEVICE_VOLUME,
			Player.COMMAND_SET_DEVICE_VOLUME_WITH_FLAGS,
			Player.COMMAND_ADJUST_DEVICE_VOLUME_WITH_FLAGS
		)
		.build()

	// The hub's scale is 0..100, so it is used unchanged rather than mapped onto a
	// coarser one: a step of 1 is what the picker's slider already sends.
	private val deviceInfo = DeviceInfo.Builder(DeviceInfo.PLAYBACK_TYPE_REMOTE)
		.setMinVolume(0)
		.setMaxVolume(100)
		.build()

	/**
	 * The active receiver's volume.
	 *
	 * Falls back to the last value we saw rather than a hardcoded 50: the device list and the
	 * active id arrive in separate frames, so a device that is momentarily absent from the list
	 * made the hardware rocker jump to half volume and then stay there until the next update.
	 */
	private fun remoteVolume(): Int {
		val activeId = hubManager.activeDeviceId.value
		val known = hubManager.devices.value.firstOrNull { it.id == activeId }?.volume
		if (known != null) lastKnownVolume = known
		return lastKnownVolume
	}

	@Volatile
	private var lastKnownVolume: Int = 100

	// Ignore transport commands briefly after this facade is swapped into the
	// session. media3 / the system controller re-syncs state on swap-in and can
	// call setPlayWhenReady(true), which would otherwise forward actPlay() to the
	// hub and unpause the (paused) active device on startup.
	private var graceUntilMs = 0L

	fun onActivated() {
		graceUntilMs = Clock.System.now().toEpochMilliseconds() + 1500
	}

	private fun inGrace(): Boolean = Clock.System.now().toEpochMilliseconds() < graceUntilMs

	init {
		// Recompute the exposed state whenever the remote session changes, and
		// tick once a second while playing so the scrubber/elapsed time advances.
		scope.launch {
			hubManager.remoteSession.collect { invalidateState() }
		}
		// The device list is where the receiver's volume lives, and it changes
		// independently of the session — including when another client moves the
		// slider. Without this the system volume UI would show a stale level.
		scope.launch {
			hubManager.devices.collect { invalidateState() }
		}
		scope.launch {
			while (isActive) {
				delay(1000)
				if (hubManager.remoteSession.value.isPlaying) invalidateState()
			}
		}
	}

	override fun getState(): State {
		val session = hubManager.remoteSession.value
		val tracks = session.tracks

		val playlist = tracks.mapIndexed { index, track ->
			MediaItemData.Builder("${track.id}:$index")
				.setMediaItem(
					MediaItem.Builder()
						.setMediaId(track.id)
						.setMediaMetadata(
							MediaMetadata.Builder()
								.setTitle(track.title)
								.setArtist(track.artist)
								.setAlbumTitle(track.album)
								.setArtworkUri(track.imageUrl?.toUri())
								.setIsPlayable(true)
								.build()
						)
						.build()
				)
				.setDurationUs(
					if (track.durationMs > 0) track.durationMs * 1000 else C.TIME_UNSET
				)
				.build()
		}

		val now = Clock.System.now().toEpochMilliseconds()
		val elapsed = if (session.isPlaying) (now - session.positionAtMs).coerceAtLeast(0) else 0
		val positionMs = (session.positionMs + elapsed).coerceAtLeast(0)

		return State.Builder()
			.setAvailableCommands(availableCommands)
			.setDeviceInfo(deviceInfo)
			.setDeviceVolume(remoteVolume())
			.setPlaybackState(if (tracks.isEmpty()) Player.STATE_IDLE else Player.STATE_READY)
			.setPlayWhenReady(
				session.isPlaying,
				Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST
			)
			.setPlaylist(playlist)
			.setCurrentMediaItemIndex(session.index.coerceIn(0, maxOf(0, tracks.size - 1)))
			.setContentPositionMs(
				PositionSupplier.getExtrapolating(positionMs, if (session.isPlaying) 1f else 0f)
			)
			.build()
	}

	override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
		if (inGrace()) return Futures.immediateVoidFuture()
		if (playWhenReady) hubManager.actPlay() else hubManager.actPause()
		return Futures.immediateVoidFuture()
	}

	override fun handleSeek(
		mediaItemIndex: Int,
		positionMs: Long,
		seekCommand: Int
	): ListenableFuture<*> {
		if (inGrace()) return Futures.immediateVoidFuture()
		when (seekCommand) {
			Player.COMMAND_SEEK_TO_NEXT,
			Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM -> hubManager.actNext()

			Player.COMMAND_SEEK_TO_PREVIOUS,
			Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM -> hubManager.actPrevious()

			Player.COMMAND_SEEK_TO_MEDIA_ITEM ->
				// Only forward a jump to a DIFFERENT track. media3 / the system
				// controller re-issues SEEK_TO_MEDIA_ITEM at the current index on
				// every state re-sync (metadata/position invalidations); forwarding
				// that self-seek makes the hub's `jump` reset position to 0 and
				// restart the active receiver — the phantom-jump loop that rewinds
				// playback right after each unpause. (Mirrors the ArtworkPager guard.)
				if (mediaItemIndex != hubManager.remoteSession.value.index) {
					hubManager.actJump(mediaItemIndex)
				}

			else -> if (positionMs != C.TIME_UNSET) hubManager.actSeek(positionMs)
		}
		return Futures.immediateVoidFuture()
	}

	override fun handleSetDeviceVolume(deviceVolume: Int, flags: Int): ListenableFuture<*> {
		// No grace guard here, unlike the transport handlers: a volume re-sync on
		// swap-in sends the value we just reported, so echoing it back is harmless,
		// and swallowing a real key press in the first 1.5s would be worse.
		hubManager.actSetVolume(deviceVolume.coerceIn(0, 100))
		return Futures.immediateVoidFuture()
	}

	override fun handleIncreaseDeviceVolume(flags: Int): ListenableFuture<*> {
		hubManager.actSetVolume((remoteVolume() + VOLUME_STEP).coerceIn(0, 100))
		return Futures.immediateVoidFuture()
	}

	override fun handleDecreaseDeviceVolume(flags: Int): ListenableFuture<*> {
		hubManager.actSetVolume((remoteVolume() - VOLUME_STEP).coerceIn(0, 100))
		return Futures.immediateVoidFuture()
	}

	override fun handleRelease(): ListenableFuture<*> = Futures.immediateVoidFuture()

	private companion object {
		/** One rocker press. 1-in-100 would need a hundred presses to cross the range. */
		const val VOLUME_STEP = 5
	}
}
