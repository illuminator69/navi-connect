package paige.navic.shared

import android.os.Looper
import androidx.annotation.OptIn
import androidx.core.net.toUri
import androidx.media3.common.C
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
			Player.COMMAND_GET_METADATA
		)
		.build()

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

			Player.COMMAND_SEEK_TO_MEDIA_ITEM -> hubManager.actJump(mediaItemIndex)

			else -> if (positionMs != C.TIME_UNSET) hubManager.actSeek(positionMs)
		}
		return Futures.immediateVoidFuture()
	}

	override fun handleRelease(): ListenableFuture<*> = Futures.immediateVoidFuture()
}
