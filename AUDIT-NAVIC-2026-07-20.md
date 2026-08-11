# Navic Full Code Audit — Findings & Fix Plan (2026-07-20)

## Context

Full audit of the Navic app (Kotlin Multiplatform / Compose Android client in `navic/`): bugs,
inefficiencies, optimizations, and QoL/UX improvements across UI, queue logic, and hub integration.
Three parallel deep-dive audits were run (player/queue, hub/cast, UI/downloads/AudioMuse). This file
collects the verified findings and orders them into an actionable fix plan.

---

## Area 1: Player & Queue logic

### Bugs

| Sev | Location | Finding |
|---|---|---|
| HIGH | `MediaPlayer.android.kt:706-719` (+ `:410`) | Every `isPlaying→true` launches a new unbounded progress loop; no Job tracking/cancel → overlapping loops, jitter. Fix: track `progressJob`, cancel before relaunch. |
| HIGH | `MediaPlayer.android.kt:272-292` | `switchSessionPlayer` drops URI-less items but reuses old index (only clamped) → wrong track after Cast→local handoff. Fix: relocate current item by `mediaId` in filtered list. |
| MED | `MediaPlayer.android.kt:528-556` | `updatePlaybackState` indexes possibly-stale `_uiState.queue` with controller index → transient null `currentSong`. Fix: guard on size mismatch or map by `mediaId`. |
| MED | `MediaPlayer.android.kt:513-526` | `refreshCurrentCollection` caches `loadingCollectionId` even when album lookup returned null → never retries. Fix: reset in `finally`, don't cache on miss. |
| MED | `MediaPlayer.android.kt:396-406` | Auto-skip of unavailable track calls `seekToNextMediaItem()` with no next → stuck on last track. Fix: stop/idle when `!hasNextMediaItem()`. |
| MED | `MediaPlayer.android.kt:859-871` | `clearQueue` keeps `savedQueueId`/`savedQueueKind` → SavedQueues screen shows cleared session as active. Fix: null them out. |
| MED | `MediaPlayer.android.kt:1115-1125` | Local `seek()` never routed to hub when remote active — scrubber moves silent local player. Fix: route via `hubManager.actSeek` when `isRemoteActive` (mirrors QueueScreen routing at `QueueScreen.kt:322,365,373,145`). |
| MED | `SavedQueueRepository.kt:57-118` + `MediaPlayer.kt:340-382` | Unsynchronized `cachedId/cachedQueueRef/cachedSig` mutated by two concurrent uiState collectors → cache thrash, redundant blob re-encodes. Fix: single collector or guard cache. |
| LOW | `HubManager.kt:567-573` | `progress` frame missing `isPlaying` defaults to `false` → spurious paused flicker. Fix: default to current mirror value. |
| LOW | `MediaPlayer.android.kt:917-936` | `playNextSingleLocal` with `currentIndex == -1` on non-empty queue prepends+activates. Fix: treat like empty branch (append). |
| LOW | `RemoteSessionPlayer.kt:60-66,127-151` | 1500 ms activation grace drops genuine user taps from system controls. Consider narrowing. |

### Inefficiencies

| Sev | Location | Finding |
|---|---|---|
| MED | `MediaPlayer.android.kt:463-508` | 7-flow `combine` without `distinctUntilChanged`; every `isCellular` re-emit rebuilds all media items and re-seeks current → rebuffering on flaky networks. |
| MED | `RemoteSessionPlayer.kt:82-125` (`:74-79`) | Full playlist `MediaItemData` list rebuilt every 1 s tick. Cache keyed on track-id signature. |
| LOW | `HubManager.kt:393-399` | 250 ms remote-mirror tick wakes forever even when local. Gate on `isRemoteActive`. |
| LOW | `MediaPlayer.android.kt:457` | Controller setup blocks on `downloadManager.allDownloads.first()` on critical path. |

### QoL / UX

| Sev | Location | Finding |
|---|---|---|
| MED | `MediaPlayer.kt:109-171` | Undo stack (MAX 10) is dead code — UI exposes only top; dismissed snapshots retained. Implement multi-level undo or reduce to single slot. |
| LOW | `MediaPlayer.android.kt:1095-1113` | No queue-empty guard / shuffle+repeat-one coordination in toggles. |
| LOW | `QueueScreen.kt:321-324,370-374,144-146` | Undo snapshot captured before hub confirms remote edit — undo may "restore" an unchanged queue. |

---

## Area 2: Hub & Cast integration

