package paige.navic.domain.manager

import paige.navic.domain.manager.base.BasePreferenceManager
import paige.navic.domain.models.settings.AnimationStyle
import paige.navic.domain.models.settings.AutoplayMode
import paige.navic.domain.models.settings.BottomBarCollapseMode
import paige.navic.domain.models.settings.BottomBarVisibilityMode
import paige.navic.domain.models.settings.CoverArtQuality
import paige.navic.domain.models.settings.CoverArtShape
import paige.navic.domain.models.settings.FontOption
import paige.navic.domain.models.settings.GridSize
import paige.navic.domain.models.settings.MarqueeSpeed
import paige.navic.domain.models.settings.MoodCharacter
import paige.navic.domain.models.settings.MiniPlayerProgressStyle
import paige.navic.domain.models.settings.MiniPlayerStyle
import paige.navic.domain.models.settings.NavigationBarLabelVisibility
import paige.navic.domain.models.settings.NavigationBarStyle
import paige.navic.domain.models.settings.NowPlayingBackgroundStyle
import paige.navic.domain.models.settings.NowPlayingSliderStyle
import paige.navic.domain.models.settings.OfflineMode
import paige.navic.domain.models.settings.ReplayGainMode
import paige.navic.domain.models.settings.StreamingQuality
import paige.navic.domain.models.settings.Theme
import paige.navic.domain.models.settings.ThemeMode
import paige.navic.domain.models.settings.ToolbarPosition
import com.russhwolf.settings.Settings as KmpSettings

