# navi-connect — Implementation prompts (pickup-ready)

Two self-contained prompts for a fresh agent. Each starts with an **audit step**, because a lot of
the originally-planned work is already in the tree — implement only what the audit shows is missing.

General ground rules (both tasks):
- **Navic cannot be compiled in the sandbox** — no JDK/Android SDK. Write Kotlin carefully; the user
  builds in Android Studio (`./gradlew :composeApp:assembleRelease` — test *release*, debug Compose
  is misleadingly choppy). Expect an occasional compile-fix round.
- **iOS must still compile** (commonMain changes) even though iOS is out of scope for testing.
- Navic files use **tabs**; multi-line `Edit` matches are fragile — match single bare lines without
  leading whitespace. Kotlin is whitespace-insensitive, so minor indent imperfections are harmless.
- After any Feishin change, typecheck with the two `tsc` commands (NOT `pnpm run typecheck`):
  `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` and `... tsconfig.node.json ...`.

---

## PROMPT 1 — Finish the Symfonium-style plan (Navic)

You are continuing the navi-connect project (see `HANDOFF.md`, `NAVIC-SYMFONIUM-PLAN.md`,
`PROJECT-SUMMARY-2026-07-18.md`). The Symfonium-style reliability/queue/offline/polish plan is
**largely implemented already** — your job is to finish the genuinely-remaining pieces without
duplicating what exists.

### Phase 0 — Audit (do this first, report a checklist)

For each plan area, open the listed files and classify it **done / partial / missing**. Confirmed
already present in the tree (do NOT rebuild — only extend if the audit shows a gap):

- **Shared song-row system** — `ui/components/common/SongRow.kt` + `ui/screens/collection/components/SongRow.kt`.
- **Remote queue edit** — `HubManager.actMoveQueueItem/actRemoveQueueItem/actClearQueue` (wired in
  `ui/screens/queue/QueueScreen.kt`); confirm clear/remove route to the hub while remote (not the
  old always-local `player.clearQueue()`), and that reorder/remove before the current index preserves
  the playing track's identity.
- **Download center** — `ui/screens/settings/DownloadCenterScreen.kt` + `viewmodels/DownloadCenterViewModel.kt`,
  `data/database/entities/DownloadEntity.kt` (has `status` QUEUED/DOWNLOADING/DOWNLOADED/FAILED,
  `retryCount`, `error`, `fileSize`, `sourcePolicy`, timestamps), `dao/DownloadDao.kt`, `DownloadMigrations.kt`.
- **Saved queues** — `ui/screens/savedqueues/SavedQueuesScreen.kt` + viewmodel,
  `domain/repositories/SavedQueueRepository.kt`, `data/database/entities/SavedQueueEntity.kt`,
  `dao/SavedQueueDao.kt`.
- **Palette de-dup** — verify `NowPlayingScene.colorSchemeForCurrentSong` no longer duplicates
  `util/ui/CoverColorScheme.kt` (it appears already consolidated), and that no composable builds an
  inline `HttpClient(...)` for palette loading.
- **`SongRow` "View album" null-safety** — verify the old `song.albumId as String` cast is gone
  (it appears fixed) so remote-placeholder songs can't crash album navigation.