### Bugs

| Sev | Location | Finding |
|---|---|---|
| HIGH | `HubManager.kt:99,496,513-523` | No pong-timeout / Ktor `pingInterval` — half-open socket (Wi-Fi→cellular, NAT timeout) blocks `incoming` forever; `pingLoop` failure just returns. Connection silently dead until app kill. Fix: `pingInterval = 10.seconds` and/or stale-pong teardown. |
| HIGH | `HubManager.kt:451-464` | Fixed 3 s reconnect, no exponential backoff (protocol mandates it); bad token → infinite tight retry loop, auth error never surfaced. Fix: capped exponential backoff; stop on auth error frame. |
| HIGH | `MediaPlayer.android.kt:207-226` | **Known open item root cause**: `SessionAvailabilityListener` only fires on transitions — a cast session already live at process launch is never adopted; playback silently reverts to phone. Fix: check `castPlayer.isCastSessionAvailable` right after construction and call `switchSessionPlayer(castPlayer)`. |
| MED | `HubManager.kt:185-191,142,458,473` | `restart()` cancels without joining → old coroutine races new one over shared `wsSession` (duplicate/stale sockets). Called on every settings change (`NaviConnectScreen.kt:58`). Fix: join old job or mutex + generation counter. |
| MED | `HubManager.kt:667,947,996` | `hubDrivenUntilMs` is a fixed 2 s wall-clock guard; a slow `do:load` outlasts it → reporter echoes the queue back / pauses wrong device. Fix: load-generation token cleared on completion. |
| MED | `MediaPlayer.android.kt:142-143,1141-1182` + `CastOptionsProvider.kt:14-18` | Local player auths via custom HTTP headers; Default Media Receiver can only fetch plain URLs → header-authenticated deployments can't cast. Fix: embed credentials in cast URLs or document limitation. |
| LOW | `HubManager.kt:690-697` | `do:seek` racing ahead of queue load is silently dropped (`duration == 0`). Fix: stash pending seek, apply after load. |
| LOW | `HubManager.kt:567-573` | Partial `progress` frame defaults `positionMs` to 0 and `isPlaying` to false → scrubber snap. Default to previous values. (Same as Area 1 finding.) |

### Inefficiencies

| Sev | Location | Finding |
|---|---|---|
| MED | `HubManager.kt:586-608` | `applySession` remaps the entire queue JSON on every session frame even when only index/position changed. Fix: diff by id-signature, reuse previous tracks list. |
| MED | `HubManager.kt:414-415,382-399` | Display mirror rebuilds the full id-signature string (`joinToString`) ~4×/s; reporter side already has a by-reference cache (`:917-926`) — apply it to the mirror too. |
| LOW | `HubManager.kt:393-399` | 250 ms mirror loop wakes unconditionally even when not remote-active. |
| LOW-MED | `MediaPlayer.kt:343-361` | Full-queue JSON serialized to disk every ~1 s debounce during playback. Skip when only progress changed; persist position separately. |

### QoL / UX

| Sev | Finding |
|---|---|
| MED | `error` frames (`auth`, `target_offline`, `bad_action`) only logged (`HubManager.kt:579-582`) — wrong token looks like "never connects". Expose a connectionState/errors StateFlow → snackbar in DevicePickerSheet / NaviConnectScreen. |
| MED | Native cast is fully disjoint from the hub (protocol §12.2 unimplemented): Chromecast never registers as a receiver, picker shows two separate lists (`DevicePickerSheet.kt:161` vs `:195`), transfers can't target it. Biggest structural gap (acknowledged Phase 2). |
| LOW | Transfer to a device that just went offline → hub error only logged, sheet already dismissed, no feedback (`DevicePickerSheet.kt:115-124`). |
| LOW | Cast connect to a stale cached route id is a silent no-op (`AndroidCastManager.kt:102-106`). |
| LOW | Active cast scan (`CALLBACK_FLAG_PERFORM_ACTIVE_SCAN`, `AndroidCastManager.kt:79`) can leak past abnormal picker teardown — battery drain. |

