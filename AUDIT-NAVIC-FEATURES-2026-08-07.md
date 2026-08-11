# Navic feature audit — what's missing for a full-fledged music player (2026-08-07)

Measures Navic against Spotify / Symfonium as a general-purpose player, rather than against the
navi-connect remote-control feature set (which is well ahead). Companion to `AUDIT-NAVIC-2026-07-20.md`
(correctness) — this one is about **capability gaps**.

Paths are relative to `navic/composeApp/src/commonMain/kotlin/paige/navic/` unless marked
`androidMain` / `androidApp`.

§5 records the saved-queue defects found in the same pass; those were **fixed** — see §5 for what
landed. Everything in §1–§4 is backlog.

---

## 1. Offline / download robustness

The download system is feature-rich at the policy layer and fragile at the transport layer.

| Gap | Where it stands today | Impact |
|---|---|---|
| **Downloads die with the process** | `DownloadManager` (`domain/manager/DownloadManager.kt`) runs on its own `CoroutineScope(Dispatchers.IO)`. No `WorkManager`, no foreground service. `reconcileInterruptedDownloads()` marks everything QUEUED/DOWNLOADING as `FAILED("Interrupted")` at next launch. | **High.** Backgrounding the app during a large album download loses it. This is the single biggest gap vs Symfonium. |
| **No range resume** | `.part` file is deleted on failure (`executeDownloadProcess`), not resumed. | High — pairs with the above: every interruption restarts from byte 0. |
| **No auto-retry** | `MAX_DOWNLOAD_RETRIES = 3` is enforced only by `PlaylistDownloadManager`'s 6-hour sync. Manual `retryDownload` is unbounded; nothing retries on connectivity regain. | Medium. A tunnel drops a track and it sits FAILED until noticed. |
| **No streaming cache** | No `SimpleCache`/`CacheDataSource` anywhere. `PlaybackService` uses a plain `DefaultDataSource.Factory` (`androidMain/shared/MediaPlayer.android.kt`). | Medium-high. Replaying a streamed track always re-downloads it — Spotify/Symfonium both cache-what-you-play. |
| **No storage cap / LRU** | `DownloadDao.getTotalSize()` is reported in `ui/screens/settings/DataStorageScreen.kt` and never acted on. Rolling eviction exists **only** inside a playlist policy. | Medium. Downloads grow until the disk complains. |
| **Concurrency setting needs a restart** | `Semaphore(downloadMaxConcurrency…)` permits are fixed at construction (documented at `DownloadManager.kt:75`). `downloadEntireLibrary` separately hardcodes 10 workers, ignoring the preference entirely. | Low-medium, but it reads as a bug. |
| **One-directional orphan repair** | `isDownloadFilePresent()` + `DownloadCenterViewModel.repairMissing()` fix rows-without-files. Nothing ever scans for **files-without-rows** or stale `.part` files. `DownloadDatabase` has `fallbackToDestructiveMigration(true)`. | Medium. One destructive migration silently leaks every downloaded file, unreachable and undeletable short of clearing app data. |
| **Downloadable scope is narrow** | Song, album, playlist, queue-slice, whole library. No artist-level download (`ArtistDetailScreen` loops per album), no favorites/starred download, no genre or smart-filter download. | Medium. "Download everything by this artist" is a table-stakes action. |
| **Auto-download only for playlists** | `PlaylistDownloadManager` policies (permanent / rolling-N / byte budget) are persisted as a **`Settings` JSON blob**, not Room. Sync is a fixed 30 s-then-6 h timer — no sync on foreground, on network regain, or on playlist change, and no UI listing existing policies. | Medium. Symfonium auto-syncs albums and artists too, and reacts to changes. |
| `downloadCollection()` mislabels playlists | Tags them `DownloadSource.ALBUM` (`DownloadManager.kt:314`), so the Download Center groups them wrongly. | Low — one-line fix. |
| **No per-download progress notification** | Nothing surfaces download progress outside the Download Center screen. | Low-medium. |

