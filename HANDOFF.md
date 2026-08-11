# navi-connect — Handoff

A Spotify-Connect–style remote-control layer over a personal **Navidrome** server.
Multiple clients act as both controllers and receivers; a central headless **hub**
holds the session intent and routes commands. Audio never flows through the hub —
each receiver streams directly from Navidrome by track id.

- **Navidrome**: https://music.example.com (public). Single user, Unraid `media` Docker network.
- **Clients (forks):** Feishin (Electron/Windows), Navic (Kotlin Multiplatform, Android only — iOS out of scope).
- **Hub:** Python asyncio/websockets, port **4790**.

Layout:
```
navi-connect/
  hub/          Python relay hub (hub.py + Dockerfile/compose + tools/)
  feishin/      Feishin fork (Electron)
  navic/        Navic fork (Kotlin Multiplatform / Compose)
  PROTOCOL.md   wire protocol
  ROADMAP-V2.md phased feature roadmap
  HANDOFF.md    this file
```

---

## Architecture

- The **hub** owns *session intent*: `queue, index, positionMs, isPlaying, activeDevice, repeat, shuffle, order`.
  It persists state (survives restart, but `activeDevice` is cleared and `isPlaying=false` on load) and
  optionally mirrors to Navidrome's native `savePlayQueue`.
- Every client connects over WS, sends a `hello` (with a persisted device id + token), gets a `welcome`
  (session snapshot + device list). Frames are plain JSON with a `t` discriminator
  (`hello/welcome/act/do/report/released/session/progress/devices/error`). See `PROTOCOL.md`.
- The **active receiver** is the source of truth for live position; it sends `report` ~1 Hz.
  Controllers send `act` (play/pause/next/previous/jump/seek/setQueue/enqueue/volume/repeat/shuffle/transfer);
  the hub applies intent and forwards `do` directives to the active receiver.
- **Transfer-with-resume:** hub sends `do:release` to the old device (which replies `released` with its
  final index+position), then `do:load` to the target with `{tracks, index, positionMs, play}`.
- **Chromecast** is bridged *by Feishin's main process* (it's not a hub client of its own): every mDNS-discovered
  cast device is registered with the hub as a virtual `receiver` (id `cast-<id>`, "📺 <name>"), so it shows up in
  every client's device picker. Audio = direct Navidrome stream URLs (must be publicly reachable — see Tailscale note).

---

## Build & run

### Hub
```
cd hub
# .env from .env.example: HUB_TOKEN, NAVIDROME_URL, HUB_MIRROR_PLAYQUEUE, HUB_ND_USER/PASS,
#                          AUDIOMUSE_URL + AUDIOMUSE_TOKEN (Tier-2 proxy)
docker compose up -d          # or: python hub.py  (Python 3.11+, `websockets`)
```
Tools (manual testing): `hub/tools/` — `fake_receiver.py`, `controller.py`, `test_transfer.py`.

### Feishin (Electron, Windows)
Requires Node 20 LTS + `corepack enable`, then `pnpm install`.
```
cd feishin
pnpm dev                                  # dev
# Production build (portable — USE THIS):
pnpm run build
pnpm exec electron-builder --win --x64 --dir   # → dist/win-unpacked/Feishin.exe
```
- **Typecheck WITHOUT triggering a deps re-check** (important — `pnpm run typecheck` re-checks deps and
  has bitten us before):
  ```
  .\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json  --composite false   # renderer
  .\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.node.json --composite false   # main
  ```
- **Installer is NOT viable:** the NSIS installer (`package:win`) produces a working `Feishin-*-win-x64.exe`,
  but Windows Defender quarantines the unsigned 223 MB `Feishin.exe` during NSIS temp-extraction (even with
  install-folder exclusions). Use the **portable `dist/win-unpacked/`** folder — copy it anywhere and run
  `Feishin.exe`. (Proper fix would be code-signing.)
