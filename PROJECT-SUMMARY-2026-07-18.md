# navi-connect — Status Summary (2026-07-18)

*Built by reading every markdown doc in the root + `navic/`, `feishin/`, `hub/` folders and
the project memory, then cross-checking each claim against the actual source tree.*

**What it is:** a Spotify-Connect–style remote-control layer over a personal Navidrome
(`https://music.example.com`). Two client forks — **Feishin** (Electron/Windows) and **Navic**
(Kotlin Multiplatform, Android) — act as both controllers and receivers; a headless Python
**hub** (asyncio/websockets, port 4790) holds session intent and routes commands. Audio never
flows through the hub. iOS is out of scope.

---

## Doc vs. code: the headline finding

The docs are accurate, but several of them are *plans that have since been implemented in code*.
The biggest gap between "what the docs say is pending" and "what the tree actually contains":

- **`navic/redesign.md`** (Direction-B ambient theming handoff) reads as a to-do list, but its
  targets already exist in code: `LibraryHeroAmbient`, a real `rememberLibraryTabBackground()`
  (wired into Library + Album/Artist/Genre tabs), `BlendBackground` `crossfade(400)`, and the
  MiniPlayer tinted ring/shadow. This handoff is **substantially done** (not yet user-verified).
- **`NAVIC-SYMFONIUM-PLAN.md`** lists remote queue remove/clear, a real download center, and
  saved queues as future work — but `actMoveQueueItem/actRemoveQueueItem/actClearQueue`,
  `DownloadEntity`/`DownloadDao` with status+retry fields, and `SavedQueueEntity`/`SavedQueueDao`
  are all present in the tree. Large parts of this plan are **already built**.
- The `SongRow` `albumId as String` crash it flags is gone (no such cast remains).

So this summary separates **confirmed working**, **built but not yet verified**, and **genuinely
open**.

---

## ✅ Done and confirmed by the user

**Core platform**
- Hub + transfer-with-resume (index + position preserved), Feishin ⇄ Navic ⇄ Chromecast in any
  direction, including paused transfers. Hub hardening: `INTENT_GRACE` stale-report guard,
  active-device retained on disconnect (enables cast re-adoption).
- **Feishin unified player** — one player bar and side queue drive local *or* remote via transport
  interception; startup runaway-audio watchdog; remote-aware Auto DJ; cast bridge with dead-socket
  recovery and running-session re-adoption.
- **Navic unified player** — blended `uiState` mirrors the session across mini-player, now-playing,
  queue and artwork pager; Android notification/lock-screen controls drive the remote session
  (`RemoteSessionPlayer` facade); hi-DPI row-overlap fixed.

**Roadmap-V2 Phase 1–3**
- Phase 1: star ratings + "Favorites" rename, similar-songs radio, artist radio, home rows
  (frequent/newest), Song Journey — both clients.
- Phase 2: Feishin metered/cellular transcode profile; Navic smart-playlist editor (Navidrome
  native API); playlist downloads with quality/format + rolling-vs-permanent cache (both clients,
  with the mirror-by-song-id fix).
- Phase 3: Chromecast from Feishin via `castv2-client` + mDNS, implemented as the full
  virtual-receiver cast bridge (cast devices appear in every picker). Field-confirmed flawless.

**AudioMuse recommendation layer**
- **Tier 1** (both clients): Instant Mix, Artist Radio, Song Journey via `getSimilarSongs2` /
  `getSonicSimilarTracks` / `findSonicPath`, behind a `sonicSimilarity` capability probe.
- **Tier 2 — Navic:** Sonic Fingerprint autoplay; Adaptive "Mood Flow" (alchemy centroid with
  skip/play-through signals); Echo/Steady/Transition character presets; adaptive visualizer with
  mood-reactive palette.
- **Tier 2 — Feishin:** autoplay-source dropdown (Auto DJ / Fingerprint / Mood Flow), Mood Flow
  feedback signals, blob visualizer with mood palette.
