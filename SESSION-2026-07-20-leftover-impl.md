# Session summary — leftover-items implementation (2026-07-20)

Follow-up to `SESSION-2026-07-20-audit-remaining.md`. Implemented the **bugs + cheap
wins**, plus one approved large feature (**Feishin Saved Queues + Continue Listening**).
Plan: `~/.claude/plans/in-the-root-folder-velvet-elephant.md`.

**Nothing is committed** — all changes are uncommitted in the working trees (per the
decision to leave the prior session's commits unpushed and stack locally). Decide on
commit/push separately.

| Area | What | Verified? |
|---|---|---|
| feishin Haze visualizer | container-relative sizing fix | typecheck clean (web+node); **not dev-run** |
| navic downloads | retry cap + dedup TOCTOU + bulk query | `:androidApp:assembleRelease` **PASSES** |
| navic connectionError | snackbar + inline badge | same release build passes |
| feishin Mood Flow depth | temp/subtract_distance + re-splice + preset UI | typecheck clean; **not dev-run** |
| doc fix | AUDIT-NAVIC item 21 CLAP correction | n/a |
| hub ops | health endpoint, TTL prune, SIGTERM flush | `tools/test_edits.py` PASSES (incl. new prune test); health + SIGTERM manually verified |
| feishin Saved Queues | store + capture + Continue Listening + save-as-playlist | typecheck clean; **not dev-run** |

---

## Phase 1 — bugs

**feishin glassy-theme side-queue visualizer bleed + panel gap** — `glassy-dark/glassy_overrides.css`.
Diagnosed live in the running app via Chrome DevTools (not by guessing). Root cause: the glassy theme
stretches the side queue to full viewport height (its `.main-content-body` is forced to `100vh`), so
the queue's `react-split-pane` group ran behind the translucent 90px player bar. The theme tried to
compensate with per-section `position: relative; bottom: 90px` hacks — which lifted only the lyrics
panel (leaving a 90px **gap** below it) and missed the visualizer panel (which **bled** under the
bar). Fix: reserve the bar height on the panel container so the whole split-pane group is
bar-excluded and every pane reflows to fit — `#sidebar-play-queue-container { box-sizing: border-box;
padding-bottom: 90px; }` — and removed the `bottom: 90px` lyrics hack. Verified in-browser: split-pane
group ends exactly at the bar (726px), lyrics/visualizer panels contiguous, waveform stays above the
bar while playing. *(Earlier guesses at `full-screen-visualizer.tsx` and the full-screen-player glassy
override were the wrong surfaces and were reverted — the audit's "#16 full-screen-visualizer.tsx" note
mis-attributed it; the real surface is the side queue.)*

**navic downloads** (`DownloadManager.kt`, `PlaylistDownloadManager.kt`) —
- **Retry storm fixed**: `syncPlaylist` now excludes FAILED rows at/above
  `MAX_DOWNLOAD_RETRIES` (3), and re-attempts of failed rows count toward the cap via a new
  `downloadSong(..., incrementRetry)` param. Previously a permanently-failing track was
  re-queued on every 6h sync forever (retryCount never advanced on the auto path).
- **Dedup TOCTOU fixed**: the "already active?" check and the `activeDownloads[id]` claim are
  now one atomic `withLock`.
- `retryDownload` delegates to `downloadSong` (removes the redundant double-insert).
- `downloadSongs`/`downloadEntireLibrary` use one bulk `getSongIdsByStatus(DOWNLOADED)`
  instead of per-song point queries.

## Phase 2 — cheap wins

**navic connectionError consumer** — `HubManager.connectionError` was produced but unconsumed.
`NaviConnectScreen` now shows a snackbar on a new error + an inline error-colored status line;
`DevicePickerSheet` shows the error in place of "Not connected to the hub".

**feishin Mood Flow depth** — the main/preload already accepted `temperature`/`subtract_distance`;
only the renderer omitted them. `audio-muse-source.ts` gained `moodCharacter` presets
(Echo/Steady/Transition, mirroring Navic's `MoodCharacter.kt`) threaded through `fetchAlchemyIds`;
`use-auto-dj.ts` now does a **bounded re-splice loop** (`fetchMoodFlowIds`, ≤3 passes, temperature
widening on thin harvests) instead of a one-shot top-up; a "Character" selector appears under the
Mood Flow autoplay source in `right-controls.tsx`. New setting `autoDJ.moodCharacter` (default
`steady`). Closes the README Mood Flow re-splice/character-param open item.

**doc fix** — `AUDIT-NAVIC-2026-07-20.md` item 21 corrected: CLAP mood search is wired
(`MoodSearchSheet.kt` → `SearchScreen.kt`), not dead code.

**hub ops** (`hub.py`, `Dockerfile`, `.env.example`, `tools/test_edits.py`) —
- Plain-HTTP **health endpoint** (default `HUB_HEALTH_PORT` = `HUB_PORT`+1) returning status JSON;
  `Dockerfile` `HEALTHCHECK` probes it via python (no curl in the slim image).
- **Device auto-prune on load** via `HUB_DEVICE_TTL_DAYS` (0 = keep forever).
- **SIGTERM/SIGINT graceful flush**: stops serving, `_save()`s, lets a pending mirror write finish.
- New prune regression test.

## Phase 3 — feishin Saved Queues + Continue Listening (large)

Greenfield, mirroring Navic's `SavedQueueRepository` model. Purely local (persisted renderer store;
hub not involved).
- `store/saved-queues.store.ts` — persisted rolling store (cap 20, evict oldest by `updatedAt`),
  `SavedQueueKind` = album/playlist/radio/moodFlow/journey/manual, cheap cursor-only progress path.
- `features/player/utils/saved-queue-source.ts` — best-effort session-kind stamping
  (`markNextQueueSource` hint + album-vs-manual inference).
- `features/player/hooks/use-saved-queues.ts` — `SavedQueuesCaptureHook` (debounced capture on
  queue/song/index change + throttled position writes; re-adopts an identical queue instead of
  duplicating across restarts) and `useRestoreSavedQueue` (setQueue at index/position + restore
  shuffle/repeat + resume playback). Mounted in `audio-players.tsx`.
- `features/home/components/continue-listening-carousel.tsx` — home row of cards (tap to resume);
  per-card menu: Resume / Save as playlist (reuses `SaveAsPlaylistForm` + `addToPlaylist`) / Remove.

### Deferred within Phase 3 (not blocking)
- A dedicated full-screen **saved-queues management view** and **rename** UI (store supports rename;
  only the Continue Listening row + card menu are wired). Per the plan's time-box, the persistence +
  Continue Listening + resume + save-as-playlist core landed first.
- Source-kind stamping is inference-based (album/manual) except where `markNextQueueSource` is called;
  radio/journey/moodFlow call sites are not yet stamped (they still capture, as manual/album).

## Still deferred from the audit (unchanged, see `SESSION-2026-07-20-audit-remaining.md`)
Cast-bridge fixes (need a Chromecast), navic `DownloadEntity` Room migration + `DownloadCenter`
single-LazyColumn (migration/device risk), feishin `report{ended}` (no event to hook), and the
lower-value parity items (ListenBrainz scrobbling, queue undo, 4-way autoplay, etc.).

## Verification commands
- hub: `cd hub && python tools/test_edits.py`
- navic: `set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot & .\gradlew :androidApp:assembleRelease`
- feishin: `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`) — **NOT** `pnpm run typecheck`.
- feishin **still owed a `pnpm dev` walk-through** (Haze overlay in both windowBar styles; Mood Flow character preset carrying params + re-splicing; Continue Listening capture/restore/eviction).
