# Remaining / untouched after 2026-07-20 audit-fix session

Companion to `SESSION-2026-07-20-audit-fixes.md`. Everything below was **not**
implemented, with the reason. Grouped by repo, then by why it was skipped.
Line refs are from the audit docs (may have drifted after this session's edits —
re-locate by symbol, not line number).

## ⚠️ First, before trusting anything

- **navic/ `:androidApp:assembleRelease` now PASSES** (JDK 21 at
  `C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot` + gradlew work in this
  environment — the old "no sandbox compile" note is obsolete). The **iOS
  targets were not built**, though — run an iOS/`commonMain` compile before
  trusting the shared code on Apple platforms. Commit is `bbc1f51`.
- **feishin/ typechecks but was not dev-run.** `pnpm dev` against a live hub and
  walk the manual flows in the audit's Verification section (kill hub mid-session,
  transfer to offline device, double-click mid-album track while remote, etc.).
- **navic/ and feishin/ commits are NOT pushed.** hub/ is pushed. Decide on push.
- Verify commands: feishin `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`) — **NOT** `pnpm run typecheck` (breaks the lockfile).

## Doc correction owed (from the Feishin audit, still unapplied)

- `AUDIT-NAVIC-2026-07-20.md` **item 21 is stale**: CLAP mood search is *no longer
  dead code* in Navic — it's wired via `MoodSearchSheet.kt` → `SearchScreen.kt`.
  Update that audit doc's "wire up the dead CLAP mood-search code" line.

---

## hub/ — deferred (all are Area 6 *features*, not bugs)

The bug/robustness findings are all done. Untouched, by value-to-effort:
- Health endpoint + Dockerfile `HEALTHCHECK` (S)
- Device auto-prune on load, `HUB_DEVICE_TTL_DAYS` (S)
- Sleep timer `act` (S)
- **ListenBrainz scrobbling** — highest-value given `lb-bot` already exists (M)
- Recently-played / history ring buffer (M)
- Server-authoritative favorite/rating broadcast (M)
- Graceful takeover / "who's controlling" broadcast (M)
- Multi-zone / rooms (L)
- Tiny LOW ops items: offload `_save` to a thread / debounce; trap SIGTERM for a
  final flush; friendly error when a `setQueue` exceeds `max_size`.

---

## navic/ — deferred