- **CLAP text→mood search** (both clients); scoped **AudioMuse generator chip** naming the active
  generator + centroid tint (both). Chat/`chatPlaylist` intentionally skipped (no LLM on server).
- Navic Haze frost (mini-player + nav) and the now-playing Mood Flow quick-access button confirmed.

---

## 🛠️ Built in code, not yet verified (Navic can't compile in the dev sandbox)

These are present in the source but await a user release build / field test:

- **Latest ambient-theming rounds** (`navic/redesign.md` + `HANDOFF-theming.md`): `LibraryHeroAmbient`,
  `rememberLibraryTabBackground`, `BlendBackground` crossfade, cover-driven MiniPlayer ring/shadow,
  cover-scheme text tinting, cover-driven status bars.
- **Remote queue editing** — move / remove / clear routed to the hub (`act move/remove/clear`).
- **Download center + saved-queue infrastructure** — `DownloadEntity`/`DownloadDao` with
  status/retry, `SavedQueueEntity`/`SavedQueueDao`, migrations.
- **Transfer index-misalignment fix** (`resolveQueue` 1:1 with placeholder songs), the
  `RemoteSessionPlayer` media-session facade, and the **native cast-crash fix**
  (`SafeMediaItemConverter` for null `MediaInfo`).

Feishin equivalents typecheck clean (web + node) per the docs; run the two `tsc` commands (not
`pnpm run typecheck`) after any Feishin change.

---

## 📋 Still open / to-do

**AudioMuse — Adaptive polish (the current frontier, `DESIGN-adaptive-audiomuse.md` §5–7)**
- **Feishin proper Mood Flow re-splice loop** — today it tops up at queue-refill time; the design's
  "discard the un-played tail and splice fresh candidates ahead of the playhead" is not done.
- **Character-param wiring in Feishin** (`temperature` / `subtract_distance`) so Echo/Steady/
  Transition actually bias the engine (Navic already does this).
- **Feishin parity for the mood-reactive palette + Haze frosting + energy-reactive motion.**

**Navic native Cast lifecycle (`HANDOFF.md` "⏳ STILL OPEN")** — *confirmed still absent in code*
- No Android-side re-adoption of a still-running cast session after a process restart (no
  `getSessions`/`join` in Navic's `AndroidCastManager`, unlike the Feishin bridge). Symptoms:
  session not restored on reopen, "Cast again" no-op, transfer resumes stale pre-crash song,
  transfer-back restarts at index 0. The crash itself is fixed; the lifecycle cascade is not.

**Expressive blur expansion (`DESIGN-expressive-blur.md`, "plan only")**
- Haze is wired on NowPlaying only. Extending backdrop blur to the mini-player bar, bottom sheets,
  and top app bars (with a shared `expressiveHazeSource/Effect` helper) is unbuilt; verify the
  `haze 2.0.0-alpha03` build on CMP 1.11 first.

**Symfonium-plan later QoL (`NAVIC-SYMFONIUM-PLAN.md`, mostly un-started)**
- Queue history (auto-save replaced queues, restore/resume/save-as-playlist), queue undo, download
  QoL (Wi-Fi-only / charging-only / concurrency / repair), library QoL (alphabet jump, continue-
  listening row, recently-added songs, downloaded-only filters, better empty/stale states).

**Cross-cutting**
- **Server prerequisite:** confirm the AudioMuse plugin is installed + enabled and the library
  index is warm on `music.example.com` — the two sonic endpoints 404 without it; keep Tier-2 fail-soft.
- **No version control** on any of the repos in this environment — worth `git init` given the size.
- **Feishin installer is AV-blocked** → ship the portable `dist/win-unpacked/` build; cast URLs
  must use `https://music.example.com` (not the Tailscale IP).
- **Housekeeping:** `MEMORY.md`'s index line still says "building hub next," which is long out of
  date; and `STATUS-2026-06-19.md` predates the Tier-2 completion — both could be refreshed.
