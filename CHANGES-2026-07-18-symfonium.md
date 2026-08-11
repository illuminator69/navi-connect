# Navic — Symfonium Plan: Remaining Work (2026-07-18)

Finishing pass over `NAVIC-SYMFONIUM-PLAN.md`. Phase 0 audit confirmed most of the plan was
already built; this session implemented the genuinely-remaining pieces. All changes are in
`navic/composeApp`, edited in place (the repo's `.git` is empty — nothing committed; build in
Android Studio).

> **Not compiled in this environment** (no JDK/Android SDK). Verified structurally: brace-depth
> zero on every edited file, all new `Res.string.*` names cross-checked defined-vs-referenced,
> DI / expect-actual / iOS parity checked by hand. Expect one or two compile-fix rounds.

---

## Phase 0 — Audit result

Confirmed **already present** (not rebuilt): shared SongRow system, remote queue edit
(clear/remove/move routed to hub), download center (retry/repair/cancel), saved-queue infra
(auto-save + resume-at-position), palette de-dup, SongRow `albumId` null-safety, atomic downloads
(`.part` → finalize), sync guard (skips obsolete cleanup when any album fetch failed), DAO-side
**album** sorting.

**Remaining and implemented this session:** items 1–5 below.

---

## 1. Queue Undo *(was missing)*

Short-lived (~6 s), session-scoped, in-memory undo for: **clear**, **remove row**, **move row**,
and **play-now queue replacement**. Restores to whichever session is active (local or remote).

- `shared/MediaPlayer.kt`
  - New `QueueUndoSnapshot` / `QueueUndoKind`, an undo deque, and `queueUndo: StateFlow`.
  - `captureQueueUndo(kind)` snapshots the active session's queue/index/position before a
    destructive edit; `performQueueUndo()` restores it; `dismissQueueUndo()`; auto-expires at 6 s.
  - `captureQueueUndo(REPLACE)` added to `playCollection` / `shufflePlay`.
  - `RemotePlaybackRouter` gained `restoreQueue(songs, index, positionMs, play)`.
- `domain/manager/HubManager.kt`
  - Implements `restoreQueue` → `loadSessionQueue(...)`, which now accepts `positionMs` + `play`
    (previously hardcoded to `0` / `true`). The hub already handles `setQueue` with those fields.
- `ui/screens/queue/QueueScreen.kt`
  - `captureQueueUndo(...)` at the clear / remove / move-commit / related-tab play-now sites.
  - A `SnackbarHost` (bottom) driven by `queueUndo`, with an **Undo** action.
- `values/strings.xml`: `action_undo`, `undo_queue_cleared` / `_removed` / `_moved` / `_replaced`.

## 2. Queue history session typing *(was partial)*

Saved queues now record **how they were created** so the list can group generated sessions.

- `data/database/entities/SavedQueueEntity.kt`: added `sourceKind` (default `"manual"`) and a
  cached `coverArtId`.
- `domain/models/SavedQueueSource.kt` *(new)*: string constants
  `manual / album / playlist / radio / moodFlow / journey` + `DomainSongCollection.toSavedQueueKind()`.
- `data/database/DownloadMigrations.kt`: `MIGRATION_CACHE_17_18` adds both columns (additive,
  idempotent). Folded together since v18 hadn't shipped.
- `data/database/CacheDatabase.kt`: version **17 → 18**.
- `di/PlatformModule.android.kt` + `di/PlatformModule.ios.kt`: register `MIGRATION_CACHE_17_18`.
- `ui/core/PlayerUiState.kt`: added `savedQueueKind`.
- Kind stamped at mint sites:
  - `androidMain/.../MediaPlayer.android.kt`: `playCollectionLocal` / `shufflePlayLocal` derive from
    the collection; `playRadio` → `radio`. `loadRemoteQueue` now takes optional `savedQueueId` +
    `savedQueueKind` (default `null`/`manual` keeps hub-mirror queues transient).
  - `domain/manager/RadioManager.kt`: `playMix(kind)` — `startRadio`→radio, `startJourney`→journey,
    `playMoodMix`→moodFlow; local mixes now persist as saved-queue sessions.
- `domain/repositories/SavedQueueRepository.kt`: writes `sourceKind` + `coverArtId`.
- `data/database/dao/SavedQueueDao.kt`: `updateProgress` also refreshes `coverArtId`.
- `shared/MediaPlayer.kt`: `swapToSavedQueue(id, play)` carries id **and** kind through the
  restore (fixes a latent bug where the history-row id was lost) and gains a `play` flag
  (restore-paused vs resume-playing). Added public `newQueueSessionId()`.
- UI:
  - `ui/screens/savedqueues/SavedQueuesScreen.kt`: **kind filter chips**, per-row kind label, and
    row actions **restore / resume / save-as-Navidrome-playlist**; snackbar feedback.
  - `ui/screens/savedqueues/viewmodels/SavedQueuesViewModel.kt`: `saveAsPlaylist(id, name)` via
    `sessionManager.api.createPlaylist` + local cache; `SavedQueueMessage` feedback.
- `values/strings.xml`: `action_restore_queue`, `action_resume_queue`, `action_save_as_playlist`,
  `title_save_as_playlist`, `option_playlist_name`, `message_saved_as_playlist`,
  `message_save_playlist_failed`, `filter_all`, `queue_kind_*`.

## 3. Download QoL *(was partial)*

Added Wi-Fi-only / charging-only constraints, configurable concurrency, and download-next-N.
(Repair, retry-failed, download-current-queue already existed.)