- **Atomic downloads** — verify `DownloadManager` writes to a temp/`.part` file then finalizes.
- **Sync guard** — verify `DbRepository` still skips obsolete-song/album cleanup when any album
  fetch failed (don't regress it).

### Phase 1 — Implement the remaining items (confirmed missing / likely missing)

1. **Queue Undo** (missing — no undo/snackbar in `ui/screens/queue/`). Add short-lived, session-scoped
   undo for: clear queue, remove row, move row, and play-now queue replacement. Implement as an
   in-memory `ArrayDeque<QueueUndoSnapshot>` (or single last-action) in `MediaPlayerViewModel` (local)
   and mirror for remote via `HubManager` (re-apply the previous queue with `actSetQueue`/setQueue).
   Surface via a Compose `SnackbarHost` on the queue screen with an "Undo" action; expire after ~6s.

2. **Queue history with session typing** (partial — `SavedQueueEntity` stores snapshots but has **no
   session-kind field**). Add a `sourceKind` column (enum-as-string: `manual`, `album`, `playlist`,
   `radio`, `moodFlow`, `journey`) via a new Room migration (follow the additive
   `ALTER TABLE ADD COLUMN` pattern already in `DownloadMigrations.kt`, default `manual`). Set it at
   save time from whoever mints the queue (`RadioManager.startRadio`→`radio`, `startJourney`→`journey`,
   Mood Flow top-up→`moodFlow`, album/playlist play→their kinds). Surface as a filter/section header in
   `SavedQueuesScreen`, and add row actions: **restore queue**, **resume queue** (restore + seek to
   saved `currentIndex`/`positionMs`), **save as Navidrome playlist** (reuse `NativeApiManager` /
   existing playlist-create path).

3. **Download QoL** (audit `DownloadManager` + `DownloadCenterScreen` first — some may exist). Add any
   missing: Wi-Fi-only and charging-only constraints (read `PreferenceManager` flags; gate the
   download loop), max-concurrency setting (the semaphore permit count), "repair missing files"
   (re-download rows whose `filePath` is gone), retry-failed, and per-policy ownership already shown
   via `sourcePolicy`. Also: **download current queue** and **download next N songs** entry points.

4. **Library QoL** (mostly missing — verify). Add, in priority order: an **alphabet jump list**
   (fast-scroll index) for large Album/Artist/Song lists; a **Continue Listening** row on
   `LibraryScreen` sourced from saved-queue/session history; a **Recently added songs** row (not only
   albums — `getAlbumList2`/`getSongs` newest); **downloaded-only** filters on artist/album/playlist
   lists; and better **empty/error/stale** states ("Showing cached library, sync failed" instead of a
   blank/scary page — wire into the existing sync-failure signal in `DbRepository`).

5. **DAO-side sorting** (partial — `AlbumRepository.getAlbumsLimited` + `SortUtils.toSqlQuery(limit)`
   exist). Extend the same pattern to move remaining large in-memory `SongRepository`/`SortUtils`
   sorts into `SongDao`/`AlbumDao` queries where a full-library in-memory sort still happens.

### Constraints
- Queue rows must use **one shared queue/now-playing ambient**, never per-row extracted cover colors
  (mixed queues would look noisy) — the plan is explicit on this.
- Keep library-home **brightness stable**; only hues adapt.
- Hub changes only where remote queue/session behavior needs them; Navic is the primary target.

### Verify
- Build Navic release; manual checks from `NAVIC-SYMFONIUM-PLAN.md` "Test Plan": clear/remove/undo
  queue locally + remotely; restore/resume/save-as-playlist a saved queue; start/cancel/retry/repair a
  large download with Wi-Fi-only on; alphabet jump + downloaded-only filter; forced album-fetch-failure
  shows cached library, not a blank page.
- Add hub tests for remove/clear/move/paused-transfer/stale-report/release-timeout if not present.

---

## PROMPT 2 — Navic native Cast lifecycle re-adoption (Android)

You are continuing navi-connect. **The native-Cast crash is already fixed** (`SafeMediaItemConverter`
in `androidMain/.../shared/MediaPlayer.android.kt` — a converter returning a placeholder `MediaItem`
when `MediaQueueItem.getMedia()` is momentarily null). What remains is the **cast session lifecycle
after a process restart**, flagged "⏳ STILL OPEN" in `HANDOFF.md`.

This is **Navic's own MediaRouter/media3 Cast path** (`AndroidCastManager` + `PlaybackService`'s
`CastPlayer` swap) — *not* the Feishin hub cast bridge. Use Feishin's hardened bridge
(`feishin/src/main/features/core/cast/index.ts`: `adoptRunningSession`/`getSessions`+`join`,
try/catch around `getStatus`, release semantics) as the reference behavior to mirror.

### The problem (four symptoms the user hit)
After the app process dies while a TV keeps casting, on reopen:
1. Cast session **not restored** in now-playing (TV still plays, Navic shows nothing).
2. Tapping **Cast → TV again does nothing** (stale MediaRouter/`CastManager` state).
3. **Transfer** resumes the **stale pre-crash song**, not the song selected locally before re-casting.
4. **Transfer back** to Navic leaves the TV casting AND restarts at **index 0** (index+position lost).

### Current code (read these first)
- `androidMain/.../domain/manager/AndroidCastManager.kt` — MediaRouter discovery + `connect`/
  `disconnect`. `_devices` cached across discovery stops. No re-adoption logic.