### Skipped because RISKY to do blind (no compiler this session)
- **`DownloadManager` dedup / retry-cap / bulk-query** (audit Area 3 MED, #12):
  check-and-insert under one lock (TOCTOU); exclude FAILED with `retryCount ≥ N`
  in `PlaylistDownloadManager.syncAll` re-queue; bulk `getSongIdsByStatus()`
  instead of per-song point queries; conflated channel for progress writes.
- **`hubDrivenUntilMs` → load-generation token** (`HubManager`, #9). Subtle
  concurrency; a wrong version can break the working remote flow.
- **`applySession` queue diffing + mirror by-reference sig cache** (#13).
- **`SavedQueueRepository` cache synchronization** (unsynced `cachedId/…` mutated
  by two concurrent uiState collectors, #25 MED).

### Skipped because of MIGRATION / VISUAL risk (needs a device)
- **Drop `DownloadEntity` from `CacheDatabase`** (#15). This is a **Room schema
  change** — needs a version bump + migration; the file's own comment warns of the
  identity-hash launch crash. Do it with a proper migration, not a blind removal.
- **`DownloadCenterScreen` → single `LazyColumn`** (Area 3 HIGH). Risks the `Form`
  rounded-card grouping; the dominant runtime cost (per-tick DB re-query +
  whole-library re-sort) is **already fixed** in `DownloadCenterViewModel`, so this
  is now just the completed-section compose-at-rest cost.

### Skipped as LOW / polish
- `HubManager` 250 ms mirror loop wakes even when local (gate on `isRemoteActive`).
- Controller setup blocks on `downloadManager.allDownloads.first()` on the critical path.
- `playNextSingleLocal` with `currentIndex == -1` on a non-empty queue (semantics ambiguous).
- `RemoteSessionPlayer` 1500 ms activation grace could be narrowed.
- `RadioManager`/`RelatedSongs` collapse fetch errors to `Success(empty)` (offline vs empty).
- `retryDownload` double-inserts a QUEUED row (harmless).
- `NowPlayingAutoplaySelector` `remember(isConfigured)` keys on a non-reactive getter.

### Enabling half done, UI not wired
- **`connectionError` StateFlow was added to `HubManager` but nothing consumes it.**
  Next step (audit Phase 3 #16): observe it in `DevicePickerSheet` /
  `NaviConnectScreen` → snackbar, plus a connection-status badge.

### Phase 3/4 (QoL + parity — all features, out of scope this session)
- Tier-2 autoplay reachability greying; concurrency-stepper "applies after restart"
  hint; queue-undo single-slot simplify; deferred features (downloaded-only filter
  via `getSongIdsByStatus()`, alphabet fast-scroll, recently-added songs home row).
- Navic ← Feishin: recently-added songs row, user-facing cellular transcode toggle,
  music-folder browsing (`getMusicFolders`).

---

## feishin/ — deferred

### Skipped for a concrete reason
- **`report{ended:true}` at queue end** (Area 1 MED, #7). No player `ended` /
  `queue-end` event exists in `use-player-events` to hook; inferring it from
  status→PAUSED at the last index is fragile. Needs a real end-of-queue event
  (or a deliberate heuristic) first.
- **Cast bridge behaviour fixes** (Area 2 MED, #10) — Chromecast-specific, can't
  verify without a device. Keepalive + backoff were done; still open:
  - IP change on mDNS `up` (update host + reconnect; the `up` handler early-returns
    on an existing id → bridge stays dead-but-registered after DHCP/band change).
  - Stable device id (`txt.id || service.name` registers one device twice).
  - Honor repeat mode in the `FINISHED` branch (track `repeat`/`shuffle` locally,
    also from session frames / `setRepeat` do-commands).
  - Surface cast load errors + warn when "Public server URL" is unset (device
    "connects" but silently never plays with LAN URLs).

### Actual bug still open (Phase 3 list but it's a fix, not a feature)
- **Glassy/Haze theme: full-screen visualizer bleeds through the player bar**
  (#16). `full-screen-visualizer.tsx` sizes itself with viewport math
  (`calc(100vh - 90/120px)`) inside `main-content`, overflowing under the
  translucent Haze bar. Fix: size the overlay to its container (`height:100%` /
  `inset:0`; animate with `y`/percent instead of `top:100vh`), verify the
  open/close slide in both windowBar styles.

### Skipped as LOW
- Mood-signal flush on stop/queue-end (`use-mood-flow-signals.ts` — last track
  before stop never produces a signal).
- `use-remote-aware.ts` 250 ms forced re-render → 500 ms or rAF-gated.
- Debug-log the swallowed connect errors (`hub/index.ts`, cast `catch {}`s).
- `hubEvents.emit('settings')` on every identical re-push; cast 1 Hz `getStatus`
  poll duplicates pushed status.
- Device-picker: remote volume slider the README claims; in-flight/failure state
  on `transfer()`; connection-status badge (the disconnect IPC it needs is now done).

### Phase 3/4 (feature parity — out of scope)
- Queue undo (mirror Navic's `QueueUndoSnapshot`).
- Mood Flow depth (wire `temperature`/`subtract_distance` presets through
  `use-auto-dj.ts` → `audio-muse-source.ts` → the existing main handler; add the
  re-splice/re-centroid loop) — **closes a README open item**.
- Mood Flow visualizer as a **separate** `moodflow` visualizer type (do NOT modify
  butterchurn/audiomotion) — **closes a README open item**.
- 4-way autoplay control (Off/Similar/Fingerprint/Adaptive, capability-greyed).
- **Saved queues + Continue Listening** (the one approved large port) — persistence
  layer + session-kind stamping + saved-queues view + resume-at-position, modelled
  on Navic's `SavedQueueRepository.kt` / `SavedQueueSource.kt`.

### Explicitly OUT OF SCOPE (user decision, per the Feishin audit)
- Downloads → Feishin; Visualizer → Navic; Tag editor → Navic.