**Suggested order:** foreground service + `WorkManager` → range resume → streaming `SimpleCache` →
storage cap/LRU → orphan sweep → artist/favorites download.

---

## 2. Offline UX & file management

The *data* is there; the *mode* isn't. This is the cheapest section to make a big difference in.

- **`OfflineMode.Forced` doesn't actually make the app offline.** `domain/models/settings/OfflineMode.kt`
  (`Auto | Forced | NoWiFi`) is consumed in exactly one place — `ConnectivityManager.android.kt:95` —
  where it flips `isOnline` to false. The library still browses everything cached; non-downloaded rows
  are merely greyed out via `MediaPlayer.isAvailable()`. There is no "show only what I can play" filter.
- **No quick offline toggle.** It lives in Settings → Data & Storage. Symfonium puts it one tap away.
- **"Downloaded only" is a sort option, not a mode.** `DomainSongListType.Downloaded`,
  `DomainAlbumListType.Downloaded`, `DomainArtistListType.Downloaded`, `DomainPlaylistListType.Downloaded`
  must each be re-selected per list, and **no genre equivalent exists**.
- **No storage-usage breakdown.** `DataStorageScreen.kt` shows two aggregate lines (count + total size).
  No by-album/artist/playlist view, and no per-download delete UI — deletion is all-or-nothing
  (`clearAllDownloads`) or via a playlist policy.
- **Files are opaque and unreachable.** `StorageManager.android.kt` writes
  `context.filesDir/downloads/<songId>.<ext>` — flat, untagged, app-private. No folder tree, no tag
  writing, no MediaStore export, no SD-card / external / SAF destination picker. Uninstalling loses
  everything with no export path.
- **Downloaded artwork isn't guaranteed offline.** `cacheCoverArt`/`cacheAlbumCoverArt` pre-warm Coil's
  disk cache (2 GB LRU, `di/SingletonImageLoaderInit.kt`) — shared with all browsing artwork and freely
  evictable. There's no companion artwork file per download. Lyrics *are* stored properly (`LyricEntity`).
- **No partial-download indicator** on an album or playlist — `getCollectionDownloadStatus` collapses to
  one of four states.
- **The offline write queue is narrow.** `SyncActionEntity` covers `STAR/UNSTAR/DELETE_PLAYLIST/SCROBBLE/
  STAR_0..5` (drained by `SyncManager.processQueue`). No offline play-count accumulation, no offline
  playlist edits (add/remove track), no offline playlist creation.

**Suggested order:** make `Forced` filter the library → global downloaded-only toggle + quick access →
storage-usage screen with per-item delete → SAF/external destination → offline playlist edits.

---

## 3. Playback engine & library

### Playback
Present: gapless preference, audio offload, ReplayGain (`util/core/ReplayGainUtils.kt`, applied as a
volume scalar), playback speed (0.5–2.0), sleep timer (`domain/manager/SleepTimerManager.kt`), audio
focus/ducking, becoming-noisy, Media3 media-button handling, per-network streaming quality.

**Not present:** crossfade · skip-silence (`skipSilenceEnabled` never set) · **any equalizer or DSP**
(no `AudioEffect`/`Equalizer`/`LoudnessEnhancer`/`DynamicsProcessing`, and no system-EQ launch intent —
this is the most-requested single feature in players of this class) · local output-device routing ·
user-tunable buffering (hardcoded 32/64 s in `PlaybackService.onCreate`) · custom headset-button mapping ·
pitch control · sleep-timer "end of track/queue" (duration only).

### Queue
Present: shuffle, repeat off/all/one, play-next / add-to-queue, drag reorder, remove, clear, a real undo
stack (`QueueUndoSnapshot`, 10 deep / 6 s, `shared/MediaPlayer.kt`), persistence across restarts, an
"Up next" tab, and autoplay/endless queue via `RadioManager`.

**Not present:** **play history** (the Queue screen's second tab is "Related", not history — there is no
recently-played track list anywhere) · stop-after-current-track · A–B repeat · shuffle-albums /
smart-shuffle · multi-select queue editing · remove-duplicates (only a warning dialog on add).