Verified correct (don't re-flag): `released` handshake ordering; report loop cancelled per-connection; no idle 1 Hz report spam; 1:1 placeholder queue resolution keeps hub indices aligned.

---

## Area 3: UI / Downloads / AudioMuse / Theming

### Bugs

| Sev | Location | Finding |
|---|---|---|
| HIGH | `DownloadCenterViewModel.kt:90-123` | `allDownloads` emits on every ~1% progress write (`DownloadManager.kt:599-616`); collector re-runs `getSongsByIds` over the whole downloaded library and re-sorts all four sections per emission → sustained DB/CPU jank while downloading. Fix: `distinctUntilChanged` on id-set; merge progress separately. |
| HIGH | `DownloadCenterScreen.kt:95-101,217-231` | Completed section is an eager `Column` + `forEach` — composes every row for a fully-downloaded library. Fix: single `LazyColumn` with keyed `items`. |
| MED-HIGH | `BlendBackground.kt:55-57,119,136,154` | Frame-tick rotation read via `Modifier.rotate(state)` at composition scope → recomposes Box + three AsyncImages ~60×/s during playback. Fix: `graphicsLayer { rotationZ = ... }` (draw-phase read; same trick as `CoverColorScheme.kt:390-393`). |
| MED | `PlaylistDownloadManager.kt:143-157` | 6-hour `syncAll` re-queues permanently-failing songs forever; `retryCount` tracked but never gates (`DownloadManager.kt:519,281`). Fix: exclude FAILED with retryCount ≥ N or per-policy backoff. |
| MED | `DownloadManager.kt:313-317,353,455-457` | Library/queue download does one sequential point query per song for "already downloaded". Fix: load set once via `getSongIdsByStatus()` (`DownloadDao.kt:35`). |
| LOW-MED | `DownloadManager.kt:226-234,525-529` | Duplicate-download TOCTOU: check and insert in separate locks; `executeDownloadProcess` removes from `activeDownloads` early. Fix: check-and-insert under one lock. |
| LOW-MED | `DownloadManager.kt:605-614` | Progress writes cancel/relaunch a coroutine per 1% tick, no write ordering. Fix: conflated channel + single collector. |
| LOW | `DownloadManager.kt:273-291` | `retryDownload` double-inserts QUEUED row (harmless, redundant). |
| LOW | `RelatedSongsViewModel.kt:74-79` (+ `RadioManager.kt:208-211`) | Fetch errors collapse to `Success(empty)` — user can't tell "offline" from "nothing related". |

### Inefficiencies

| Sev | Location | Finding |
|---|---|---|
| MED | `AudioMuseManager.kt:48-64,80-95,118-140,154-168,180-192` | New `HttpClient` built and torn down per call, on the hot autoplay top-up path (`RadioManager.kt:385-397`). Fix: one lazy reused client (pattern exists in `CoverColorScheme.kt:57-65`). |
| MED | `AdaptiveMoodBackground.kt` | **Dead code** (verified: sole reference is its own declaration at `:39`; nothing composes it). Delete the file instead of optimizing its per-frame 90dp blur. |
| LOW | `NowPlayingAutoplaySelector.kt:84-95` | `remember(audioMuseManager.isConfigured)` keys on a non-reactive plain getter — Tier-2 modes don't appear until screen re-entry after configuring. |

Notes: palette/image caching (`CoverColorScheme`) is already well-optimized; no main-thread DB queries found; download constraint gating, `.part`→finalize atomic write, and crash reconciliation are sound.

### QoL / UX + deferred-feature hook points

| Sev | Finding |
|---|---|
| MED | Concurrency stepper (`DownloadCenterScreen.kt:258-279`) silently applies only after restart (semaphore fixed at construction, `DownloadManager.kt:78-79`) with no user-facing hint. |
| LOW | Tier-2 autoplay rows shown whenever URL+token set; unreachable core silently degrades to genre mix (`RadioManager.kt:400-405`). Grey out via a reachability probe per fail-soft design. |
| — | ~~**CLAP Mood Search is dead code**~~ **CORRECTION (2026-07-20):** no longer dead — `RadioManager.fetchMoodSearchSongs` + `isClapAvailable` are wired via `MoodSearchSheet.kt` → `SearchScreen.kt`. No action needed. |
| — | **Downloaded-only filter**: `DownloadDao.getSongIdsByStatus()` already built; no UI references it. Hook: `SortButton` menus on album/song/playlist lists. |
| — | **Alphabet fast-scroll**: absent. Hook: `AlbumListScreen`, `ArtistListScreen`, library `Content.kt:120`. |
| — | **Recently-added songs row**: home has album rows only (`library/components/Content.kt:204-274`). Hook: new `horizontalSection` + query. |

### Architecture

| Sev | Finding |
|---|---|
| LOW | `DownloadEntity` declared in both Room DBs but only `DownloadDatabase` is wired (`CacheDatabase.kt:34-46` vs `di/DatabaseModule.kt:10-19`) — dead schema doubling migration burden; a future `CacheDatabase.downloadDao()` call would silently read an empty table. Fix: drop it from `CacheDatabase`. |

---

## Proposed fix plan (prioritized)

### Phase 1 — High-severity correctness & perf (do first)
1. **Hub connection robustness** (`HubManager.kt`): add Ktor `pingInterval` (dead-socket detection), capped exponential backoff + stop-on-auth-error, make `restart()` join the old job before reconnecting.
2. **Cast re-adoption at launch** (`MediaPlayer.android.kt:207-226`): after constructing `castPlayer`, check `isCastSessionAvailable` and switch immediately — closes the known open item.
3. **Progress-loop job leak** (`MediaPlayer.android.kt:706-719`): track/cancel `progressJob`.
4. **Cast→local index remap** (`MediaPlayer.android.kt:272-292`): relocate current item by `mediaId` after filtering URI-less items.
5. **Download Center perf** (`DownloadCenterViewModel.kt:90-123` + `DownloadCenterScreen.kt`): distinct id-set flow + LazyColumn conversion.
6. **BlendBackground recomposition storm** (`BlendBackground.kt:119,136,154`): replace the three `.rotate(state)` calls with `.graphicsLayer { rotationZ = state }`. Visually identical by construction — `rotate(x)` *is* `graphicsLayer(rotationZ = x)` with the same center transform-origin, same layers, same parent 80dp blur/color-matrix/crossfade; only the read moves from composition to the layer-update phase. Keep the `withFrameNanos` loop and states unchanged; verify on-device that the now-playing wash animates identically.
7. **Delete dead `AdaptiveMoodBackground.kt`** (`ui/screens/nowPlaying/components/controls/`): declared but composed nowhere — remove the file (and nothing else; no call sites exist).

### Phase 2 — Medium correctness
8. Route local `seek()` to hub when remote active (`MediaPlayer.android.kt:1115-1125`).
9. Replace `hubDrivenUntilMs` wall-clock guard with a load-generation token (`HubManager.kt:667`).
10. `progress`-frame defaults keep previous values instead of 0/false (`HubManager.kt:567-573`).
11. `clearQueue` nulls `savedQueueId`; `refreshCurrentCollection` retry on miss; stuck-last-unavailable-track stop (`MediaPlayer.android.kt`).
12. Download retry cap in `syncAll` re-queue filter (`PlaylistDownloadManager.kt:143-157`); check-and-insert dedup under one lock; bulk downloaded-set query instead of per-song point queries (`DownloadManager.kt`).
13. Shared lazy `HttpClient` in `AudioMuseManager`; by-reference signature cache in the hub mirror; `applySession` queue diffing; skip full-queue re-serialization when only progress changed.
14. `distinctUntilChanged` on the 7-flow combine to stop cellular-flap rebuffering (`MediaPlayer.android.kt:463-508`).
15. Drop dead `DownloadEntity` schema from `CacheDatabase`.

### Phase 3 — QoL / UX
16. Surface hub `error` frames + connection state (snackbar in device picker / NaviConnect settings).
17. Grey out Tier-2 autoplay modes on reachability probe (fail-soft polish); make `isConfigured` reactive.
18. "Applies after restart" hint on the concurrency stepper (or resizable semaphore).
19. Simplify queue undo to a single slot (or expose real multi-level undo); capture remote undo after hub ack.
20. Distinguish "fetch failed" from "empty" in RelatedSongs.
21. Deferred features at their identified hooks: downloaded-only filter (via `getSongIdsByStatus()`), alphabet fast-scroll, recently-added songs row. (CLAP mood-search is already wired via `MoodSearchSheet.kt` → `SearchScreen.kt` — not dead code; see the corrected row above.)
22. (Larger, optional) Hub↔native-cast integration per protocol §12.2 so the Chromecast becomes a hub device from Navic too.

## Verification
- No compiler in the sandbox: build via `set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot` + `.\gradlew :androidApp:assembleRelease` in Android Studio/terminal (per README §8); commonMain must still compile for iOS.
- Manual flows to re-test after Phase 1/2: rapid pause/play (progress loops), cast→local handoff with an unsynced track, kill+relaunch app during a live cast session, Wi-Fi→cellular handoff while hub-connected, wrong hub token (should surface error, back off), Download Center open during a large playlist download, scrub while a remote device is active.
