# navi-connect ecosystem — v2 roadmap

Grounded in surveys of: AudioMuse-AI-NV-plugin (exposes everything via standard
Subsonic `getSimilarSongs2`/`getSimilarSongs`/`getArtistInfo` + OpenSubsonic
`sonicsimilarity` extension), Navidrome 0.62 source (native API accepts smart
playlists as `rules` JSON — criteria model in `model/criteria`; album lists via
`getAlbumList2` types `frequent`/`newest`/`recent`), and the current Feishin /
Navic forks.

Legend: **[F]** Feishin · **[N]** Navic · **[H]** hub · effort S/M/L/XL

---

## Phase 1 — quick wins (Subsonic APIs already exist)

1. **[F] Remote-bar parity: queue + favorite + rating** (S)
   Add to the remote playerbar: queue popover (session queue, tap→`act jump`),
   favorite + 5-star rating for the remote now-playing track via Feishin's own
   mutations (same server; mirrors what Navic's RemoteControlSheet already does).

2. **[N] Star ratings + rename "starred"→"Favorites"** (M)
   Subsonic `setRating(id, 0-5)` (already wrapped by `SyncActionType.STAR_0..5`
   machinery — Navic's `AlbumRepository.rateAlbum` shows the exact pattern; do
   the same for songs everywhere "starred" appears). UI: 5-star control in
   NowPlaying + song sheets; relabel starred → Favorites.

3. **[N] Track/album/artist radio == AudioMuse support** (M)
   One feature: `getSimilarSongs2(id, count)` — works vanilla and is upgraded
   server-side when the AudioMuse plugin is installed (its README says exactly
   this). Add "Start radio" to song/album/artist sheets → fetch similar songs →
   `loadRemoteQueue`-style local play (or publish to hub session). Artist radio
   from artist id; album radio: seed = album's tracks. Optional later:
   OpenSubsonic `sonicsimilarity` extension endpoints for mix-by-multiple-seeds.

4. **[N] Home page rows: Most played + Newly added** (M)
   `getAlbumList2 type=frequent` and `type=newest` (Navidrome supports both),
   rendered as horizontal scrollable categories like Recently Played, each with
   a "See all" that opens the existing album-list screen pre-filtered.

## Phase 2 — bigger client features

5. **[F] Transcode profile for metered/cellular networks** (M)
   Subsonic `stream` already accepts `maxBitRate` + `format`. Feishin: add a
   per-server "transcoding profile" (format/bitrate) + a toggle ("use when
   metered" via `navigator.connection` where available, else manual toggle in
   the playerbar config menu).

6. **[N+F] Smart playlists, Symphonium-style UX** (L)
   Backend exists: Navidrome native API playlists accept `rules` (criteria
   JSON: loved, rating, playCount, dateAdded, genre, year, …, sort + limit).
   Feishin: already has the query-builder — polish only.
   Navic: new "smart playlist" editor (rule rows → criteria JSON → native API
   `POST /api/playlist`), playlists then behave like normal server playlists
   (auto-updating server-side; clients just see fresh contents).

7. **[N] Downloads for (smart) playlists: rolling vs permanent cache** (L)
   Navic already has DownloadManager + per-song download. Add per-playlist
   download policy: `permanent` (keep all) or `rolling(N songs/GB)` — on
   playlist refresh, download new entries, evict ones that left the playlist
   (rolling) keeping within budget. Quality/format per policy via stream
   `maxBitRate`/`format` params on the download URL.

8. **[F] Playlist downloads with quality/format** (L)
   Feishin has no offline cache at all (desktop streams). Implement a minimal
   downloads dir: per-playlist "Download" with format/bitrate choice (uses the
   same transcoded stream URLs), tracked in a local manifest; rolling option
   shared design with Navic's.

## Phase 3 — Chromecast everywhere

9. **[F] Chromecast from Feishin** (L)
   Electron has no Cast SDK; use `castv2-client` + mDNS discovery in the main
   process (proven pattern), casting Navidrome stream URLs to the Default Media
   Receiver. Same lazy-discovery/cache UX as Navic's picker.

10. **[H+N+F] Full virtual-receiver model** (XL)
    Cast devices become first-class hub devices: the bridging client registers
    `bridge-cast` virtual devices (hub `hello` extension), relays `do` commands
    to the Cast session and reports position back. Then Feishin's picker can
    target the living-room TV even when only the phone is near it. Protocol
    addition: `device.caps += ["bridge-cast"]`, `bridgedBy` field, hub routes
    `do` for a virtual device to its bridge.

## Phase 4 — polish / low priority

11. **[F] Synced lyrics while remote** (M) — Feishin already renders synced
    lyrics from the local timestamp; in remote mode, drive the lyrics clock
    from the interpolated session position (the remote bar already computes
    it). Low priority per user.

---

## Suggested order

1 → 2 → 3 → 4 (Phase 1, immediate) · then 5 → 6 → 7 → 8 · then 9 → 10 · then 11.
Each lands independently; nothing blocks the others except 10 (needs 9 for
Feishin-side bridging, and benefits from hub changes shared with Navic).