### Library
Present: albums / artists / songs / genres / playlists / radios / starred / shares / search screens,
a broad sort vocabulary (`util/core/SortUtils.kt`), server search with automatic local fallback
(`SearchRepository.performLocalSearch`), alphabet fast-scroll (`ui/components/common/AlphabeticalScroller.kt`,
on albums/artists/playlists), multi-disc grouping, artist bios and similar artists, configurable nav tabs.

**Not present:** folder / file-tree browsing (`getIndexes`/`getMusicDirectory` unused — a common
self-hosted expectation) · grid⇄list toggle per screen · **album-artist browsing and compilation
handling** (album artists are fetched and synced by `DbRepository`, and `albumartist` is a smart-playlist
field, but songs carry a single `artistId` and there is no album-artist grouping — "Various Artists"
albums fragment) · decade/year-range browse UI (`DomainAlbumListType.ByYear` is SQL-backed but exposed
only as a bare sort entry) · mood/tag browsing (`DomainSong.moods` is stored and only ever fed to
AudioMuse) · faceted filtering or per-list text filter · a genre detail screen · alphabet scroll on songs.

### Metadata & social
Present: Subsonic scrobbling with offline queueing (`ScrobbleManager` + `SyncActionType.SCROBBLE`),
play counts, star + 5-star rating, a genuinely strong lyrics stack (three providers with user-ordered
priority, synced **and** word-by-word karaoke, DB-cached, share cards), artist bios, similar artists,
deep technical metadata (`ui/screens/song/SongDetailScreen.kt`).

**Not present:** Last.fm scrobbling (only `lastFmUrl` deep links) · ListenBrainz (zero references —
notable given `lb-bot/` sits in this repo) · **credits/roles UI** (`DomainContributor` and
`DomainArtist.roles` are parsed and stored but never rendered — cheap, high-perceived-quality win) ·
local `.lrc`/embedded lyrics · lyrics offset editing · thumbs up/down.

### Playlists
Present: create, add songs, remove song, delete (offline-capable), smart playlists via the Navidrome
native API (`ui/screens/playlist/SmartPlaylistEditorScreen.kt` + `domain/manager/NativeApiManager.kt`,
16 fields), save-queue-as-playlist, download policies.

**Not present:** **playlist track reordering** (the drag machinery in `util/ui/ReorderUtils.kt` is
queue-only; nothing calls `updatePlaylist` with an order) · m3u/pls import or export · playlist folders ·
public/collaborative toggle (the Subsonic `public` flag isn't surfaced) · playlist cover upload ·
duplicate detection on add.

---

## 4. Platform integration

- **No Android Auto.** `PlaybackService` extends `MediaSessionService`, not `MediaLibraryService` — no
  `onGetLibraryRoot`, no browse tree, no `automotive_app_desc`, no `MediaBrowserService` intent filter.
  This is the largest single platform gap: it's the difference between "a phone app" and "a music player".
- **No Wear OS** module.
- **No quick-settings tile** (`TileService`) — the natural home for the offline toggle in §2.
- **No app shortcuts** (`shortcuts.xml` / `ShortcutManager`) — no long-press "shuffle favorites".
- **No custom notification actions.** `DefaultMediaNotificationProvider` is used unmodified, so no
  favorite / shuffle / repeat button and no `setCustomLayout`; no notification sleep-timer or speed control.
- **Dead widget.** `androidApp/.../widgets/nowplaying/NowPlayingWidget.kt` + `NowPlayingReceiver.kt` +
  `NowPlayingKeys.kt` exist but are **not registered in `AndroidManifest.xml` and have no widget-info
  XML** — unreachable code. Either wire it up or delete it. (`miniplayer` and `turntable` widgets *are*
  registered and work.)
- No in-app language picker beyond `locales_config.xml`; no tablet/landscape now-playing layout beyond
  `ArtworkPager`'s `isLandscape`.

---

## 5. Saved-queue audit — findings and fixes (done in this pass)

Sixteen defects found reading the saved-queue path end to end (Navic Room + `SavedQueueRepository` +
`HubManager`, `hub/hub.py`, `PROTOCOL.md` §8.3). **All fixed**; regression tests in
`hub/tools/test_saved_queues.py` (8 cases), and the existing `hub/tools/test_edits.py` suites still pass.

**Hub (`hub/hub.py`)**
1. **Tombstones didn't stop resurrection.** Consulted only in `_merge_saved_queues`. A device still
   playing a queue another device deleted republished its id and re-created the record. → guard in
   `_upsert_saved_queue`, and `setQueue` now mints a new id for a tombstoned one.
2. **Deleting an id the hub didn't hold wrote no tombstone** (`if rid and rid in self.saved_queues`
   gated both). → factored `_delete_saved_queue()`, tombstones unconditionally.