class PreferenceManager(
	settings: KmpSettings
) : BasePreferenceManager(settings) {
	var font by preference(FontOption.GoogleSans)
	var fontPath by preference("")
	var animationStyle by preference(AnimationStyle.Expressive)
	var nowPlayingBackgroundStyle by preference(NowPlayingBackgroundStyle.Dynamic)
	// navi-connect: real backdrop blur (Haze) for the Mood Flow frosted UI.
	// Experimental (alpha dependency) → off by default.
	var expressiveBlur by preference(false)
	var swipeToSkip by preference(true)
	var gridSize by preference(GridSize.TwoByTwo)
	var coverArtShape by preference(CoverArtShape.Soft)
	var coverArtQuality by preference(CoverArtQuality.High)
	var artGridItemSize by preference(150f)
	var marqueeSpeed by preference(MarqueeSpeed.Slow)
	var alphabeticalScroll by preference(false)
	var lyricsAutoscroll by preference(true)
	var lyricsBeatByBeat by preference(true)
	var lyricsKeepAlive by preference(true)
	var lyricsBlur by preference(false)
	var lyricsBrightInactive by preference(false)
	var enableScrobbling by preference(true)
	var scrobblePercentage by preference(.5f)
	var minDurationToScrobble by preference(30f)
	var replayGainMode by preference(ReplayGainMode.Off)
	var gaplessPlayback by preference(true)
	var audioOffload by preference(false)
	var streamingQualityWifi by preference(StreamingQuality.Lossless)
	var streamingQualityCellular by preference(StreamingQuality.Lossless)
	var isAdvancedTranscodingActive by preference(false)
	var customMaxBitrateWifi by preference(0)
	var customMaxBitrateCellular by preference(0)
	// On a metered connection, play the downloaded copy or stream the server's original?
	// Default true (play the download) — that's the point of downloading, and it costs no data.
	// Turn it off when the downloads are space-saving transcodes but you'd rather hear the
	// original away from Wi-Fi. On an unmetered connection a download is always preferred.
	var preferDownloadsOnCellular by preference(true)
	// Downloads are kept, so they get their OWN quality rather than inheriting whatever the
	// current network's streaming setting happens to be. Original by default: an offline copy you
	// keep is the last place to silently save bandwidth.
	//
	// Bitrate and container are separate, and use the same value model as a per-playlist download
	// policy (PlaylistDownloadDialog): bitrate 0 = the server's original file (so FLAC stays FLAC),
	// otherwise kbps; format "" = original container, else an explicit transcode target. The old
	// [downloadQuality] tier couldn't express "320 kbps mp3" at all — its tiers stopped at 192 and
	// always forced opus on Android.
	var downloadBitrate by preference(0)
	var downloadFormat by preference("")
	// Legacy: superseded by downloadBitrate/downloadFormat, read once by [migrateDownloadQuality]
	// so an existing choice carries over instead of silently resetting to Original.
	var downloadQuality by preference(StreamingQuality.Lossless)
	var customMaxBitrateDownload by preference(0)
	private var downloadQualityMigrated by preference(false)

	/**
	 * Carry a pre-existing download-quality tier over to the bitrate/format pair. Runs once; a user
	 * who never changed the default (Lossless → 0/"") is unaffected either way.
	 */
	private fun migrateDownloadQuality() {
		if (downloadQualityMigrated) return
		downloadQualityMigrated = true
		val legacy = downloadQuality
		if (legacy == StreamingQuality.Lossless) return
		val custom = customMaxBitrateDownload
		downloadBitrate = if (custom > 0) custom else legacy.bitrateAndroid
		downloadFormat = legacy.containerAndroid.orEmpty()
	}

	init {
		migrateDownloadQuality()
	}
	// Download constraints (Symfonium-style). Wi-Fi-only pauses transfers on a metered/cellular
	// connection; charging-only pauses them off charger. Max-concurrency caps simultaneous
	// transfers (the download semaphore's permit count); coerced into [1, 10] where read.
	var downloadWifiOnly by preference(false)
	var downloadChargingOnly by preference(false)
	var downloadMaxConcurrency by preference(4)
	var nowPlayingToolbarPosition by preference(ToolbarPosition.Bottom)
	var nowPlayingSongInfo by preference(true)
	var nowPlayingSliderStyle by preference(NowPlayingSliderStyle.Squiggly)
	var customHeaders by preference("")
	var checkForUpdates by preference(true)

	// navigation bar settings
	var bottomBarCollapseMode by preference(BottomBarCollapseMode.OnScroll)
	var bottomBarVisibilityMode by preference(BottomBarVisibilityMode.AllScreens)
	var navigationBarStyle by preference(NavigationBarStyle.Floating)
	var navigationBarLabelVisibility by preference(
        NavigationBarLabelVisibility.Always
    )
	var miniPlayerStyle by preference(MiniPlayerStyle.Detached)
	var miniPlayerProgressStyle by preference(MiniPlayerProgressStyle.Seekable)

	/**
	 * If we have informed the user (on Android) about
	 * Google locking down sideloading.
	 */
	var showedSideloadingWarning by preference(false)

	// theme related settings
	var theme by preference(Theme.Dynamic)
	var themeMode by preference(ThemeMode.System)
	var accentColourH by preference(0f)
	var accentColourS by preference(0f)
	var accentColourV by preference(1f)

	// sync related settings
	var lastFullSyncTime by preference(0L)

	// navi-connect: continuous autoplay (Tier 1 = Similar). See AutoplayMode.
	var autoplayMode by preference(AutoplayMode.Off)

	// navi-connect Tier 2: AudioMuse-AI core API (direct HTTP, Bearer token).
	// Unlocks Sonic Fingerprint autoplay (+ later: adaptive/chat/mood search).
	var audioMuseUrl by preference("")
	var audioMuseToken by preference("")

	// navi-connect Tier 2: adaptive "Mood Flow" tuning preset. See MoodCharacter.
	var moodCharacter by preference(MoodCharacter.SteadyVibes)

	// navi-connect hub (Spotify-Connect-style remote control)
	var hubEnabled by preference(false)
	var hubUrl by preference("ws://192.168.1.10:4790")
	var hubToken by preference("")
	var hubDeviceName by preference("Navic")
	var hubDeviceId by preference("")
	// Comma-joined device ids the user manually hid from the device picker.
	var hubHiddenDeviceIds by preference("")
	// Saved-queue deletions we haven't been able to tell the hub about yet, as "id:millis"
	// pairs joined by commas. Deleting history while offline used to be silently undone: the
	// row went from Room, then the next reconnect adopted the hub's list — which still had it.
	// Mirrors hub.py's `deleted_saved_queues`. See SavedQueueRepository.
	var deletedSavedQueueIds by preference("")

	fun customHeadersMap(): Map<String, String> = buildMap {
		for (line in customHeaders.lines()) {
			val parts = line.split(":", limit = 2)
			if (parts.size < 2) continue

			val rawKey = parts[0]
			val rawValue = parts[1]

			val key = rawKey.trim()
			val value = rawValue.trim()
			if (key.isNotEmpty() && value.isNotEmpty()) put(key, value)
		}
	}

	var offlineMode by preference(OfflineMode.Auto)
}
