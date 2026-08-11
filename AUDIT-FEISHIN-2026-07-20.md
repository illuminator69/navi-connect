# Feishin Full Code Audit + Feishin↔Navic Feature Parity — Findings & Plan (2026-07-20)

## Context

Follow-up to the Navic audit (`AUDIT-NAVIC-2026-07-20.md`). Same treatment for Feishin
(Electron/TS/React desktop client in `feishin/`): bugs, inefficiencies, optimizations, QoL/UX —
plus a **two-way feature-parity plan** (port Navic-only features to Feishin and Feishin-only
features to Navic where they make sense per platform). Three parallel deep-dive audits were run
(renderer player/queue/hub, main process + AudioMuse/visualizer, feature-parity map).

---

## Area 1: Renderer — player / queue / hub-remote logic

### Bugs

| Sev | Location | Finding |
|---|---|---|
| HIGH | `main/features/core/hub/index.ts:84-85` + `use-hub.tsx:418-421` + `hub.store.ts:71` | Renderer never told the socket dropped — `connected` stuck `true` (store `reset` confirmed unused). `isRemoteSessionActive()` (`remote-queue.ts:96`) stays true; all transport routes `act` frames into a dead socket (`send()` silently drops, `hub/index.ts:46`); player bar frozen on stale remote state. Fix: emit synthetic `{t:'disconnected'}` on ws close/error; `use-hub` sets `connected:false, activeDeviceId:null`. |
| HIGH | `use-hub.tsx:413-461` | Hub `error` frame (`target_offline`, `auth`, …) unhandled — transfers to offline devices / bad token give zero feedback; picker `transfer()` (`hub-device-picker.tsx:51`) has no pending/failure state. Fix: handle `error` → toast. |
| HIGH | `use-hub.tsx:294,160-170,468,487` | `report`-echo after `do:load`: `setQueue` fires `onCurrentSongChange` → `publishQueue` (we're now active, `lastQueueSig` not updated in load branch) → sends `act:setQueue` back with the *previous* track's `positionMs` — can overwrite the resumed position. Fix: set `lastQueueSig` in the load branch and/or gate on `hubDrivenUntil`. |
| MED | `player-context.tsx:243-246` + `remote-queue.ts:120` | `Play.NOW` on a specific track while remote drops `playSongId`; `enqueueToRemote` always sends `index:0` → remote starts from track 1 instead of the clicked song. Fix: thread song → index into `setQueue.index`. |
| MED | `use-hub.tsx:88-97` | Receiver never emits `report{ended:true}` (protocol §5.3) — hub can't distinguish "paused at last track" from "finished". Fix: detect last-track-end and report it. |
| MED | `player-context.tsx:797-809` | Remote queue reorder handles only single-item moves; multi-select drag silently no-ops; indices read from a queue a concurrent frame can shift. Fix: loop moves with offset adjustment, or toast unsupported. |
| LOW | `preload/hub.ts:9,31-33` | `removeAllListeners('hub-message')` clobbers any second listener; `onMessage` stacks raw `ipcRenderer.on`s. Fix: track and remove the specific handler. |

### Inefficiencies

| Sev | Location | Finding |
|---|---|---|
| MED | `use-hub.tsx:490-502` | Progress-path `report()` unthrottled — fires at the audio engine's event rate (several/s) despite the ~1 Hz contract; multiplies hub traffic + rebroadcasts. Fix: ≥1000 ms throttle on the progress path only. |
| LOW | `use-remote-aware.ts:159-163` | 250 ms forced re-render of every consumer while remote-playing; 500 ms or rAF-gated tick would halve it. |
| LOW | `use-hub.tsx:113-152` | `buildHubTracks` re-resolves `getStreamUrl` for all N tracks on each queue-membership change, no cache. Fix: memoize by track id. |

### QoL / UX

| Sev | Finding |
|---|---|
| MED | **No queue undo** (Navic has it): `clearQueue`/`clearSelected` (`player-context.tsx:561-579`) irreversible local or remote. Parity item — snapshot + toast-action undo. |
| MED | No optimistic remote scrub: `useRemoteSeek` (`use-remote-aware.ts:186-193`) doesn't update `remotePositionMs/At` → thumb snaps back until next 1 Hz frame. |
| LOW | Device picker missing the remote volume slider README claims; no in-flight/failure state on transfer (`hub-device-picker.tsx:51-53,68-106`). |
| LOW | Any "connected" indicator built on `useHubConnected()` misreports after a drop (consequence of HIGH #1). |

Verified correct (don't re-flag): `subscribeCurrentTrack` `_uniqueId` equality (no per-render storm); `usePlayerActions` `useShallow` stability; `resolveSongs` placeholder synthesis; remote `move` pop-then-insert offset math.

---

## Area 2: Main process — cast bridge, hub transport, AudioMuse proxy; Auto DJ / sonic / visualizer

### Bugs

| Sev | Location | Finding |
|---|---|---|
| HIGH | `hub/index.ts:81-88` | Main forwards only `hub-message`; `open/close/error` never reach the renderer — root cause of Area 1's stuck-`connected` bug; local playback stays silenced by the runaway watchdog (`use-hub.tsx:497`) after any hub blip. Fix: IPC status events on open/close. |
| HIGH | `hub/index.ts:60-88` + `cast/index.ts:113-159` | No WS ping/pong keepalive (protocol §3 mandates it) — half-open sockets never detected; bridge/client sits dead with `readyState===OPEN`. Fix: heartbeat + force-terminate on missed pongs. |
| MED | `hub/index.ts:28,51-58` + `cast/index.ts:46,252-259` | Fixed 3 s / 5 s reconnect, no backoff — every client + every cast bridge hammers a down hub indefinitely. Fix: capped exponential backoff with jitter. |
| MED | `cast/index.ts:670-681,86` | Bridge pins the Chromecast's first-seen IP; on DHCP/band change the `up` handler early-returns on existing id and the bridge can never reconnect — virtual receiver permanently dead-but-registered. Fix: on `up` with differing host, update host + reconnect (or recreate). |
| MED | `cast/index.ts:643-653` | `device.id = txt.id \|\| service.name` — inconsistent TXT records register the same physical device twice (two 📺 entries, two hub sockets). Fix: stable id derivation / key by host. |
| MED | `cast/index.ts:161-236,553` | `handleDo` has no `setRepeat`/`setShuffle` case — Chromecast always stops at queue end, ignoring repeat modes set by any controller. Fix: track modes locally, honor in the FINISHED branch. |
| MED | `cast/index.ts:547-552,578` + `use-hub.tsx:490-502` | Double/high-rate `report`s: cast emits from both the status event and the 1 s ticker; renderer reports on every progress tick. Fix: ~1 Hz throttle on change. |
| MED | `use-hub.tsx:102-111,147` + `remote-queue.ts:22-30` + `cast/index.ts:535-540` | Unset "Public server URL" silently hands the Chromecast unreachable LAN URLs — device "connects" but never plays; only a logged IDLE/ERROR. Fix: surface cast load errors + warn when casting without a public URL. |
| LOW | `use-mood-flow-signals.ts:41-55` | Final track before stop/queue-end never produces a mood signal (only fires on next song change); first song has no previous ref. Fix: flush on stop/queue-end. |
| LOW | `hub/index.ts:64-66,85-87` + `cast/index.ts` (many `catch {}`) | Connect failures swallowed with no breadcrumb. Fix: debug-level logs. |

### Inefficiencies

| Sev | Location | Finding |
|---|---|---|
| MED | `use-hub.tsx:113-152` | Any genuine queue change rebuilds all N tracks' stream URLs (dup of Area 1 — memoize per track id). |
| MED | `use-remote-aware.ts:157-163` | 4 Hz forced re-render of all remote-aware consumers (dup of Area 1). |
| LOW | `cast/index.ts:566-599` | 1 Hz `getStatus` poll on top of pushed status events — position captured twice. |
| LOW | `hub/index.ts:121` | `hubEvents.emit('settings')` on every identical re-push (cast side already dedupes). |

### QoL / UX + known-open-item hook points

| Sev | Finding |
|---|---|
| MED | No hub connection-status indicator anywhere (depends on the open/close IPC fix). Hook: status badge by the device picker. |
| MED | No cast device management/error surfacing: load errors, adoption timeouts, dead-socket drops are log-only; no rescan trigger; no way to clear a stranded bridge. |
| MED | **README open item confirmed — character params unwired**: main handler supports `temperature`/`subtract_distance` (`audiomuse/index.ts:13-21,105-107`) but `fetchAlchemyIds` (`audio-muse-source.ts:40-58`) and `use-auto-dj.ts:84-89` never send them; no re-splice loop. Hook: pass params from an autoDJ setting through preload; add re-centroid loop as signals accumulate. |
| MED | **README open item confirmed — mood palette has no consumer**: `audiomuse-track-mood` main handler + `fetchTrackMood` exist but nothing calls them. (Note: the "blob visualizer" exists at `features/visualizer/components/visualizer.tsx`, not in a `blob/` folder — its palette is a genre-keyword heuristic, not mood-driven.) |
| LOW | Mood Flow silently degrades to single-seed "similar" on remote/cold-start; generator chip could show a "warming up" state. |

---

## Area 3: Feature parity map (both directions, code-verified)

> Correction to `AUDIT-NAVIC-2026-07-20.md` item 21: CLAP mood search is **no longer dead code** in
> Navic — wired via `MoodSearchSheet.kt` into `SearchScreen.kt:155/240/507`. Update the audit doc.

### Navic → Feishin (Navic has, Feishin lacks)

| Feature | Navic implementation | Feishin status | Port cost |
|---|---|---|---|
| **Queue undo** (clear/remove/move/play-now-replace, local+remote) | `MediaPlayer.kt` (`QueueUndoSnapshot`, 6 s expire) + `QueueScreen.kt` snackbar | Absent | Med — snapshot stack in player store + toast action; remote restore via `remote-queue.ts` |
| **Saved queues / session-typed history** (+ restore/resume/save-as-playlist) | `ui/screens/savedqueues/*`, `SavedQueueEntity.kt` (`sourceKind`), `SavedQueueRepository.kt` | Absent | High — needs persistence layer + session-kind stamping + screen |
| **Continue Listening row** (resume at playhead) | `ContinueListeningCard.kt` + `LibraryScreen.kt` | Partial (RECENTLY_PLAYED albums carousel, no positional resume) | Depends on saved queues |
| **Mood Flow adaptive depth + character presets** (Echo/Steady/Transition actually bias alchemy params) | `RadioManager.playMoodMix`, `MoodCharacter.kt` | Partial — signals + dropdown exist (`auto-dj/*`), re-splice loop + char-param wiring unbuilt (known open item) | Med — engine wiring exists |
| **4-way autoplay control** (Off/Similar/Fingerprint/Adaptive, capability-greyed) | `AutoplayMode.kt` + `NowPlayingAutoplaySelector.kt` | Partial (source dropdown, no gating UI) | Low-med |
| Offline downloads / Download Center / constraints | `DownloadManager.kt` + `DownloadCenterScreen.kt` etc. | Absent | Very high — **out of scope** (user decision) |

### Feishin → Navic (Feishin has, Navic lacks)

| Feature | Feishin implementation | Navic status | Port cost |
|---|---|---|---|
| **Audio-reactive visualizer** | `features/visualizer/` (Butterchurn, audioMotion, blob + palette) | Absent (BlendBackground is ambient only; AdaptiveMoodBackground dead) | High — **out of scope** (user decision) |
| **Tag editor** | `features/tag-editor/*` | Absent | Med — **out of scope** (user decision) |
| **Music-folder browsing** | `features/folders/*` | Absent | Low-med — Subsonic `getMusicFolders` + list screen |
| **Recently-added *songs* home row** | `HomeItem.RECENTLY_ADDED` in `home-route.tsx` | Absent (albums only; known deferred item) | Low-med |
| **Metered/cellular transcode profile (user-facing)** | `use-effective-transcode.ts` + `transcode-settings.tsx` | Partial — reacts to `isCellular` internally, no settings toggle | Low |
| Custom user themes | `custom-themes/*` | Different philosophy (cover-driven dynamic theming) — skip | — |
| Discord Rich Presence | `use-discord-rpc.ts` | Platform-inappropriate on Android — skip | — |

### Both, different backends (not gaps)
Smart playlists (Feishin client-side query builder vs Navic native `rules` API); lyrics; replay gain; sleep timer; Tier-1 radio trio (Instant Mix / Artist Radio / Song Journey); CLAP search; generator chip; ratings/favorites; transfer; system media controls.

---

## Proposed plan (prioritized)

Scope decisions (user): of the large parity ports, only **Saved queues → Feishin** is in scope.
Visualizer→Navic, Downloads→Feishin, and Tag editor→Navic are explicitly **out of scope**.
The mood palette lands as a **separate visualizer type**, not a modification of the built-ins.

### Phase 1 — Feishin connection correctness (high severity)
1. **Hub disconnect visibility** (`main/features/core/hub/index.ts` + `preload/hub.ts` + `use-hub.tsx` + `hub.store.ts`): emit IPC status on ws `open`/`close`/`error`; renderer sets `connected:false, activeDeviceId:null` on close so `isRemoteSessionActive()` releases the player bar. Add a connection badge near the device picker.
2. **WS keepalive** in both the hub client (`hub/index.ts`) and cast bridge (`cast/index.ts`): heartbeat ping/pong, force-terminate on missed pongs (protocol §3).
3. **Exponential backoff with jitter** for both reconnect loops (replace fixed 3 s / 5 s).
4. **Handle hub `error` frames** in `use-hub.tsx` → toast (`target_offline`, `auth`, …); pending/failure state on picker `transfer()`.
5. **Fix the `do:load` report echo** (`use-hub.tsx:294,160-170`): update `lastQueueSig` in the load branch + gate `publishQueue` on `hubDrivenUntil`.

### Phase 2 — Feishin medium bugs & perf
6. `Play.NOW` while remote: thread `playSongId` → `setQueue.index` (`player-context.tsx:243-246`, `remote-queue.ts:120`).
7. Emit `report{ended:true}` at queue end (`use-hub.tsx`).
8. Remote multi-item queue move: loop with offset adjustment (`player-context.tsx:797-809`).
9. Throttle progress-path `report()` to ~1 Hz; dedupe cast's double report (status event + ticker) (`use-hub.tsx:490-502`, `cast/index.ts:547-578`).
10. Cast bridge: handle IP change on mDNS `up` (update host + reconnect); stable device id (avoid `txt.id||name` duplicates); honor repeat mode at FINISHED; surface cast load errors + warn when Public server URL unset.
11. Preload `onMessage`: per-listener unsubscribe instead of `removeAllListeners`.
12. Memoize `buildHubTracks` stream/image URLs by track id; optimistic remote scrub (`useRemoteSeek` updates `remotePositionMs/At`); mood-signal flush on stop/queue-end; debug-log swallowed connect errors.

### Phase 3 — Parity: Feishin ← Navic
13. **Queue undo** (mirror Navic's `QueueUndoSnapshot` + 6 s expire): snapshot stack in player store, toast with Undo action for clear/remove/move/play-now-replace, remote-aware restore via `remote-queue.ts`.
14. **Mood Flow depth** (closes README open item): pass `temperature`/`subtract_distance` from Echo/Steady/Transition presets through `use-auto-dj.ts` → `audio-muse-source.ts` → existing main handler (`audiomuse/index.ts:105-107`); add the re-splice/re-centroid loop as signals accumulate.
15. **Mood Flow visualizer as a separate visualizer type** (closes README open item; do NOT modify butterchurn/audiomotion): add a new `moodflow` option to `visualizer.type` (settings + the type switch in `full-screen-visualizer.tsx:161-165` and the sidebar `VisualizerPanel` in `sidebar-play-queue.tsx:155`). New component under `features/visualizer/components/moodflow/` reusing the existing blob-canvas renderer (`features/visualizer/components/visualizer.tsx`) but with its palette driven by a new `use-track-mood.ts` (consuming the already-built `fetchTrackMood`, `audio-muse-source.ts:60-67`) + `mood-centroid.store`, replacing the genre-keyword `paletteFromSong` heuristic. Built-in visualizers stay untouched.
16. **Glassy-theme bugfix — visualizer bleeding through the player bar**: `full-screen-visualizer.tsx` renders inside `main-content` (`main-content.tsx:209`) but its motion variants size it with viewport math (`height: calc(100vh - 90/120px)`, `full-screen-visualizer.tsx:33-36,65-68`), overflowing main-content's bottom edge under the player bar; the Haze frosted bar (`player-bar.module.css:16-21`) is translucent so the canvas shows through. Fix: size the overlay to its container (`height: 100%` / `inset: 0`; animate with `y`/percent transforms instead of `top: 100vh`), verifying the open/close slide animation in both windowBar styles.
17. **4-way autoplay control**: single Off/Similar/Fingerprint/Adaptive selector with capability-gated greying (port `AutoplayMode.kt` UX to the autoplay dropdown).
18. **Saved queues + Continue Listening** (the approved large port): persistence layer (electron-store JSON or main-process sqlite), session-kind stamping at radio/journey/moodFlow mint sites, saved-queues view + resume-at-position, Continue Listening card on home. Model on Navic's `SavedQueueRepository.kt`/`SavedQueueSource.kt`.
19. Remote volume slider in the device picker (README claims it; missing).

### Phase 4 — Parity: Navic ← Feishin
20. **Recently-added songs home row** (also a deferred Navic item): new query + `horizontalSection` in `library/components/Content.kt` (mirror Feishin's `HomeItem.RECENTLY_ADDED`).
21. **User-facing cellular transcode profile**: settings toggle over Navic's existing `isCellular` reactivity (mirror `use-effective-transcode.ts` semantics).
22. **Music-folder browsing**: Subsonic `getMusicFolders` + list screen (mirror `features/folders/*`).

### Deliverables
- Correct `AUDIT-NAVIC-2026-07-20.md` item 21: CLAP mood search is now wired (`MoodSearchSheet.kt` → `SearchScreen.kt`).
- Commit Feishin changes in its repo; Navic changes in the navic repo.

## Verification
- **Feishin**: typecheck via `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`) — NOT `pnpm run typecheck` (breaks lockfile). Run `pnpm dev` against the live hub.
- **Navic**: build `:androidApp:assembleRelease` outside the sandbox (no JDK here); commonMain must still compile for iOS.
- Manual flows: kill the hub mid-session (bar must unfreeze + badge go red, backoff not hammer); transfer to an offline device (toast); double-click a mid-album track while remote (starts on that track); queue-end on Chromecast with repeat-all; DHCP-style cast IP change (re-register); undo after remote clear; Mood Flow with each character preset (params visible in hub/audiomuse logs); saved-queue resume at position on desktop; open the full-screen visualizer under a glassy/Haze theme and confirm nothing renders through the player bar (both windowBar styles); select the new Mood Flow visualizer type and confirm butterchurn/audiomotion are unchanged.
