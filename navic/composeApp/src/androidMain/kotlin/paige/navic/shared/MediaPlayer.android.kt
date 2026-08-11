package paige.navic.shared

import android.app.Application
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Looper
import androidx.annotation.OptIn
import androidx.core.net.toUri
import androidx.lifecycle.viewModelScope
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.common.Timeline
import androidx.media3.common.TrackSelectionParameters
import androidx.media3.common.Tracks
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.DefaultMediaNotificationProvider
import androidx.media3.session.MediaController
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.MoreExecutors
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import androidx.compose.runtime.snapshotFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.koin.core.component.KoinComponent
import org.koin.core.component.inject
import paige.navic.data.database.dao.AlbumDao
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.manager.AndroidScrobbleManager
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.manager.SyncManager
import paige.navic.domain.models.DomainAlbum
import paige.navic.domain.models.DomainExplicitStatus
import paige.navic.domain.models.DomainRadio
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongCollection
import paige.navic.domain.models.SavedQueueSource
import paige.navic.domain.models.toSavedQueueKind
import paige.navic.domain.models.settings.ReplayGainMode
import paige.navic.domain.repositories.PlayerStateRepository
import paige.navic.domain.repositories.SavedQueueRepository
import paige.navic.ui.components.common.CoilBitmapLoader
import paige.navic.ui.core.PlayerUiState
import paige.navic.util.core.Logger
import paige.navic.util.core.ResourceProvider
import paige.navic.util.core.effectiveGain
import java.io.File
import kotlin.time.Duration
import kotlin.time.Duration.Companion.milliseconds

@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService(), KoinComponent {
	private var mediaSession: MediaSession? = null
	private var exoPlayer: ExoPlayer? = null
	private var remotePlayer: RemoteSessionPlayer? = null
	private val serviceScope = MainScope()
	private var scrobbleManager: AndroidScrobbleManager? = null
	private val resourceProvider: ResourceProvider by inject()

	private val connectivityManager: ConnectivityManager by inject()

	private val syncManager: SyncManager by inject()
	private val sessionManager: SessionManager by inject()
	private val preferenceManager: PreferenceManager by inject()
	private val hubManager: HubManager by inject()

	@OptIn(UnstableApi::class)
	override fun onCreate() {
		super.onCreate()
		// Read as far ahead as the track allows rather than the ~1 min a video-shaped default gives
		// us: a fully-buffered song rides out a Wi-Fi→cellular handover (or a lift ride) without a
		// gap, which is the whole point on a music player. maxBufferMs alone isn't enough — the
		// real limiter is targetBufferBytes, which otherwise defaults to a fraction of a megabyte
		// for audio and caps a lossless stream at a few seconds. 48 MB covers a long FLAC; it is a
		// ceiling, not an allocation, so short/transcoded tracks still cost little.
		val loadControl = DefaultLoadControl.Builder()
			.setBufferDurationsMs(
				/* minBufferMs = */ 32_000,
				/* maxBufferMs = */ 600_000,
				/* bufferForPlaybackMs = */ 2_500,
				/* bufferForPlaybackAfterRebufferMs = */ 5_000
			)
			.setTargetBufferBytes(48 * 1024 * 1024)
			.setBackBuffer(10_000, true)
			.build()

		val notificationProvider = DefaultMediaNotificationProvider.Builder(this)
			.build().apply {
				setSmallIcon(resourceProvider.icNavic)
			}

		val httpDataSourceFactory = DefaultHttpDataSource.Factory()
			.setDefaultRequestProperties(preferenceManager.customHeadersMap())
		val dataSourceFactory = DefaultDataSource.Factory(this, httpDataSourceFactory)
		val mediaSourceFactory = DefaultMediaSourceFactory(dataSourceFactory)

		val player = ExoPlayer.Builder(this)
			.setLoadControl(loadControl)
			.setMediaSourceFactory(mediaSourceFactory)
			.setHandleAudioBecomingNoisy(true)
			.setWakeMode(C.WAKE_MODE_NETWORK)
			.build()
			.apply {
				setAudioAttributes(
					AudioAttributes.Builder()
						.setUsage(C.USAGE_MEDIA)
						.setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
						.build(),
					true
				)
				setMediaNotificationProvider(notificationProvider)
				trackSelectionParameters =
					trackSelectionParameters.buildUpon().setAudioOffloadPreferences(
						TrackSelectionParameters.AudioOffloadPreferences
							.Builder()
							.setIsGaplessSupportRequired(preferenceManager.gaplessPlayback)
							.setAudioOffloadMode(
								if (preferenceManager.audioOffload) {
									TrackSelectionParameters.AudioOffloadPreferences.AUDIO_OFFLOAD_MODE_ENABLED
								} else {
									TrackSelectionParameters.AudioOffloadPreferences.AUDIO_OFFLOAD_MODE_DISABLED
								}
							)
							.build()
					).build()
			}

		scrobbleManager =
			AndroidScrobbleManager(player, serviceScope, connectivityManager, syncManager, sessionManager, preferenceManager)

		val sessionIntent = applicationContext.packageManager
			.getLaunchIntentForPackage(applicationContext.packageName)
			?.apply {
				flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or
					Intent.FLAG_ACTIVITY_CLEAR_TOP
			}

		val sessionPendingIntent = PendingIntent.getActivity(
			this,
			0,
			sessionIntent,
			PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
		)

		mediaSession = MediaSession.Builder(this, player)
			.setSessionActivity(sessionPendingIntent)
			.setBitmapLoader(CoilBitmapLoader(this))
			.build()
		exoPlayer = player

		// navi-connect: while another device is the active receiver, swap the
		// session's player to a facade that mirrors AND drives the REMOTE session,
		// so the Android system controls (notification / lock screen / Bluetooth)
		// reflect and control it. Swap back to the local ExoPlayer when playback
		// returns here. NOTE: starting brand-new LOCAL playback while remote is
		// blocked by the facade (the queue can't be mutated through it) — transfer
		// playback here first.
		remotePlayer = RemoteSessionPlayer(Looper.getMainLooper(), hubManager, serviceScope)
		serviceScope.launch {
			hubManager.isRemoteActive.collect { remote ->
				val session = mediaSession ?: return@collect
				val local = exoPlayer ?: return@collect
				val rp = remotePlayer ?: return@collect
				if (remote) {
					if (session.player !== rp) {
						local.pause()
						rp.onActivated()
						session.player = rp
					}
				} else {
					if (session.player === rp) {
						session.player = local
					}
				}
			}
		}
	}

	override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? {
		return mediaSession
	}

	override fun onTaskRemoved(rootIntent: Intent?) {
		onDestroy()
	}

	override fun onDestroy() {
		scrobbleManager?.release()
		serviceScope.cancel()
		stopForeground(STOP_FOREGROUND_REMOVE)
		mediaSession?.run {
			player.stop()
			release()
		}
		exoPlayer?.release()
		remotePlayer?.release()
		super.onDestroy()
		mediaSession = null
		exoPlayer = null
		remotePlayer = null
		stopSelf()
	}

	companion object {
		fun newSessionToken(context: Context): SessionToken {
			return SessionToken(context, ComponentName(context, PlaybackService::class.java))
		}
	}
}