3. **Live-record merge ordering.** The `cur is None` insert ran *before* the live-session guard, so
   after a restart that kept `session.savedQueueId` but lost the record, a stale client copy became the
   playing queue. And the guard `continue`d outright, so an offline rename of the current queue could
   never reconcile. → live branch first; rebuild from the live session; accept a newer `name`.
4. **One malformed record could break persistence permanently.** A non-numeric `updatedAt` made
   `sorted(key=…)` in `_saved_queues_list` raise `TypeError` *inside `_save()`*, aborting the act handler.
   → `_sanitize_saved_queue()` (record + per-track whitelist, int/str coercion, caps), `_as_int` on the
   sort key, `_save`'s payload built inside its `try`, and `_load` re-sanitises so a poisoned
   `state.json` self-heals.
5. **A reconnecting client could evict another device's visible history** — the merge accepted
   `SAVED_QUEUES_MAX * 2` rows. → capped at 20; eviction now logs what it dropped.
6. **`renameSavedQueue` on an unknown id was silent** while the client had already renamed locally. →
   `error { code: "unknown_saved_queue" }`.
7. **Clear-all cost N broadcasts + N state writes.** → new `deleteSavedQueues { ids }` act, and
   `syncSavedQueues` gained `deleted[]`, applied **before** `queues`.

**Navic**
8. **`SavedQueueRepository.index` was a plain `mutableMapOf`** read synchronously on the main thread by
   `findMatching` while mutated from IO coroutines — a `ConcurrentModificationException` waiting to
   happen, and a torn read mints a duplicate card. → copy-on-write `@Volatile` map + `Mutex` for writers.
9. **Session ids could collide across devices.** `"q_${millis}_${sessionIdCounter++}"` with a non-atomic
   `Int`; two devices starting in the same millisecond both produced `q_<t>_0`, and the hub's id-keyed
   merge would silently fuse two unrelated sessions. → random suffix, matching hub and Feishin.
10. **Offline deletes were silently reverted** — the hub act only fired when connected, so the reconnect
    adopted the hub's list and put the row back. → local tombstone store
    (`PreferenceManager.deletedSavedQueueIds`, capped 200 / 30-day TTL), replayed via
    `syncSavedQueues.deleted`, and `applySavedQueues` filters tombstoned ids.
11. **`reconcileRemoteQueue` dropped the queue identity**, calling `loadRemoteQueue` with its null
    defaults mid-playback; the next publish minted a fresh id and forked a card. → identity carried through.
12. **`hubSavedQueueIdFor` minted without persisting**, and the claim-republish loop re-derived it each
    pass — one card per republish. → `MediaPlayerViewModel.adoptQueueSessionId()`.
13. **Cover-art divergence.** Navic moved `coverArtId` with the resume cursor while hub and Feishin freeze
    it at birth, so one shared record rendered different art per client. → frozen at birth everywhere
    (`SavedQueueDao.updateProgress` no longer writes it; `applySavedQueues` uses the first track);
    PROTOCOL.md §8.3 now states the rule explicitly for id-rendered art.
