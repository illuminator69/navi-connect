# Session — Hub-managed saved-queue history + Navic queue-sheet fixes (2026-07-26)

Made the **saved-queue history (Continue Listening) hub-owned and shared** across both clients, fixed
capture on both, and cleaned up the Navic queue sheet. Also fixed an earlier remote-unpause loop.
Plan: `~/.claude/plans/navic-still-doesnt-save-expressive-lantern.md`.

## Verification status
| Area | Verified? |
|---|---|
| Hub (`hub.py`) | `python tools/test_edits.py` **PASSES** (3/3, incl. new saved-queue test) |
| Feishin (renderer + main) | `tsc --noEmit` **clean** on `tsconfig.web.json` + `tsconfig.node.json`; **not dev-run** |
| Navic | `:androidApp:assembleRelease` **BUILD SUCCESSFUL** (compile+R8+package; `lintVitalRelease` skipped — slow linter, not compilation); **not device-tested** |
| End-to-end over the wire | **NOT tested** — needs live hub + both clients (see "To verify") |

---

## 1. Remote unpause loop (earlier fix)

**Bug:** remote unpause didn't stick — playback resumed then rewound to 0, on a ~2 s cadence.
**Cause:** `RemoteSessionPlayer` (media3 facade for the Android system transport) forwarded the system
controller's re-issued `COMMAND_SEEK_TO_MEDIA_ITEM` at the *current* index as `actJump(idx)`; the hub's
`jump` resets `position=0` and restarts the receiver.
**Fix:** only forward the jump when the target index differs from `remoteSession.value.index` (mirrors
the `ArtworkPager` self-jump guard).
- `navic/composeApp/src/androidMain/.../shared/RemoteSessionPlayer.kt`

## 2. Auto DJ top-up: earlier local fixes (superseded/kept as offline path)

Before the hub redesign, two local fixes landed and remain as the **offline** path:
- Feishin saved-queue capture no longer forks a new entry on append (prefix-continuation → update in
  place): `feishin/src/renderer/features/player/hooks/use-saved-queues.ts`.
- Feishin publishes the queue to the hub on **membership** changes (not just song/status), so a mid-track
  transfer carries a topped-up queue: `feishin/src/renderer/features/hub/hooks/use-hub.tsx`.

---

## 3. Hub-managed saved-queue history (the main change)

### Design
The hub is now authoritative for a rolling, capped-20 saved-queue history:
- The live `Session` carries `savedQueueId` / `sourceKind` / `sourceName`; the record with
  `id == session.savedQueueId` is the **current** queue (clients highlight it "Now Playing", first).
- `setQueue` upserts a record from the live queue (client sends a stable id; hub mints if absent).
  `enqueue` / `remove` / `move` grow/edit that **same** record — so a top-up updates the current queue
  instead of forking. Kind/name are set once (when the queue is born) so re-publishes don't clobber them.
- Broadcast `savedQueues { queues:[…] }` on any change (also embedded in `welcome`).
- New acts: `renameSavedQueue`, `deleteSavedQueue`, `syncSavedQueues` (client pushes its local/offline
  history up; hub **union-merges** newest-wins, caps, rebroadcasts).
- Each client keeps its existing local store as an **offline cache**; when connected, the hub list is
  authoritative (adopted as a **replace**, so deletes propagate). On (re)connect a client sends its local
  rows up *before* adopting, so offline-captured queues survive.

### Wire protocol
Documented in `PROTOCOL.md` §5 (new frames), §8.3 (full saved-queue-history model). `setQueue` gains
optional `savedQueueId` / `sourceKind` / `sourceName` / `serverId` (backward compatible).

---

## Files touched

### Hub (Python)
- `hub/hub.py` — `Session` gains `saved_queue_id`/`source_kind`/`source_name`; `self.saved_queues` dict
  + `_upsert_saved_queue` / `_sync_current_saved_queue` / `_touch_saved_queue_progress` /
  `_merge_saved_queues` / `_evict_saved_queues` / `_saved_queues_list` / `_broadcast_saved_queues` /
  `_mint_saved_queue_id`; wired into `setQueue`/`enqueue`/`remove`/`move`, `welcome`, `_on_report`,
  `_save`/`_load`; new acts `renameSavedQueue`/`deleteSavedQueue`/`syncSavedQueues`.
- `hub/tools/test_edits.py` — `Client` captures `savedQueues`/`savedQueueId`; new `test_saved_queues`
  (record / enqueue-grow / second-record / merge / delete / rename).
- `PROTOCOL.md` — §5 act+broadcast tables, new §8.3.