class AndroidMediaPlayerViewModel(
	private val application: Application,
	stateRepository: PlayerStateRepository,
	private val albumDao: AlbumDao,
	downloadManager: DownloadManager,
	connectivityManager: ConnectivityManager,
	private val sessionManager: SessionManager,
	private val preferenceManager: PreferenceManager,
	savedQueueRepository: SavedQueueRepository
) : MediaPlayerViewModel(
	stateRepository = stateRepository,
	downloadManager = downloadManager,
	connectivityManager = connectivityManager,
	savedQueueRepository = savedQueueRepository
) {
	private var controller: MediaController? = null
	private var controllerFuture: ListenableFuture<MediaController>? = null

	private var loadingCollectionId: String? = null

	private var pendingSyncState: PlayerUiState? = null

	// Last map seen by the URI re-resolution pass, so [reresolveQueueUris] can be re-run from a
	// track transition without waiting for the flow to re-emit.
	private var lastDownloadedMap: Map<String, String> = emptyMap()

	// Set when re-resolution deliberately LEFT the playing item alone (see [reresolveQueueUris]);
	// cleared by the re-run on the next track transition, which is when the swap is free.
	private var currentItemReresolvePending = false

	// A source/network error killed playback; retry (same URI, resumed by range request) as soon
	// as we're back online rather than leaving the player dead in STATE_IDLE.
	private var retryOnReconnect = false

	init {
		connectToService()
	}

	private fun connectToService() {
		viewModelScope.launch {
			val sessionToken = PlaybackService.newSessionToken(application)
			controllerFuture = MediaController.Builder(application, sessionToken).buildAsync()
			controllerFuture?.addListener({
				controller = controllerFuture?.get()
				setupController()
			}, MoreExecutors.directExecutor())
		}
	}

	/**
	 * Whether a downloaded copy should actually be played, given the current connection.
	 *
	 * Always yes off a metered link. On cellular it follows `preferDownloadsOnCellular`: on (the
	 * default) plays the download and spends no data; off streams the server's original, which is
	 * what you want when the downloads are space-saving transcodes and you'd rather have the
	 * original away from Wi-Fi.
	 */
	private fun useDownloadFor(localPath: String?): Boolean {
		if (localPath == null) return false
		// A file we already have always beats a stream we can't fetch — "prefer the server's
		// original" can't mean "play nothing" when there's no usable connection.
		if (!connectivityManager.isOnline.value) return true
		if (!connectivityManager.isCellular.value) return true
		return preferenceManager.preferDownloadsOnCellular
	}

	private fun getStreamUrl(id: String): Uri {
		val isCellular = connectivityManager.isCellular.value
		val bitrate = if (preferenceManager.isAdvancedTranscodingActive) {
			if (isCellular) preferenceManager.customMaxBitrateCellular else preferenceManager.customMaxBitrateWifi
		} else {
			if (isCellular) preferenceManager.streamingQualityCellular.bitrateAndroid else preferenceManager.streamingQualityWifi.bitrateAndroid
		}
		val container = if (isCellular) preferenceManager.streamingQualityCellular.containerAndroid else preferenceManager.streamingQualityWifi.containerAndroid

		return sessionManager.api.getStreamUrl(id, bitrate, container)
			.toUri()
			.buildUpon()
			.appendQueryParameter("estimateContentLength", "true")
			.build()
	}

	/**
	 * Re-point every queued item at the uri it *should* have right now — the download if
	 * [useDownloadFor] says so, otherwise a stream url at the current network's quality.
	 *
	 * The playing item is deliberately exempt from remote re-pointing. Replacing its uri throws
	 * away everything ExoPlayer has buffered and restarts the request, so a Wi-Fi→cellular handover
	 * used to *stop the song*, ask the server for a fresh transcode, and stall until enough of it
	 * arrived. Keeping the uri means the in-flight response (or, if it did drop, a Range retry of
	 * the same one) just continues, and the new quality takes effect from the next track. Swapping
	 * TO a local file is still allowed mid-track: that reads off disk, so it costs no stall and is
	 * the thing that keeps playback alive when the connection goes away entirely.
	 */
	private fun reresolveQueueUris(downloadedMap: Map<String, String>) {
		val player = controller ?: return
		currentItemReresolvePending = false

		for (i in 0 until player.mediaItemCount) {
			val item = player.getMediaItemAt(i)
			val id = item.mediaId
			val localPath = downloadedMap[id].takeIf { useDownloadFor(it) }

			val isCurrentlyLocal = item.localConfiguration?.uri?.scheme == "file"

			val newItem = if (localPath != null) {
				if (!isCurrentlyLocal) {
					item.buildUpon()
						.setUri(File(localPath).toUri())
						.build()
				} else null
			} else {
				val newUri = getStreamUrl(id)
				if (isCurrentlyLocal || item.localConfiguration?.uri != newUri) {
					item.buildUpon()
						.setUri(newUri)
						.build()
				} else null
			}

			if (newItem == null) continue

			if (i == player.currentMediaItemIndex) {
				if (newItem.localConfiguration?.uri?.scheme != "file") {
					// Remote→remote (quality change) or local→remote: defer, it would stall.
					currentItemReresolvePending = true
					continue
				}
				val currentPosition = player.currentPosition
				player.replaceMediaItem(i, newItem)
				player.seekTo(i, currentPosition)
			} else {
				player.replaceMediaItem(i, newItem)
			}
		}
	}

	private fun setupController() {
		viewModelScope.launch {
			controller?.apply {
				addListener(object : Player.Listener {
					override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
						updatePlaybackState()

						// A track ending and the next one starting does not change
						// isPlaying, so without this the mini-player and turntable
						// widgets kept rendering the previous song's title, artist and
						// art until playback was next paused or resumed. Resolve the
						// song from the item that just became current rather than
						// trusting _uiState — updatePlaybackState above has just set
						// it, but a broadcast that silently lags one track is exactly
						// the bug being fixed, so don't make it depend on ordering.
						broadcastNowPlaying(
							controller?.isPlaying == true,
							songForMediaItem(mediaItem)
						)

						// The track we protected from a mid-song uri swap isn't playing any more,
						// so apply the deferred quality/source change now — this is where a
						// network handover actually lands.
						if (currentItemReresolvePending) reresolveQueueUris(lastDownloadedMap)

						if (reason == Player.MEDIA_ITEM_TRANSITION_REASON_AUTO) {
							mediaItem?.mediaId?.let { id ->
								if (!isAvailable(id)) {
									val c = controller
									if (c != null && c.hasNextMediaItem()) {
										c.seekToNextMediaItem()
									} else {
										// No next item: an unavailable LAST track would leave the player
										// stuck buffering forever — stop rather than spin.
										c?.pause()
									}
								}
							}
						}
					}

					override fun onIsPlayingChanged(isPlaying: Boolean) {
						_uiState.update { it.copy(isPaused = !isPlaying) }
						if (isPlaying) startProgressLoop()
						broadcastNowPlaying(isPlaying, _uiState.value.currentSong)
					}

					// Metadata can change without the item doing so (a live stream, or
					// a re-resolved item). The widgets key off metadata, so they need
					// this too.
					override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) {
						broadcastNowPlaying(
							controller?.isPlaying == true,
							_uiState.value.currentSong
						)
					}

					override fun onPlaybackStateChanged(playbackState: Int) {
						_uiState.update { it.copy(isLoading = playbackState == Player.STATE_BUFFERING) }
						updatePlaybackState()
					}

					override fun onPlayerError(error: PlaybackException) {
						// A dropped connection surfaces as a source error and parks the player in
						// STATE_IDLE, where nothing revives it. Remember to prepare() once we're
						// online again (see the re-resolution collector) so the song resumes on
						// its existing uri rather than needing a re-queue.
						val isNetwork = when (error.errorCode) {
							PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED,
							PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT,
							PlaybackException.ERROR_CODE_IO_UNSPECIFIED -> true

							else -> false
						}
						if (isNetwork) {
							// Only ever retried off a connectivity change, never immediately —
							// an unconditional prepare() here would spin error→prepare→error.
							retryOnReconnect = true
						} else {
							Logger.e("MediaPlayer", "playback error", error)
						}
					}

					override fun onShuffleModeEnabledChanged(shuffleModeEnabled: Boolean) {
						_uiState.update { it.copy(isShuffleEnabled = shuffleModeEnabled) }
					}

					override fun onRepeatModeChanged(repeatMode: Int) {
						_uiState.update { it.copy(repeatMode = repeatMode) }
					}

					override fun onTracksChanged(tracks: Tracks) {
						updatePlaybackProperties(tracks)
					}

					override fun onTimelineChanged(timeline: Timeline, reason: Int) {
						updatePlaybackState()
					}
				})
				updatePlaybackState()
				updatePlaybackProperties(currentTracks)

				downloadManager.allDownloads.first()
				pendingSyncState?.let { state ->
					syncPlayerWithState(state)
					pendingSyncState = null
				}

				combine(
					downloadManager.downloadedSongs,
					connectivityManager.isCellular,
					snapshotFlow { preferenceManager.streamingQualityWifi },
					snapshotFlow { preferenceManager.streamingQualityCellular },
					snapshotFlow { preferenceManager.isAdvancedTranscodingActive },
					snapshotFlow { preferenceManager.customMaxBitrateWifi },
					snapshotFlow { preferenceManager.customMaxBitrateCellular },
					// So flipping the toggle (or moving on/off cellular) re-points the queue at the
					// downloads or back at the server without needing a re-queue.
					snapshotFlow { preferenceManager.preferDownloadsOnCellular },
					connectivityManager.isOnline
				) { it.toList() }.distinctUntilChanged().collectLatest { args ->
					@Suppress("UNCHECKED_CAST")
					val downloadedMap = args[0] as Map<String, String>
					lastDownloadedMap = downloadedMap
					reresolveQueueUris(downloadedMap)

					// Back online after a source error: resume the SAME uri from where the buffer
					// ran out (prepare() re-requests with a Range header) instead of leaving the
					// player dead until the user hits play.
					if (retryOnReconnect && connectivityManager.isOnline.value) {
						retryOnReconnect = false
						controller?.prepare()
					}
				}
			}
		}
	}

	private fun refreshCurrentCollection(albumId: String) {
		if (loadingCollectionId == albumId) return
		loadingCollectionId = albumId

		viewModelScope.launch {
			runCatching {
				val album = albumDao.getAlbumById(albumId)

				_uiState.update { it.copy(currentCollection = album?.toDomainModel()) }
				// A miss (album not cached yet) must NOT stick — otherwise we never retry
				// once it syncs in. Only a real hit keeps the loading guard set.
				if (album == null) loadingCollectionId = null
			}.onFailure {
				loadingCollectionId = null
			}
		}
	}

	/** The queued song a MediaItem stands for, or the current one if it isn't queued. */
	private fun songForMediaItem(mediaItem: MediaItem?): DomainSong? {
		val id = mediaItem?.mediaId ?: return _uiState.value.currentSong
		return _uiState.value.queue.firstOrNull { it.id == id } ?: _uiState.value.currentSong
	}

	/**
	 * Tell the home-screen widgets what is playing.
	 *
	 * MiniPlayer/TurnTable/QuickPicks receivers listen for this and have no other
	 * source of truth, so every place the track or the play state changes has to
	 * send it — see the call sites in the controller listener.
	 */
	private fun broadcastNowPlaying(isPlaying: Boolean, song: DomainSong?) {
		val intent = Intent("${application.packageName}.NOW_PLAYING_UPDATED").apply {
			setPackage(application.packageName)
			putExtra("isPlaying", isPlaying)
			putExtra("title", song?.title ?: "Unknown song")
			putExtra("artist", song?.artistName ?: "Unknown artist")
			putExtra("artUrl", song?.coverArtId?.let { sessionManager.getCoverArtUrl(it) })
		}
		application.sendBroadcast(intent)
	}

	private fun updatePlaybackState() {
		val controller = controller ?: return
		val index = controller.currentMediaItemIndex
		val currentSong = _uiState.value.queue.getOrNull(index)

		val derivedCollection = currentSong?.let { song ->
			val stateCollection = _uiState.value.currentCollection

			if (stateCollection?.id == song.albumId.toString()) {
				stateCollection
			} else {
				refreshCurrentCollection(song.albumId.toString())
				null
			}
		}

		_uiState.update { state ->
			state.copy(
				currentIndex = index,
				currentSong = currentSong,
				currentCollection = derivedCollection ?: state.currentCollection,
				isPaused = !controller.isPlaying,
				isShuffleEnabled = controller.shuffleModeEnabled,
				repeatMode = controller.repeatMode
			)
		}
		applyReplayGain()
		updateProgress()
	}

	private fun applyReplayGain() {
		if (preferenceManager.replayGainMode != ReplayGainMode.Off) {
			(_uiState.value.currentSong)?.replayGain?.let { replayGain ->
				controller?.volume = replayGain.effectiveGain(preferenceManager.replayGainMode)
			}
		} else {
			controller?.volume = 1f
		}
	}

	override fun syncPlayerWithState(state: PlayerUiState) {
		viewModelScope.launch {
			val player = controller

			if (player == null) {
				pendingSyncState = state
				return@launch
			}

			if (state.queue.isEmpty() || player.mediaItemCount > 0) return@launch

			val mediaItems = withContext(Dispatchers.Default) {
				state.queue.map { it.toMediaItem() }
			}

			player.setMediaItems(mediaItems)

			player.shuffleModeEnabled = state.isShuffleEnabled
			player.repeatMode = state.repeatMode
			player.playbackParameters = PlaybackParameters(state.playbackSpeed)

			val index = if (state.currentIndex in mediaItems.indices) state.currentIndex else 0

			val songDurationMs = state.queue.getOrNull(index)?.duration?.inWholeMilliseconds ?: 0L

			val position = if (songDurationMs > 0) {
				(state.progress * songDurationMs).toLong()
			} else {
				0L
			}

			player.seekTo(index, position)
			player.prepare()
		}
	}

	override fun loadRemoteQueue(
		songs: List<DomainSong>,
		index: Int,
		positionMs: Long,
		play: Boolean,
		savedQueueId: String?,
		savedQueueKind: String,
		savedQueueName: String?
	) {
		viewModelScope.launch {
			val player = controller ?: return@launch
			if (songs.isEmpty()) return@launch

			val mediaItems = withContext(Dispatchers.Default) {
				songs.map { it.toMediaItem() }
			}

			val idx = if (index in songs.indices) index else 0
			val durationMs = songs.getOrNull(idx)?.duration?.inWholeMilliseconds ?: 0L

			_uiState.update {
				it.copy(
					queue = songs,
					currentSong = songs.getOrNull(idx),
					currentCollection = null,
					currentIndex = idx,
					isPaused = !play,
					isLoading = false,
					// A hub-driven / adopted queue is a transient mirror of the session (savedQueueId
					// = null), so observeAndSaveState won't overwrite the user's saved-queue row. A
					// locally-started mix / undo restore passes a real id + kind to persist as its own
					// history session instead.
					savedQueueId = savedQueueId,
					savedQueueKind = savedQueueKind,
					// currentCollection is null on this path, so without an explicit name a locally
					// started radio / Mood Flow / journey persisted as an unnamed row ("No name").
					savedQueueName = savedQueueName,
					progress = if (durationMs > 0) {
						(positionMs.toFloat() / durationMs.toFloat()).coerceIn(0f, 1f)
					} else 0f
				)
			}

			player.setMediaItems(mediaItems)
			player.seekTo(idx, positionMs)
			player.prepare()
			if (play) player.play() else player.pause()
		}
	}

	override fun reconcileRemoteQueue(songs: List<DomainSong>, index: Int) {
		viewModelScope.launch {
			val player = controller ?: return@launch
			val currentId = _uiState.value.currentSong?.id
			val newCurrent = songs.getOrNull(index)

			// Current track changed (or nothing loaded) → caller-visible
			// behaviour is a plain reload; keep the position at zero.
			if (currentId == null || newCurrent?.id != currentId || player.mediaItemCount == 0) {
				val st = _uiState.value
				// Carry the saved-queue identity through. loadRemoteQueue defaults it to null
				// ("a transient mirror of someone else's queue"), which is right for a do:load
				// but wrong here: this is the SAME session being reconciled after an edit, and
				// blanking the id mid-playback made the next publish mint a fresh one — forking
				// a second Continue Listening card for one listening session.
				loadRemoteQueue(
					songs, index, 0L, !st.isPaused,
					st.savedQueueId, st.savedQueueKind, st.savedQueueName
				)
				return@launch
			}

			val items = withContext(Dispatchers.Default) { songs.map { it.toMediaItem() } }
			val currentIndex = player.currentMediaItemIndex

			// Rebuild the tail, then the head, leaving the playing item alone.
			if (player.mediaItemCount > currentIndex + 1) {
				player.removeMediaItems(currentIndex + 1, player.mediaItemCount)
			}
			if (index + 1 < items.size) {
				player.addMediaItems(items.subList(index + 1, items.size))
			}
			if (currentIndex > 0) {
				player.removeMediaItems(0, currentIndex)
			}
			if (index > 0) {
				player.addMediaItems(0, items.subList(0, index))
			}

			_uiState.update {
				it.copy(queue = songs, currentIndex = index, currentSong = newCurrent)
			}
		}
	}

	override fun setPlayerVolume(volume: Float) {
		viewModelScope.launch {
			controller?.volume = volume.coerceIn(0f, 1f)
		}
	}

	override fun applyRemoteRepeat(mode: Int) {
		viewModelScope.launch {
			controller?.repeatMode = mode
			_uiState.update { it.copy(repeatMode = mode) }
		}
	}

	override fun applyRemoteShuffle(enabled: Boolean) {
		viewModelScope.launch {
			controller?.shuffleModeEnabled = enabled
			_uiState.update { it.copy(isShuffleEnabled = enabled) }
		}
	}

	private var progressJob: Job? = null

	private fun startProgressLoop() {
		// Every isPlaying→true used to launch a fresh, untracked loop; rapid
		// pause/play stacked overlapping loops that fought over `progress` (jitter).
		// Cancel the previous one before starting a new one so only ever one runs.
		progressJob?.cancel()
		progressJob = viewModelScope.launch {
			while (controller?.isPlaying == true) {
				val player = controller ?: break
				val duration = player.duration
				if (duration > 0) {
					val progress =
						(player.currentPosition.toFloat() / duration.toFloat()).coerceIn(0f, 1f)
					_uiState.update { it.copy(progress = progress) }
				}
				delay(200.milliseconds)
			}
		}
	}

	private fun updateProgress() {
		controller?.let { player ->
			val duration = player.duration
			if (duration > 0) {
				val pos = player.currentPosition
				val progress = (pos.toFloat() / duration.toFloat()).coerceIn(0f, 1f)
				_uiState.update { it.copy(progress = progress) }
			}
		}
	}

	@OptIn(UnstableApi::class)
	private fun updatePlaybackProperties(tracks: Tracks) {
		val audioGroup = tracks.groups.firstOrNull { it.type == C.TRACK_TYPE_AUDIO && it.isSelected }
		if (audioGroup != null) {
			for (i in 0 until audioGroup.length) {
				if (audioGroup.isTrackSelected(i)) {
					val format = audioGroup.getTrackFormat(i)
					Logger.i("MediaPlayer", "Active Track Format: $format")
					_uiState.update { state ->
						state.copy(
							playbackBitrate = format.bitrate.takeIf { it > 0 },
							playbackSampleRate = format.sampleRate.takeIf { it > 0 },
							playbackMimeType = format.sampleMimeType
						)
					}
					break
				}
			}
		}
	}

	override fun addToQueueSingleLocal(song: DomainSong) {
		viewModelScope.launch {
			controller?.addMediaItem(withContext(Dispatchers.Default) { song.toMediaItem() })
			_uiState.update { state ->
				val newQueue = state.queue + song
				state.copy(
					queue = newQueue,
					currentIndex = if (state.currentIndex == -1) 0 else state.currentIndex,
					currentSong = if (state.currentIndex == -1) song else state.currentSong
				)
			}
		}
	}

	override fun addToQueueLocal(collection: DomainSongCollection) {
		viewModelScope.launch {
			val (items, newCollection) = withContext(Dispatchers.Default) {
				val newCollection = if (collection is DomainAlbum) collection.songs.sortedWith(compareBy(
					{ it.discNumber },
					{ it.trackNumber }
				)) else collection.songs
				newCollection.map { it.toMediaItem() } to newCollection
			}
			controller?.addMediaItems(items)
			_uiState.update { state ->
				val newQueue = state.queue + newCollection
				state.copy(
					queue = newQueue,
					currentIndex = if (state.currentIndex == -1) 0 else state.currentIndex,
					currentSong = if (state.currentIndex == -1) newCollection.firstOrNull() else state.currentSong
				)
			}
		}
	}

	override fun appendToQueue(songs: List<DomainSong>) {
		if (songs.isEmpty()) return
		viewModelScope.launch {
			val items = withContext(Dispatchers.Default) { songs.map { it.toMediaItem() } }
			controller?.addMediaItems(items)
			_uiState.update { state ->
				val newQueue = state.queue + songs
				state.copy(
					queue = newQueue,
					currentIndex = if (state.currentIndex == -1) 0 else state.currentIndex,
					currentSong = if (state.currentIndex == -1) newQueue.firstOrNull() else state.currentSong
				)
			}
		}
	}

	override fun removeFromQueue(index: Int) {
		viewModelScope.launch {
			// Defensive: the displayed list can diverge from the local queue (remote
			// session mirror) — never index out of bounds.
			if (index !in 0 until _uiState.value.queue.size) return@launch
			controller?.removeMediaItem(index)
			_uiState.update { state ->
				val newQueue = state.queue.toMutableList().apply { removeAt(index) }
				val newIndex = when {
					index < state.currentIndex -> state.currentIndex - 1
					index == state.currentIndex -> if (newQueue.isEmpty()) -1 else state.currentIndex.coerceAtMost(
						newQueue.size - 1
					)

					else -> state.currentIndex
				}
				state.copy(
					queue = newQueue,
					currentIndex = newIndex,
					currentSong = if (newIndex == -1) null else newQueue[newIndex]
				)
			}
		}
	}

	override fun moveQueueItem(fromIndex: Int, toIndex: Int) {
		viewModelScope.launch {
			// Defensive: indices come from the displayed list, which can diverge
			// from the local queue (e.g. while a remote session is mirrored in) —
			// never index out of the local queue.
			val size = _uiState.value.queue.size
			if (fromIndex !in 0 until size || toIndex !in 0 until size || fromIndex == toIndex) {
				return@launch
			}
			controller?.moveMediaItem(fromIndex, toIndex)
			_uiState.update { state ->
				val newQueue = state.queue.toMutableList().apply {
					val item = removeAt(fromIndex)
					add(toIndex, item)
				}
				val newIndex = when (state.currentIndex) {
					fromIndex -> toIndex
					in (fromIndex + 1)..toIndex -> state.currentIndex - 1
					in toIndex until fromIndex -> state.currentIndex + 1
					else -> state.currentIndex
				}
				state.copy(
					queue = newQueue,
					currentIndex = newIndex,
					currentSong = if (newIndex == -1) null else newQueue[newIndex]
				)
			}
		}
	}

	override fun clearQueue() {
		viewModelScope.launch {
			_uiState.update {
				it.copy(
					queue = emptyList(),
					currentSong = null,
					currentIndex = -1,
					progress = 0f,
					// Clearing ends the session: drop its saved-queue identity so the
					// SavedQueues screen doesn't keep showing it as the active queue.
					savedQueueId = null,
					savedQueueKind = "manual",
					savedQueueName = null
				)
			}
			controller?.clearMediaItems()
			// The publish path bails on an empty queue, so without this the hub kept serving the
			// queue we just threw away — open another client and the whole list was back.
			remotePlaybackRouter?.takeIf { it.isHubConnected }?.clearSessionQueue()
		}
	}

	override fun playAt(index: Int) {
		viewModelScope.launch {
			controller?.let { player ->
				if (index in 0 until player.mediaItemCount) {
					player.seekTo(index, 0L)
					player.play()
				}
			}
		}
	}

	override fun playCollectionLocal(collection: DomainSongCollection, startSong: DomainSong) {
		viewModelScope.launch {
			val (items, newCollection) = withContext(Dispatchers.Default) {
				val sortedCollection = if (collection is DomainAlbum) {
					collection.songs.sortedWith(compareBy({ it.discNumber }, { it.trackNumber }))
				} else {
					collection.songs
				}
				sortedCollection.map { it.toMediaItem() } to sortedCollection
			}

			val startIndex = newCollection.indexOfFirst { it.id == startSong.id }.coerceAtLeast(0)

			controller?.let { player ->
				player.setMediaItems(items, startIndex, 0L)
				player.prepare()
				player.play()
			}

			_uiState.update { state ->
				state.copy(
					queue = newCollection,
					currentIndex = startIndex,
					currentSong = newCollection.getOrNull(startIndex),
					// Stamp the collection we're playing FROM. It was previously only ever
					// derived asynchronously from the playing song's album, so at this moment
					// (when the queue is published to the hub) it was null or the previous
					// queue's — leaving shared history rows with no name.
					currentCollection = collection,
					// Fresh queue → saved-queue session (see SavedQueueRepository). Resolved here,
					// synchronously with the queue swap, so it's correct immediately — and resolved
					// rather than minted so replaying the same album refreshes its card.
					savedQueueId = sessionIdFor(newCollection),
					savedQueueKind = collection.toSavedQueueKind(),
					savedQueueName = collection.name
				)
			}
		}
	}

	override fun playNextSingleLocal(song: DomainSong) {
		viewModelScope.launch {
			controller?.addMediaItem(
				_uiState.value.currentIndex + 1,
				withContext(Dispatchers.Default) { song.toMediaItem() }
			)
			_uiState.update { state ->
				val newQueue =
					if (state.queue.isEmpty())
						state.queue + song
					else
						state.queue.slice(0..state.currentIndex) + song + state.queue.slice(state.currentIndex+1..<state.queue.size)
				state.copy(
					queue = newQueue,
					currentIndex = if (state.currentIndex == -1) 0 else state.currentIndex,
					currentSong = if (state.currentIndex == -1) song else state.currentSong
				)
			}
		}
	}

	override fun playNextLocal(collection: DomainSongCollection) {
		viewModelScope.launch {
			val (items, newCollection) = withContext(Dispatchers.Default) {
				val newCollection = if (collection is DomainAlbum) collection.songs.sortedWith(compareBy(
					{ it.discNumber },
					{ it.trackNumber }
				)) else collection.songs
				newCollection.map { it.toMediaItem() } to newCollection
			}
			controller?.addMediaItems(_uiState.value.currentIndex + 1, items)
			_uiState.update { state ->
				val newQueue = 
					if (state.queue.isEmpty()) 
						state.queue + newCollection
					else
						state.queue.slice(0..state.currentIndex) + newCollection + state.queue.slice(
							state.currentIndex+1..<state.queue.size
						)
				state.copy(
					queue = newQueue,
					currentIndex = if (state.currentIndex == -1) 0 else state.currentIndex,
					currentSong = if (state.currentIndex == -1) newCollection.firstOrNull() else state.currentSong
				)
			}
		}
	}

	override fun playRadio(radio: DomainRadio) {
		viewModelScope.launch {
			val radioId = "radio_${radio.name.hashCode()}"

			val dummyRadioSong = DomainSong(
				id = radioId,
				title = radio.name,
				artistName = "Live Radio",
				albumId = "radio_album",
				albumTitle = "Live Stream",
				duration = Duration.ZERO,
				trackNumber = 1,
				coverArtId = null,
				artistId = "",
				parentId = "",
				comment = null,
				discNumber = null,
				isrc = emptyList(),
				year = null,
				genre = null,
				genres = emptyList(),
				moods = emptyList(),
				bpm = null,
				contributors = emptyList(),
				playCount = 0,
				userRating = 0,
				averageRating = null,
				bitRate = null,
				bitDepth = null,
				sampleRate = null,
				audioChannelCount = null,
				replayGain = null,
				fileSize = 0,
				fileExtension = "",
				mimeType = "",
				filePath = radio.streamUrl,
				starredAt = null,
				musicBrainzId = null,
				explicitStatus = DomainExplicitStatus.Unknown
			)

			val metadata = MediaMetadata.Builder()
				.setTitle(radio.name)
				.setArtist("Live Radio")
				.setIsPlayable(true)
				.build()

			val mediaItem = MediaItem.Builder()
				.setUri(radio.streamUrl)
				.setMediaId("radio_${radio.name.hashCode()}")
				.setMediaMetadata(metadata)
				.setLiveConfiguration(MediaItem.LiveConfiguration.Builder().build())
				.build()

			controller?.let { player ->
				player.stop()
				player.clearMediaItems()
				player.setMediaItem(mediaItem)
				player.prepare()
				player.play()
			}

			_uiState.update { state ->
				state.copy(
					queue = listOf(dummyRadioSong),
					currentIndex = 0,
					currentSong = dummyRadioSong,
					isLoading = true,
					savedQueueId = sessionIdFor(listOf(dummyRadioSong)),
					savedQueueKind = SavedQueueSource.RADIO,
					savedQueueName = radio.name
				)
			}
		}
	}

	override fun shufflePlayLocal(collection: DomainSongCollection) {
		viewModelScope.launch {
			val (shuffledSongs, mediaItems) = withContext(Dispatchers.Default) {
				val songs = collection.songs.shuffled()
				songs to songs.map { it.toMediaItem() }
			}

			controller?.let { player ->
				player.shuffleModeEnabled = false
				player.setMediaItems(mediaItems, 0, 0L)
				player.prepare()
				player.play()
			}

			_uiState.update { state ->
				state.copy(
					queue = shuffledSongs,
					currentIndex = 0,
					currentSong = shuffledSongs.firstOrNull(),
					// See playCollectionLocal: the source collection must be known at publish
					// time, not resolved later from whatever song happens to be playing.
					currentCollection = collection,
					savedQueueId = sessionIdFor(shuffledSongs),
					savedQueueKind = collection.toSavedQueueKind(),
					savedQueueName = collection.name
				)
			}
		}
	}

	override fun pause() {
		viewModelScope.launch(Dispatchers.Main.immediate) {
			controller?.pause()
		}
	}

	override fun resume() {
		viewModelScope.launch(Dispatchers.Main.immediate) {
			controller?.play()
		}
	}

	override fun next() {
		viewModelScope.launch(Dispatchers.Main.immediate) {
			if (controller?.hasNextMediaItem() == true) controller?.seekToNextMediaItem()
		}
	}

	override fun previous() {
		viewModelScope.launch(Dispatchers.Main.immediate) {
			val controller = controller ?: return@launch
			if (controller.hasPreviousMediaItem() && controller.currentPosition <= 1000) {
				controller.seekToPreviousMediaItem()
			} else {
				controller.seekTo(0)
			}
		}
	}

	override fun toggleShuffle() {
		viewModelScope.launch {
			controller?.let { player ->
				player.shuffleModeEnabled = !player.shuffleModeEnabled
			}
		}
	}

	override fun toggleRepeat() {
		viewModelScope.launch {
			controller?.let { player ->
				player.repeatMode = when (player.repeatMode) {
					Player.REPEAT_MODE_OFF -> Player.REPEAT_MODE_ALL
					Player.REPEAT_MODE_ALL -> Player.REPEAT_MODE_ONE
					else -> Player.REPEAT_MODE_OFF
				}
			}
		}
	}

	override fun seek(normalized: Float) {
		// When another device is active, the scrubber must drive THAT device via
		// the hub, not the silent local player (whose progress isn't even shown).
		routeRemotely?.let { router ->
			val durationMs = uiState.value.currentSong?.duration?.inWholeMilliseconds ?: 0L
			router.seek((normalized.coerceIn(0f, 1f) * durationMs).toLong())
			return
		}
		viewModelScope.launch(Dispatchers.Main.immediate) {
			controller?.let {
				val target = (it.duration * normalized).toLong()
				it.seekTo(target)
				_uiState.update { state ->
					state.copy(progress = normalized)
				}
			}
		}
	}

	override fun onCleared() {
		viewModelScope.launch {
			super.onCleared()
			controllerFuture?.let { MediaController.releaseFuture(it) }
		}
	}

	override fun setPlaybackSpeed(value: Float) {
		viewModelScope.launch {
			controller?.setPlaybackSpeed(value)
		}
		_uiState.update { it.copy(playbackSpeed = value) }
	}

	private fun DomainSong.toMediaItem(): MediaItem {
		val metadata = MediaMetadata.Builder()
			.setTitle(title)
			.setArtist(artistName)
			.setAlbumTitle(albumTitle)
			.setArtworkUri(
				coverArtId?.let { sessionManager.getCoverArtUrl(it).toUri() }
			)
			.build()

		val uri = when {
			id.startsWith("radio_") && !filePath.isNullOrEmpty() -> {
				filePath.toUri()
			}

			else -> {
				val localPath = downloadManager.getDownloadedFilePath(id).takeIf { useDownloadFor(it) }
				if (localPath != null) {
					File(localPath).toUri()
				} else {
					getStreamUrl(id)
				}
			}
		}

		val builder = MediaItem.Builder()
			.setUri(uri)
			.setMediaId(id)
			.setMediaMetadata(metadata)

		// The Cast MediaItemConverter requires a MIME type (it throws without
		// one); ExoPlayer also benefits for container sniffing.
		if (mimeType.isNotBlank()) {
			builder.setMimeType(mimeType)
		}

		if (id.startsWith("radio_")) {
			builder.setLiveConfiguration(MediaItem.LiveConfiguration.Builder().build())
		}

		return builder.build()
	}
}