14. **`replaceFromHub` didn't cap** to 20 (Feishin's `mergeFromHub` does), so Navic could hold 21 rows.
15. **Frame ordering wasn't guaranteed** — `sendAsync` did a bare `scope.launch` per frame. → `Mutex`.
16. Minor: `rename` left the in-memory `IndexEntry.updatedAt` stale (skewing `findMatching`'s tie-break) ·
    `upsert` truncated `positionMs` from a float ratio instead of rounding (resume drifted backwards per
    sync hop) · `deleteOne` vs `deleteOthers` ordered local-vs-hub work inconsistently · a corrupt
    `queueJson` uploaded `songs: []` and was silently rejected forever, unlogged · dead
    `queue_now_playing` import.

**No Room migration was needed** — `CacheDatabase` stays at **v19**. Only query text and written values
changed. Tombstones deliberately live in `PreferenceManager`, not Room.

### Feishin follow-ups (not done here)
- Keep local tombstones and send `syncSavedQueues.deleted` (it has the same offline-delete bug).
- Adopt `deleteSavedQueues` for its clear-all / delete-others surfaces.
- Confirm its card art renders from the record's birth track (it freezes `coverImageUrl`, so it is
  probably already conformant).

### Known remaining, deliberately deferred
- `PlayerUiState` carries `progress: Float`, not `positionMs`, so Navic's resume position is still
  derived from a ratio — now rounded, but exact fidelity needs a `positionMs` field on the state.
- Saved-queue records persist `streamUrl`/`imageUrl` (credentialed Subsonic URLs) in the hub's
  `state.json`, because receivers — the Chromecast bridge in particular — need them. Both clients
  re-resolve by song id on restore and ignore the stored URL, so stripping them from *history* records
  (while keeping them on the live session queue) is a safe credential-hygiene win. Not done: it changes
  what the cast bridge sees on a restore path and wants its own test.

---

## 6. Prioritized backlog

| # | Item | § | Effort | Impact | Status |
|---|---|---|---|---|---|
| 1 | Foreground service + `WorkManager` for downloads | 1 | L | High | next |
| 2 | Android Auto (`MediaLibraryService` + browse tree) | 4 | L | High | next |
| 3 | Forced-offline actually filters the library + quick toggle | 2 | S | High | next |
| 4 | HTTP range resume for interrupted downloads | 1 | M | High | next |
| 5 | Equalizer — even just a system-EQ launch intent | 3 | S | High | next |
| 6 | Streaming cache (`SimpleCache`/`CacheDataSource`) | 1 | M | Med-High | next |
| 7 | Storage-usage screen + per-download delete | 2 | M | Med-High | next |
| 8 | Play history (recently-played tracks) | 3 | S | Medium | next |
| 9 | Playlist track reordering | 3 | S | Medium | next |
| 10 | Artist / favorites download actions | 1 | S | Medium | later |
| 11 | Global download storage cap + LRU eviction | 1 | M | Medium | later |
| 12 | Orphan file + stale `.part` sweep | 1 | S | Medium | later |
| 13 | Auto-retry downloads on connectivity regain | 1 | S | Medium | later |
| 14 | Credits/roles UI (data already parsed) | 3 | S | Medium | later |
| 15 | Album-artist browsing + compilation handling | 3 | M | Medium | later |
| 16 | Custom notification actions (favorite/shuffle) | 4 | S | Medium | later |
| 17 | Crossfade + skip-silence | 3 | M | Medium | later |
| 18 | m3u import/export | 3 | M | Medium | later |
| 19 | SAF / external-storage download destination | 2 | M | Medium | later |
| 20 | Folder / file-tree browsing | 3 | M | Low-Med | later |
| 21 | Quick-settings tile + app shortcuts | 4 | S | Low-Med | later |
| 22 | Last.fm / ListenBrainz scrobbling | 3 | M | Low-Med | later |
| 23 | Wire up or delete `NowPlayingWidget` | 4 | XS | Low | later |
| 24 | Fix concurrency-needs-restart + `downloadEntireLibrary` worker count | 1 | XS | Low | later |
| 25 | `downloadCollection()` mislabels playlists as ALBUM | 1 | XS | Low | later |
| — | All 16 saved-queue defects | 5 | — | — | **fixed-in-this-pass** |