- `androidMain/.../shared/MediaPlayer.android.kt` (~L195-325):
  - `castPlayer = CastPlayer(CastContext.getSharedInstance(this), SafeMediaItemConverter())` with a
    `SessionAvailabilityListener` → `onCastSessionAvailable` swaps to `castPlayer`,
    `onCastSessionUnavailable` swaps back to `exoPlayer`.
  - `switchSessionPlayer(newPlayer)` carries queue/index/position/`playWhenReady` between players and
    snapshots URI-bearing items into `lastLocalItems` (restores them by `mediaId` on the way back).
  - `remotePlayer` (`RemoteSessionPlayer`) swap for the hub-remote path is separate — don't disturb it.
- Reference: `feishin/src/main/features/core/cast/index.ts` (`adoptRunningSession`, `maybeAdopt`,
  `tryReadoptAfterDrop`, `getSessions`→find appId `CC1AD845`→`join`).

### Implement

1. **Re-adopt a running Cast session on process (re)start.** In `PlaybackService.onCreate`, after
   building `castPlayer`, check `CastContext.getSharedInstance(this).sessionManager.currentCastSession`
   (and/or `currentSession`): if a session is already connected, treat it like `onCastSessionAvailable`
   — but **rebuild the timeline from the receiver**, not from a dead local player. Read the running
   session's `RemoteMediaClient` `MediaStatus`/`MediaQueue` to reconstruct the current item id,
   index, and position, then swap `castPlayer` into the `MediaSession` and let the app's
   `MediaController` observers (now-playing UI + hub reports) pick it up. Add a `SessionManagerListener`
   so a session that appears slightly after startup is also adopted (mirrors Feishin's `maybeAdopt`).
   Guard with an `adopted` flag so it runs once per connection.

2. **Recover stale MediaRouter state (symptom 2).** In `AndroidCastManager`, make `connect` robust to a
   route that MediaRouter thinks is already selected: if `selectedRoute` already matches but no live
   session exists, unselect+reselect (or re-add the callback with `CALLBACK_FLAG_PERFORM_ACTIVE_SCAN`
   and refresh) so "Cast → TV" always establishes a working session. Ensure discovery is (re)started
   on service create and that `_connectedName`/`_devices` reflect the truly-live session, not a stale
   cache after a crash.

3. **Make the hub session follow the live local selection (symptom 3).** When the user picks a new
   local song/queue before re-casting, that must become the session intent before any transfer. Ensure
   the local selection publishes to the hub (`HubManager.publishQueueIfOurs`/claim-active) so a
   subsequent transfer carries the *new* queue, not the pre-crash intent held by the hub. Verify the
   re-adoption in step 1 reconciles the hub session's `index`/`positionMs` to what the receiver
   actually reports (don't blindly re-push the stale hub queue over a live cast).

4. **Transfer-back must carry index+position and stop the TV (symptom 4).** Transfer-back arrives as a
   hub `do:load`/session change while `castPlayer` is the session player. Ensure the handoff:
   (a) explicitly **releases/stops the Cast session** (`RemoteMediaClient.stop()` or
   `sessionManager.endCurrentSession(true)`) so the TV stops, then (b) swaps back to `exoPlayer` via
   `switchSessionPlayer` carrying the **hub session's** index+position (not index 0). `switchSessionPlayer`
   already carries index/position between players, but transfer-back may not go through
   `onCastSessionUnavailable` — wire an explicit path from the hub `do:load`/`release` handler so the
   swap + Cast stop happen even when the route isn't unselected by the user.

### Gotchas / constraints
- All MediaRouter/Cast-framework calls on the **main thread** (`AndroidCastManager` already uses a
  main `Handler`; `PlaybackService` cast work must too).
- Wrap all `RemoteMediaClient`/`getStatus`-style reads in try/catch — the cast socket can die
  synchronously (this is exactly what bit the Feishin bridge).
- Don't let any cast handoff crash `PlaybackService` (`switchSessionPlayer` already swallows exceptions
  — keep that discipline in new paths).
- Keep the `remotePlayer` (hub-remote facade) swap logic untouched; the Cast swap and the remote-facade
  swap are independent — make sure they don't both try to own `session.player` at once (Cast to a TV
  from Navic while Navic is the active hub device is the normal case; a remote-active + cast combination
  should be reasoned about explicitly).
- This is Android-only; `CastManager` (commonMain) / `NoopCastManager` (iOS) stay as-is.

### Verify (user, on-device release build)
1. Cast Navic → TV, force-stop Navic, reopen → now-playing restores the still-playing cast session
   (right song, position, playing state).
2. After that reopen, Cast → TV again works (no dead tap).
3. Select a different local song before re-casting → transfer carries the new song, not the pre-crash one.
4. Transfer back to Navic → TV stops AND Navic resumes at the correct index+position (not track 1).