### Feishin (renderer + main)
- `src/renderer/store/hub.store.ts` — `savedQueueId` field + `useHubSavedQueueId`.
- `src/renderer/store/saved-queues.store.ts` — `mergeFromHub` (replace-from-hub, preserves local names).
- `src/renderer/features/hub/hooks/use-hub.tsx` — handle `savedQueues` frame → store; parse
  `savedQueueId` into hub store; on `welcome` sync local rows up then adopt; `resolveSavedQueueId`
  (stable-across-append id, reuse hub's current id on adopt) threaded into `publishQueue` +
  `routeLocalPlayToRemote`; `mapHubSavedQueue` / `savedQueueToHubRecord` mappers.
- `src/renderer/features/hub/utils/remote-queue.ts` — `sendHubAct` (fire when connected, for mgmt).
- `src/renderer/features/player/hooks/use-saved-queues.ts` — local capture gated to **offline** only.
- `src/renderer/features/home/components/continue-listening-carousel.tsx` — active-first + "Now Playing"
  highlight, hub-routed remove.
- `src/renderer/features/saved-queues/routes/saved-queues-route.tsx` — active-first + highlight,
  hub-routed rename/delete.

### Navic (Kotlin, commonMain unless noted)
- `domain/manager/HubManager.kt` — `RemoteSessionState.savedQueueId` (+ parse in `applySession`);
  `savedQueues` frame → `applySavedQueues` (resolve → Room `replaceFromHub`); `welcome` runs
  `syncLocalSavedQueuesUp` then `applySavedQueues`; `actRenameSavedQueue`/`actDeleteSavedQueue`;
  `hubSavedQueueIdFor` + `intToRepeat`; `savedQueueId`/`sourceKind`/`sourceName` threaded into the three
  `setQueue` frames (`publishQueueIfOurs`, `routeLocalPlayIfRemote`, `loadSessionQueue`); new constructor
  dep `SavedQueueRepository`. Implements `isHubConnected`.
- `domain/repositories/SavedQueueRepository.kt` — `RemoteSavedQueue` DTO, `allForSync`, `replaceFromHub`.
- `data/database/dao/SavedQueueDao.kt` — `getAll` / `clear` / `deleteNotIn`.
- `shared/MediaPlayer.kt` — `RemotePlaybackRouter.isHubConnected`; local capture gated when hub connected.
- `di/ManagerModule.kt` — pass `SavedQueueRepository` to `HubManager`.
- `ui/screens/savedqueues/SavedQueuesScreen.kt` — active id from hub session; active-first; hub-routed
  rename/delete/delete-others.
- `androidMain/.../shared/RemoteSessionPlayer.kt` — unpause self-jump guard (§1).
- `ui/screens/queue/QueueScreen.kt` — **two download buttons → one icon + dropdown**
  (`QueueDownloadMenuButton`); list is now `weight(1f)` to close the layout gap.

### Docs
- `README.md` — §4 frame table (`savedQueues`, saved-queue acts) + saved-queue-history note; §6 feature
  note updated (hub-synced, not per-client).

---

## Limitations / known edges
- **Restoring a saved queue onto a *remote* device** (`loadSessionQueue`) mints a fresh record rather
  than reusing the original id → can create a duplicate history entry. Chosen over the riskier reuse of a
  possibly-stale local id that could overwrite a *different* record.
- **Kind on remote-started mixes** (via `loadSessionQueue`) is best-effort from local state (may read
  `manual`); locally-played and appended queues get the right kind.
- **Offline→online window:** on `welcome` the client adopts the hub list (replace) before its
  `syncSavedQueues` round-trips, so offline-only rows can flash out then reappear (~sub-second). No data
  loss (rows are captured pre-replace and re-broadcast after merge).
- **Current-record cursor** is refreshed from reports (throttled, in-memory; persisted on the next
  session-change save) and flushed to the record on the next `setQueue` switch — resume granularity is
  song-level, not exact-ms, for a queue that has become "previous".
- `SavedQueueEntity` has no `serverId` (single-server setup); Navic-origin records sync up without one and
  Feishin falls back to its current server. No Room migration was needed.
- Navic `lintVitalRelease` was skipped to get a clean packaged build; it's a linter, not compilation.

## To verify (live walk-through — not yet done)
Run the hub + Feishin (`pnpm dev`) + the new Navic APK together:
1. Start an AudioMuse mix on one client → it appears in Continue Listening + Saved Queues on **both**,
   marked current. Auto DJ tops up → the **same** entry grows (no duplicate) on both.
2. Transfer between devices → history identical; the active highlight follows the session.
3. Rename / delete on one client → reflected on the other.
4. Take one client offline, start a new queue (captured locally), reconnect → the offline entry is
   preserved and now shows on both, newest-first.
5. Navic queue sheet: single download button + menu (whole queue / next 10), and confirm the layout gap
   is gone (release build — debug Compose spacing is misleading).

## Build/verify commands
- hub: `cd hub && python tools/test_edits.py`
- feishin: `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`)
- navic: `set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot & .\gradlew :androidApp:assembleRelease`
