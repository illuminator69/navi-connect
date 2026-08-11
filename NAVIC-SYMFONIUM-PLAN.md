# Navic Symfonium-Style Plan: Reliability, Queue, Offline, and Frontend Polish

## Summary

Make Navic feel more like Symfonium by tightening three foundations first:
reliable library sync, full queue control across local and remote sessions, and
trustworthy offline downloads. Frontend polish should move in parallel: shared
song-row styling, stronger adaptive color on library/detail pages, and more
consistent album-art accent extraction.

The now-playing queue-card idea makes sense, with one caveat: queue rows should
not derive their card background from each row's cover art. Mixed queues would
quickly become visually noisy. Instead, queue rows should use one shared ambient
surface from the current queue/now-playing context while each row keeps its own
cover art image.

Assumption: Navic is the primary target. Hub changes happen only where remote
queue/session behavior requires them.

## Key Changes

### Shared Song Row Visual System

- Create one reusable row style for album tracks, queue tracks, song lists, and
  search results.
- Code areas: `CollectionDetailScreenSongRow`, `QueueScreenItem`, `SongRow`.
- Queue rows should visually match album-page rows more closely: segmented
  grouping, similar padding, duration/waveform/download states, clear
  selected/current styling, and stable drag handle placement.
- Queue rows should use the queue/now-playing ambient theme, not per-row
  extracted colors.

### Queue Management

- Add remote queue remove and clear actions.
- Code areas: `HubManager`, `hub.py`, `QueueScreen`, `MediaPlayerViewModel`.
- Fix the current issue where queue clear always calls local
  `player.clearQueue()` even while viewing a remote queue.
- Keep duplicate queue entries keyed by slot id, not song id.
- Preserve current song identity when reordering or removing items before the
  current index.

### Library Adaptive Color

- Replace or strengthen the current flat `LibraryHeroAmbient` with a capped
  blurred-cover wash or stronger cover-color wash keyed to now-playing.
- Code areas: `LibraryScreen`, `LibraryScreenContent`, `CoverColorScheme`,
  `BlendBackground`.
- Keep the effect capped and faded to neutral before album grids begin so
  different album covers do not fight the page tint.
- Add subtle adaptive tint to overview buttons and section headers, while
  keeping light/dark brightness stable.

### Album and Detail Adaptive Color

- Replace flat `coverAmbientGradient` backgrounds on album and artist detail
  with the richer `BlendBackground` treatment where appropriate.
- Code areas: `CollectionDetailScreen`, `ArtistDetailScreen`, `HeadingRow`,
  `BlendBackground`, `CoverColorScheme`.
- Improve palette selection so accent color can choose a vivid/warm swatch, not
  only the dominant color. This should help covers like the Revengeseekerz
  screenshot pick orange instead of near-black/green.
- Unify now-playing sheet and detail pages on the same
  `rememberCoverColorScheme` helper.

### Palette and Performance Bugs

- `NowPlayingScene.colorSchemeForCurrentSong()` duplicates palette logic and may
  disagree with `CoverColorScheme`; remove the duplicate path.
- `CoverColorScheme` and `NowPlayingScene` create `HttpClient` inline in
  composable code; move to remembered/shared loader/client setup.
- `CollectionDetailScreen` comments mention `BlendBackground` even though the
  screen still paints a flat gradient; align code and comments.
- `SongRow` casts `song.albumId as String` in "View album"; remote placeholder
  songs or malformed metadata can crash here. Make album navigation nullable-safe.

### Library and Navigation Reliability

- Keep the sync guard: do not run obsolete album/song cleanup when any album
  fetch failed.
- Code areas: `DbRepository`, `AlbumDao`, `SongDao`.
- Move large in-memory song sorting toward DAO queries.
- Code areas: `SongRepository`, `SortUtils`, `SongDao`, `AlbumDao`.

### Offline Downloads

- Build a real download center with active, queued, failed, retry, cancel, and
  completed sections.
- Code areas: `DownloadManager`, `DownloadDao`, `DownloadEntity`,
  `SettingsDataStorageScreen`, `PlaylistDownloadManager`,
  `PlaylistDownloadDialog`.
- Store quality, format, file size, error, retry count, source policy, and
  timestamps.
- Write downloads to temp files first, then atomically finalize.
- Make large library/playlist downloads durable with Android background or
  foreground work.

### Cast and Session Reliability

- Harden native Cast restart/re-adoption.
- Code areas: `AndroidCastManager`, `CastManager`, `PlaybackService`,
  `MediaPlayer.android.kt`, `DevicePickerSheet`, `HubManager`.
- Reopening Navic while the TV continues playing should either reattach cleanly
  or show a recoverable state.
- Transfer back from Cast must preserve queue, index, and position.

## Later Quality-of-Life Features

### Queue History

- Auto-save replaced/completed queues with source, timestamp, track ids, index,
  and position.
- Add restore queue, resume queue, and save as playlist.
- Store generated sessions separately from ordinary queues: radio, Mood Flow,
  Journey, album, playlist, and manual queue sessions.

### Saved Queues

- Let users manually save the current queue as a local preset.
- Later option: export saved queues to Navidrome playlists.

### Queue Undo

- Add undo for clear queue, remove row, move row, and play-now queue replacement.
- Keep undo short-lived and scoped to the current session.

### Download QoL

- Download current queue.
- Download next N songs.
- Keep playlist offline with a storage budget.
- Wi-Fi-only, charging-only, max concurrency, and auto-clean unused downloads.
- Repair missing files and retry failed downloads.
- Show per-policy ownership so users can tell whether a file came from manual,
  album, playlist, rolling cache, or whole-library download.

### Library QoL

- Continue listening row from queue/session history.
- Recently added songs, not only albums.
- Downloaded-only artist/album/playlist filters.
- Alphabet jump list for large libraries.
- Better duplicate album/song grouping.
- Better empty/error/stale states: "showing cached library, sync failed" instead
  of blank or scary error pages.

## Test Plan

- Build Navic Android after palette/helper changes.
- Add hub tests for remove, clear, move, paused transfer, stale reports, and
  release timeout.
- Manual UI checks:
  - Mixed-cover queue should look cohesive, not multicolor-per-card.
  - Album rows and queue rows should feel like the same component family.
  - Library home should show visible but restrained now-playing color.
  - Warm album covers should produce warm/orange accents when appropriate.
  - No text should lose contrast over dark blue, orange, or light album art.
- Manual behavior checks:
  - Clear/remove queue locally and remotely.
  - Open "View album" from local, remote, and placeholder songs without crash.
  - Sync with forced album fetch failure and verify cached library remains.
  - Start/cancel/retry large downloads.
  - Cast to TV, kill/reopen Navic, transfer back, verify queue index/position.

## Assumptions and Defaults

- Use one ambient color per screen/surface, not per row.
- Keep library home brightness stable; only hues adapt.
- Prioritize Navic first; hub changes only where remote queue/session behavior
  needs them.
- Symfonium is the reference for polish and behavior, but Navic should keep its
  current Compose/Material expressive identity.