- navi-connect settings live under Feishin Settings → the hub URL defaults to `ws://localhost:4790`; set the
  **token**, a device **name**, and **Public server URL** = `https://music.example.com` (used to rewrite stream/image
  URLs for the cast bridge when Feishin's own server is a LAN/Tailscale address).

### Navic (Android, KMP)
No JDK/Android SDK in the dev sandbox — **build in Android Studio** (or `./gradlew :composeApp:assembleRelease`).
Test with a **release** build: debug Compose is dramatically choppier and not representative.
iOS is out of scope (commonMain changes must still compile for iOS; don't break it, but no iOS testing).

---

## navi-connect integration points (where the code lives)

### Hub — `hub/hub.py`
Single file. `Hub` class: `handler` (per-connection), `_on_act`, `_on_report`, `_transfer`, `_broadcast_*`.
- `INTENT_GRACE = 2.0s`: after a user play/pause act, contradicting `isPlaying` reports are ignored (guards
  against stale 1 Hz reports from another device's socket flipping state back).
- On active-device disconnect: `isPlaying=False`, queue/position kept, **active id retained** (only a `released`
  clears it — this is what lets a reconnecting cast bridge re-adopt).
- **AudioMuse Tier-2 proxy** (2026-08-07): `SONIC_ROUTES` + `SonicProxy` + `_build_proxy_protocol` serve plain
  HTTP on `/sonic/*` over the same port 4790 — a five-route whitelist, `Bearer $HUB_TOKEN` auth, injected
  `AUDIOMUSE_TOKEN` + `HUB_ND_USER/PASS`, 4-way concurrency cap, 60s shared cache. Clients no longer hold the
  AudioMuse address/token or the Navidrome password. Needs a `WebSocketServerProtocol` subclass because the
  legacy server rejects non-GET and hides the body from a plain `process_request`. `PROTOCOL.md` §14 +
  `DESIGN-hub-audiomuse-proxy.md` §9.

### Feishin (renderer unless noted)
- **Hub transport (main):** `src/main/features/core/hub/index.ts` (WS client, persists deviceId), preload
  bridge `src/preload/hub.ts`, settings slice in `settings.store.ts`.
- **Protocol hook:** `src/renderer/features/hub/hooks/use-hub.tsx` — maps `do`→player actions, reports ~1 Hz
  when active. Contains the **startup runaway watchdog**: while another device is active, any local playback that
  sneaks through (auto-resume race) is re-paused (`onPlayerProgress`), plus a re-asserting `reconcileRemoteActive`.
- **Unified playerbar (the big merge):** there is NO separate remote bar. `PlayerContext`
  (`src/renderer/features/player/context/player-context.tsx`) **intercepts transport** — when
  `isRemoteSessionActive()` the media* methods route to the hub via `remoteAct()` (in
  `features/hub/utils/remote-queue.ts`). Display is made remote-aware via
  `features/hub/hooks/use-remote-aware.ts` (`useRemoteAware*` hooks: song/status/timestamp/shuffle/repeat/volume;
  the remote song is enriched with `albumId`/`artists` via `getSongDetail` so the bar links work).
  `RemotePlayerbar` was deleted.
- **Unified side queue:** `features/now-playing/components/play-queue.tsx` synthesizes the remote queue as
  `QueueSong[]` with sentinel `_uniqueId = remote:<index>`; double-click routes through `mediaPlay` → hub `jump`.
  `RemotePlayQueue` was deleted.
- **Auto DJ:** `features/player/hooks/use-auto-dj.ts` — has a second subscription to `useHubStore` so it tops up
  the **remote** session when another device plays (the local subscription is gated off while remote).
- **Mood Flow feedback signals (Tier 2 Adaptive):** `features/player/auto-dj/mood-flow-signals.ts` (recency-bounded
  ADD/SUBTRACT id sets) + `features/player/hooks/use-mood-flow-signals.ts` (`MoodFlowSignalsHook`, mounted in
  `audio-players.tsx`). Classifies each outgoing local track by play fraction (≥0.85 → ADD play-through, ≤0.20 →
  SUBTRACT skip; mirrors Navic), feeding `appendAudioMuse`'s alchemy call. **Local playback only** — remote
  playback has no web-player progress, so remote Mood Flow falls back to a current-song seed. v1 tops up at
  queue-refill time (not discard-tail-splice); character presets (temperature/subtract_distance) not wired yet.
- **Cast bridge:** `src/main/features/core/cast/index.ts` (`CastDeviceBridge`). Lazy castv2 client; re-adopts a
  still-playing cast session on (re)connect (`adoptRunningSession`); all `getStatus` calls are wrapped in
  try/catch (castv2 throws synchronously on a dead socket) and a dead socket → teardown + re-adopt.
  `release`'s `stop()` is now also isolated in try/catch: after a FAILED load (IDLE/ERROR → no media
  session) castv2 `MediaController.stop` throws `mediaSessionId` undefined, which previously skipped resetting
  `releasing` and froze all future reports on the bridge. (A failed load with an unreachable `streamUrl` —
  e.g. a stale session published with the wrong origin like `hub.example.com` — is data/config, not a bridge
  bug; the bridge uses `track.streamUrl` verbatim. Ensure Feishin's "Public server URL" = `https://music.example.com`
  and replay to republish.)
  **mDNS discovery lifecycle (fixed):** the renderer re-pushes saved hub settings on every (re)connect, each
  firing `hubEvents 'settings'` → previously an unconditional `stopBridging()`+`startBridging()` that reused
  the same `Bonjour` socket (only `browser.stop()`). That churn left the socket unable to send the initial
  active query, so discovery fell back to the device's ~2-min passive announcement; with no bridge connected,
  the hub kept the stale `cast-<id>` device shown "playing/offline" and the session never re-adopted. Fix:
  the `settings` handler is now idempotent (tracks `lastBridgeConfig`, only restarts on a real enabled/url/token
  change), and discovery always runs on a FRESH `Bonjour` instance (`stopBridging` now `destroy()`s it). Fast
  discovery → bridge reconnects as `cast-<id>` → `adoptRunningSession` re-joins the running cast.
- **AudioMuse visual indicators were removed** from Feishin (sidebar badge + seek-bar tint).
- **CLAP mood search (Tier 2):** `src/main/features/core/audiomuse/index.ts` adds `audiomuse-clap-search`
  (POST `/api/clap/search`) + `audiomuse-clap-stats` (capability probe) IPC; preload `clapSearch`/`clapStats`;
  renderer `fetchClapSearch`/`fetchClapAvailable` in `auto-dj/audio-muse-source.ts`. UI =
  `features/sonic/components/clap-search-modal.tsx` (`openClapSearchModal`), launched from the command
  palette "Mood search" entry (`search/components/home-commands.tsx`, gated on the availability probe).
  Resolves result ids → songs via `getSongDetail` → `addToQueueByData`. Chat/`chatPlaylist` was skipped
  (no LLM provider on the AudioMuse server). Fully fail-soft; desktop-only (main process, no CORS).
- **AudioMuse generator chip (scoped indicator):** `features/sonic/components/audio-muse-generator-chip.tsx`
  (mounted in the queue toolbar `play-queue-list-controls.tsx`) names the active autoplay generator
  (Auto DJ / Sonic Fingerprint / Mood Flow) in the logo palette (`features/sonic/audio-muse-palette.ts`).
  Mood Flow tints by the live 2D mood centroid — surfaced by the `audiomuse-alchemy` IPC (now returns
  `{ids, centroid2d}`) into `store/mood-centroid.store.ts` via `fetchAlchemyIds`. Navic: the QueueScreen
  `isAudioMuseMix` header label was extended to name the generator + tint from `AudioMuseManager.lastMoodCentroid`.

### Navic (commonMain unless noted)
- **Hub client:** `domain/manager/HubManager.kt` — WS engine, `do` handlers, `report`/publish, `act*` helpers
  (`actPlayPause/actPlay/actPause/actNext/actPrevious/actSeek/actJump/actToggleShuffle/actToggleRepeat`),
  `RemoteSessionState`/`isRemoteActive`. **`resolveQueue(tracks)`** resolves the hub queue 1:1 (placeholder
  `DomainSong` for un-synced ids) so the hub index never shifts — used by `load`/`queueChanged`.
- **Unified player (the merge):** `MediaPlayerViewModel` (`shared/MediaPlayer.kt`) exposes a blended `uiState`
  (= remote-session override pushed by `HubManager.startRemoteMirror()` when remote, else local) **and** a
  separate `localUiState` (raw — the hub reporter uses this to avoid feedback). So the whole player UI
  (`MiniPlayer`, `NowPlayingScreen`, `ArtworkPager`, rows) reflects the session with no per-leaf swap. Transport
  is routed to the hub in the interactive components (`MiniPlayer`, `ButtonsRow`, `ProgressBar`, `ArtworkPager`,
  `QueueScreen`). `RootBottomBar` always shows `MiniPlayer`; `RemotePlayerBar` + `RemoteControlSheet` deleted;
  NowPlaying auto-minimize removed. `DevicePickerSheet` kept (transfer via NowPlaying → Radio icon).
- **Android media controls (notification/lock screen):** `androidMain/.../RemoteSessionPlayer.kt` — a
  `SimpleBasePlayer` facade over the hub session, swapped into the media3 `MediaSession` in
  `androidMain/.../MediaPlayer.android.kt` (`PlaybackService`) while `isRemoteActive`. It reports remote state +
  forwards transport to the hub; has a 1.5 s **activation grace** (`onActivated()`) so a swap-in state-sync
  doesn't spuriously forward `play` to the active device.
- **ArtworkPager** only changes track on a real user **drag** (tracks `DragInteraction.Start`) — programmatic
  scrolls from the remote mirror must not fire `actJump` (that caused an infinite skip loop).

---

## Status

**Working / confirmed by user:**
- Hub + transfer-with-resume; Feishin ⇄ Navic ⇄ Chromecast transfers.
- Feishin unified bar + side queue; startup runaway fixed; remote Auto DJ; cast bridge (incl. dead-socket recovery).
- Navic unified player (mini + now-playing + queue); Android media controls drive the remote; row-overlap (hi-DPI) fixed.
- Phase 1/2 features (ratings/favorites, similar-songs radio, smart-playlist editor, playlist downloads,
  metered transcoding) — see `ROADMAP-V2.md`; mostly built, Navic pieces compiled by user.

**Known limitations / gotchas:**
- Feishin installer unusable (AV) → portable build only.
- Navic: starting *brand-new* local playback while another device is active is blocked by the media-session
  facade — transfer to Navic first.
- Remote-bar favorite heart isn't optimistic in Feishin (reads hub-published state; the star still applies).
- Feishin remote queue rows: go-to-album/artist + remove/reorder are limited; jump + add-to-queue work.
- Cast: stream/cover URLs must be publicly reachable (Tailscale-IP server config breaks cast → set Public
  server URL to https://music.example.com).
- Navic remote **display** mirror still uses the drop-based `resolveSongs` (not `resolveQueue`); harden if a
  display/playback mismatch on un-synced songs ever appears.

**Next up (not started):** AudioMuse-AI customizable recommendations (Symfonium-style) in both players —
needs research into AudioMuse's real API beyond the `getSimilarSongs2` Subsonic shim.

---

## 🐞 Navic cast crash — CRASH FIXED; lifecycle symptoms still open
Field-reported while **casting from Navic to a TV** (Navic's own `AndroidCastManager`/media3 cast, not the
Feishin hub cast bridge).

**✅ FIXED (not yet user-verified) — the crash itself.** Deobfuscated the stack against
`mapping.txt`: it was inside the **media3-cast library**, not our code —
`RemoteMediaClient` status update → `RemoteCastPlayer$StatusListener` → `updateTimelineAndNotifyIfChanged`
→ `CastTimelineTracker.getCastTimeline` → `DefaultMediaItemConverter.toMediaItem(MediaQueueItem)` line 57,
where `MediaQueueItem.getMedia()` was **null** (the receiver briefly reports a queue item by id before its
`MediaInfo` is populated) and the default converter dereferences it. Fix: `MediaPlayer.android.kt` now
constructs `CastPlayer(castContext, SafeMediaItemConverter())` — a converter that delegates to
`DefaultMediaItemConverter` but returns a placeholder `MediaItem` when `media == null` (the next status
update fills in the real item). Not compiled in sandbox.

**⏳ STILL OPEN (future phase) — cast session lifecycle after a process restart.** Separate from the crash
above; needs its own pass. The original crash stack (NPE `getClass()` on null, main-thread Handler callback:
`g75.s`/`f75.m`/`nc7.h`/`k75.o`/`ct0.run`) is already deobfuscated + fixed above. The remaining symptoms the
user saw after the crash were a cascade from the lost session:
1. Reopen Navic → **cast session not restored in now-playing** even though the TV kept playing. Navic has
   **no Android-side re-adoption** of a still-running cast session (Feishin's cast bridge does this via
   `adoptRunningSession`/`getSessions`+`join`; Navic's `AndroidCastManager`/`PlaybackService` `castPlayer`
   does not rejoin an existing MediaRouter/Cast session on process restart).
2. Tapping **Cast → TV again did nothing** (stale `CastManager`/MediaRouter state — thinks it's already
   connected, or the route/session is in a bad state after the crash).
3. Tapping **Transfer** resumed **from the pre-crash moment with the OLD pre-crash song**, not the different
   song the user had selected locally before re-casting → the hub session still held the stale pre-crash
   intent; the new local selection never became the session queue.
4. **Transfer back to Navic** → TV kept casting AND **restarted from the first song in the queue** (index +
   position not carried; cast device didn't release / Navic re-loaded queue at index 0).

**Where to look:** Navic `AndroidCastManager` (MediaRouter discovery/connect, devices StateFlow cached across
discovery), `PlaybackService` (exoPlayer↔castPlayer swap via `SessionAvailabilityListener`, state carry
items/index/position/playWhenReady), and how Navic-native cast interacts with the **hub session** (these are
two separate mechanisms — native cast vs hub transfer — and their lifecycles are getting out of sync). Compare
against Feishin's hardened cast-bridge re-adoption + release semantics.

---

## Tips for the next session
- Memory/journal of every change is in the assistant's project memory; this doc is the durable summary.
- After any Feishin change, run the two `tsc` commands above (not `pnpm run typecheck`).
- Navic can't be compiled in the sandbox — write Kotlin carefully and have the user build; expect occasional
  compile-fix rounds (esp. for media3 `SimpleBasePlayer` API specifics).
- When debugging a blank Feishin window: `Feishin.exe --remote-debugging-port=9222`, then `chrome://inspect`
  (not the `localhost:9222` list page — it hands Chrome a `ws://` URL it can't open).