- `domain/manager/ConnectivityManager.kt` (+ `.android.kt` / `.ios.kt`): new
  `isCharging: StateFlow<Boolean>` — Android battery-status receiver; iOS stub `true`
  (out of scope, keeps it compiling).
- `domain/manager/PreferenceManager.kt`: `downloadWifiOnly`, `downloadChargingOnly`,
  `downloadMaxConcurrency` (default 4).
- `domain/manager/DownloadManager.kt`:
  - Constructor takes `ConnectivityManager`.
  - Semaphore permit count = `downloadMaxConcurrency` (coerced 1..10; applies on next app start).
  - `awaitDownloadConstraints()` holds a QUEUED row until Wi-Fi/charging allow it, *before*
    taking a permit; `downloadsConstrained: StateFlow` surfaces the paused state.
  - `downloadNextSongs(queue, fromIndex, count)` — the "download upcoming" entry point.
- `ui/screens/settings/viewmodels/DownloadCenterViewModel.kt`: `DownloadSettings` + setters,
  `constrained` flow (takes `PreferenceManager`).
- `ui/screens/settings/DownloadCenterScreen.kt`: settings section (two toggles + concurrency
  stepper) and a "waiting for Wi-Fi/charging" banner.
- `ui/screens/queue/QueueScreen.kt`: a "Download next 10" overflow next to the queue download
  button.
- `values/strings.xml`: `section_download_settings`, `setting_download_wifi_only(+_desc)`,
  `setting_download_charging_only(+_desc)`, `setting_download_concurrency(+_desc)`,
  `banner_downloads_waiting`, `action_download_next`.

## 4. Library QoL *(partial — highest-value, self-contained items delivered)*

- **Continue Listening** row on the library home, from saved-queue history; tapping resumes at
  the saved playhead.
  - `ui/screens/library/components/ContinueListeningCard.kt` *(new)*.
  - `ui/screens/library/components/Content.kt`: `continueListening` + `onResumeQueue` params;
    a `horizontalSection` above the album rows (uses cached `coverArtId`).
  - `ui/screens/library/LibraryScreen.kt`: observes `SavedQueueRepository.observeAll()`, excludes
    the live queue, caps at 10; resume via `swapToSavedQueue(id, play = true)`.
- **Stale/cached-library banner** — "showing cached library — sync failed".
  - `domain/manager/SyncManager.kt`: `SyncState.lastSyncFailed`, set from the top-level
    `syncEverything` result.
  - `ui/screens/library/**`: a full-span banner item shown when the last sync failed.
- `values/strings.xml`: `title_continue_listening`, `banner_sync_failed_cached`.

**Deferred** (need a compiler in the loop / broader surface): alphabet fast-scroll jump list;
recently-added **songs** row (no local added-date column — needs schema or network newest fetch);
downloaded-only filters on artist/album/playlist lists.

## 5. DAO-side song sorting *(finding: no change needed)*

Song list sorting is already fully DAO-side via `SortUtils.toSongSqlQuery` — every
`DomainSongListType` emits an `ORDER BY`, used by `SongRepository.getLocalData`. The only
remaining in-memory sort (`SongRepository.getDownloadedSongs`) is a required cross-database
chunk-merge (downloads.db vs cache.db, chunked under SQLite's bind limit) and cannot collapse to a
single DAO query. `getAllSongs()` is unsorted but only feeds whole-library download (order
irrelevant).

---

## Files changed

**Common (`commonMain`)**
- `shared/MediaPlayer.kt`
- `domain/manager/HubManager.kt`, `RadioManager.kt`, `DownloadManager.kt`,
  `PreferenceManager.kt`, `SyncManager.kt`, `ConnectivityManager.kt`
- `domain/repositories/SavedQueueRepository.kt`
- `domain/models/SavedQueueSource.kt` *(new)*
- `data/database/entities/SavedQueueEntity.kt`, `data/database/dao/SavedQueueDao.kt`,
  `data/database/DownloadMigrations.kt`, `data/database/CacheDatabase.kt`
- `ui/core/PlayerUiState.kt`
- `ui/screens/queue/QueueScreen.kt`
- `ui/screens/savedqueues/SavedQueuesScreen.kt` + `viewmodels/SavedQueuesViewModel.kt`
- `ui/screens/settings/DownloadCenterScreen.kt` + `viewmodels/DownloadCenterViewModel.kt`
- `ui/screens/library/LibraryScreen.kt`, `components/Content.kt`,
  `components/ContinueListeningCard.kt` *(new)*
- `composeResources/values/strings.xml`

**Android (`androidMain`)**
- `shared/MediaPlayer.android.kt`
- `domain/manager/ConnectivityManager.android.kt`
- `di/PlatformModule.android.kt`

**iOS (`iosMain`)** — parity only (keeps compiling)
- `domain/manager/ConnectivityManager.ios.kt`
- `di/PlatformModule.ios.kt`

## Verify (from the plan's Test Plan)

- Clear / remove / move / play-now → **Undo** works, locally and remotely.
- Saved queue: **restore** (paused) vs **resume** (playing at saved position); **save as playlist**
  creates a Navidrome playlist; kind filter chips group generated sessions.
- Large download with **Wi-Fi-only** on: pauses on mobile data, resumes on Wi-Fi; **charging-only**
  gates off charger; concurrency stepper changes parallelism (after restart); **download next 10**.
- Library home: **Continue Listening** row resumes a recent queue; forced sync failure shows the
  **cached-library banner**, not a blank page.
