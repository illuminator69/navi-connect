# navi-connect — architecture

How the pieces fit together, what talks to what, and where the code lives. The
[README](../README.md) is the project description; [`PROTOCOL.md`](../PROTOCOL.md) is the full wire
spec; [`TESTING-SETUP.md`](../TESTING-SETUP.md) is how to actually run it.

The hub is the only component in this repository. Both clients and lb-bot are separate repos, linked
below — this file covers the parts of them that face the hub.

---

## 1. What it is / the problem it solves

Navidrome is a self-hosted music server (Subsonic-compatible). Its clients normally each play
independently. navi-connect adds a **shared playback session** across devices — like Spotify
Connect — so you can:

- Control what's playing on one device from another (play/pause/next/seek/volume/queue edits).
- **Transfer playback with resume** between devices (same track, same position) — phone → desktop → TV.
- **Cast to a Chromecast** and have it appear as a device in every client's picker.
- Get **recommendations / autoplay / adaptive radio** (AudioMuse-AI) that play on whichever device is active.

It is a **single-user, personal** setup (one Navidrome account). Everything runs on the user's own
infrastructure (Unraid, Docker `media` network). Server: `https://music.example.com` (publicly reachable —
important for Chromecast, which must fetch stream URLs directly).

---

## 2. Components

| Component | What it is | Tech | Location |
|---|---|---|---|
| **Hub** | Headless relay holding session intent; routes commands; AudioMuse Tier-2 + lb-bot proxies; optional Navidrome `savePlayQueue` mirror | Python 3.11+, asyncio, `websockets`, **port 4790** | `hub/` |
| **Feishin** (fork) | Desktop client (controller + receiver) + the **Chromecast bridge** | Electron / TypeScript / React | [feishin-gaps](https://github.com/illuminator69/feishin-gaps) |
| **Navic** (fork) | Mobile client (controller + receiver) + native Chromecast | Kotlin Multiplatform / Compose, **Android only** | [navic-gaps](https://github.com/illuminator69/navic-gaps) |
| **Preview sidecar** | yt-dlp preview of a track the library lacks. Its own process, and **the media origin** — the hub carries only the control plane (`PROTOCOL.md` §16) | Python 3.11+, asyncio, `yt-dlp`, **port 4792** | `preview/` |
| **Navidrome** | The music server (not in this repo) | Go, Subsonic/OpenSubsonic API | `https://music.example.com` |
| **AudioMuse-AI** | Recommendation engine (not in this repo) | Navidrome plugin (Tier 1) + core HTTP API (Tier 2) | server-side |
| **lb-bot** | Library-gap filler (missing-album discography + Soulseek acquisition). Separate repo, reached **through the hub** on `/lb/*` | Python/Flask, port 8899 | [own repo](https://github.com/illuminator69/lb-bot) |

**Scope boundaries:** iOS is out of scope (Navic's commonMain must still *compile* for iOS, but no
iOS features/testing). The web build of Feishin falls back gracefully (Tier-2 AudioMuse is desktop-only
because it needs the Electron main process to bypass CORS).

---

## 3. Architecture

### Roles
- The **hub** owns *session intent*: `queue, index, positionMs, isPlaying, activeDevice, repeat,
  shuffle, order`. It persists across restarts (but clears `activeDevice` and sets `isPlaying=false`
  on load). It never touches audio.
- Every client is **both a controller and a receiver**. It connects over WebSocket, sends a `hello`
  (persisted device id + token), receives a `welcome` (session snapshot + device list).
- The **active receiver** is the source of truth for live position and sends `report` frames ~1 Hz.
- **Controllers** send `act` frames; the hub applies intent and forwards `do` directives to the active
  receiver.

### Transfer-with-resume
Hub sends `do:release` to the old device → it replies `released` with its final index+position → hub
sends `do:load {tracks, index, positionMs, play}` to the target. Resume is exact.

### Chromecast
Bridged by **either client**, whichever sees the speaker first — Feishin's main process
(`bonjour-service` + `castv2-client`) or Navic (`NsdManager` + a hand-rolled castv2 client). Neither
uses a Cast SDK. Every mDNS-discovered cast device is registered with the hub as a virtual `receiver`
(id `cast-<id>`, name `📺 <name>`), so it appears in every client's device picker and casting is just
a transfer. Audio = direct Navidrome stream URLs (must be publicly reachable — set Feishin's
"Public server URL" to `https://music.example.com`, not a Tailscale/LAN address).

Only one client bridges a given speaker: a client that sees `cast-<id>` already online stands down,
and one superseded off the id (hub close `4003`) stays down for 5 minutes rather than kicking back.
See PROTOCOL.md §12.2.

**Scrobbling a cast session** is the bridging client's job (Feishin: `use-cast-scrobble.ts`, gated
on `cast.bridgedDevices()`; Navic: `CastScrobbler`, gated on a `BRIDGING` speaker — both keyed on
the active device).
A Chromecast holds no Navidrome credentials, so the receiver-reports-its-own-plays
rule leaves nobody to report; the single-bridge rule above is what stops two
watching controllers both counting the play.

### Data/theming philosophy
Both clients derive dynamic UI color from album art (kmpalette → materialKolor scheme in Navic).
AudioMuse "Mood Flow" drives an adaptive visualizer.

---

## 4. The wire protocol (summary)

Full spec in `PROTOCOL.md`. Frames are plain JSON with a `t` discriminator. (The same port also
answers plain HTTP on `/sonic/*`, `/lb/*` and `/preview/*` — the AudioMuse Tier-2, lb-bot and
preview-sidecar proxies, §5 — which are not part of this catalog.)

| Frame | Direction | Purpose |
|---|---|---|
| `hello` | client → hub | announce device (id, token, name, platform, caps) |
| `welcome` | hub → client | session snapshot + device list |
| `act` | controller → hub | intent: `play/pause/playpause/next/previous/jump/seek/setQueue/enqueue/volume/repeat/shuffle/transfer/move/remove/clear` + saved-queue mgmt `renameSavedQueue/deleteSavedQueue/deleteSavedQueues/syncSavedQueues` + mix mgmt `saveMix/renameMix/deleteMix/touchMix` |
| `do` | hub → active receiver | directive: `load/play/pause/jump/seek/setVolume/setRepeat/setShuffle/release/queueChanged` |
| `report` | active receiver → hub | ~1 Hz position/index/isPlaying |
| `released` | receiver → hub | final index+position on release (transfer handshake) |
| `session` | hub → clients | broadcast session state changes (incl. `savedQueueId` of the current queue) |
| `progress` | hub → clients | ~1 Hz position for remote controllers to interpolate |
| `devices` | hub → clients | device list changes |
| `savedQueues` | hub → clients | shared saved-queue history changed (also embedded in `welcome`) |
| `mixes` | hub → clients | "Mixed for You" recipes changed (also embedded in `welcome`) |
| `error` | hub → client | error |

**Hub safeguards:**
- `INTENT_GRACE = 2.0s`: after a user play/pause `act`, contradicting `isPlaying` reports are ignored
  (guards against a stale 1 Hz report from *another* device's socket flipping state back).
- On active-device disconnect: `isPlaying=false`, queue/position kept, **active id cleared** — "no live
  receiver" is the signal every still-open client uses to adopt the last-known queue locally (paused).
  A device that was really still playing re-claims active via its reporter on reconnect.
- **Taking over an orphaned session** (`act:play` with no active device) is answered with a full
  `do:load` carrying the session's queue + position, not a bare `do:play` — the new device knows
  nothing of the session, so it must be told where the dead one left off.
- Transferring to the **already-active** device is a no-op (a reload there would rewind by up to one
  report interval).

**Track metadata** carried in the queue includes `streamUrl` + `mime` (for the cast bridge),
`imageUrl`, `durationMs`, and per-track `userFavorite`/`userRating`.

**A queue track need not be a library track.** An `ext:<provider>:<id>` track is a *preview* of
something the library does not have, served by the preview sidecar (§5, `PROTOCOL.md` §16). It
needs no protocol work at all, and that is why the id format was chosen: the hub's queue is opaque
passthrough and `SQ_TRACK_FIELDS` already whitelists exactly the fields a resolved preview carries,
so an `ext:` track survives saved-queue sanitisation, `syncSavedQueues` and a state reload
unchanged. Transfer, the device picker and Continue Listening work with no change. The one thing
both clients **must** add is the cast refusal: while `previewCastable` is false, a transfer to a
cast target with an `ext:` track queued is refused *with a stated reason*, because a Chromecast
that cannot reach the sidecar plays silence under a playing bar.

**"Mixed for You" is hub-owned too, and it is a recipe rather than a result.** A saved queue stores
the tracks; a mix stores `{kind, seedId, moodCharacter, count}` and each client **regenerates** it
locally with the engine it already has (`RadioManager` / `auto-dj/*`). The hub never generates
anything — it stores and broadcasts, which keeps it audio-free and AudioMuse-free. So a second play
of the same mix gives a different queue, and that is the whole point: nothing in either client
persisted a recipe before (`RadioManager` took those values as arguments and dropped them, and
`playMix` exited into a frozen `SavedQueueEntity`). Capped at 30, evicted by `updatedAt`, and
deliberately **without** tombstones — unlike a saved queue, a mix is never published concurrently
by several devices, so there is no race to arbitrate. `PROTOCOL.md` §17.

**Saved-queue history is hub-owned** (Continue Listening): the hub keeps a rolling, capped list of
queue records, broadcasts it (`savedQueues`), and marks the current one via `session.savedQueueId`. A
`setQueue` records/refreshes it and a top-up grows the *same* record; both clients render one shared
history with the active queue highlighted. Each client keeps a local store as an **offline cache** and
`syncSavedQueues` reconciles on reconnect — a field-level union-merge (newest-wins, but a copy that
lacks a name never blanks one), with **tombstoned deletions** so a client's stale row can't resurrect
a deleted queue. Tombstones are kept on both sides: a client replays the deletions it made while
offline (`syncSavedQueues.deleted`), and a tombstoned id is inert on the hub — neither a re-sync nor
a `setQueue` from a device that kept playing can bring the record back. A record's **identity is a listening session, not a track list**: name, kind and
cover are stamped once at birth, and edits (reorder/remove/play-next/top-up/shuffle) refresh the
*same* record — only a genuine new play mints another. See `PROTOCOL.md` §8.3.

---

## 5. External APIs used

### Navidrome — Subsonic / OpenSubsonic
Standard Subsonic auth (salted token). Used for library, streaming, playback, ratings, playlists:
- `stream` (with `maxBitRate` + `format` for transcoding), `getSongDetail`, `getAlbumList2`
  (`frequent`/`newest`/`recent` for home rows), `getArtists`, `search3`, `star`/`unstar`, `setRating`,
  `savePlayQueue` (hub mirror), `getСoverArt`.
- **Native Navidrome API** (`POST /auth/login` → Bearer, `POST /api/playlist` with `rules` criteria
  JSON) for **smart playlists** (Navic `NativeApiManager`).

### AudioMuse-AI — two tiers
**Tier 1 — Navidrome plugin, zero config, Subsonic auth** (works today on 0.62.0; sonic endpoints
require the AudioMuse plugin loaded — probe with `getOpenSubsonicExtensions` advertising
`sonicSimilarity`):
- `getSimilarSongs2(id, count)` → Instant Mix / Similar autoplay (works vanilla).
- `getArtistInfo` → Artist Radio.
- `getSonicSimilarTracks(id, count)` → scored similar; `findSonicPath(startSongId, endSongId, count)`
  → **Song Journey**.

**Tier 2 — AudioMuse core HTTP API** (synchronous in-memory lookups, so fast; only ML jobs are queued
off the client path). **Reached through the hub**, not directly: `<hub>/sonic/*` with the hub token
(plain HTTP on the WebSocket port — `PROTOCOL.md` §14). The hub holds
the AudioMuse address, its API token and the Navidrome password server-side, so no device carries
them and the core API needs no internet exposure; it whitelists the five routes below, injects the
credentials, caps concurrency and caches results for both clients. Each client keeps its old direct
`http://host:8000` + `Bearer <API_TOKEN>` config as an explicit fallback for a LAN setup with no hub,
and demotes itself to it for 10 minutes if the hub reports no AudioMuse configured. Still desktop-only
in Feishin (routed through the Electron main process to avoid CORS); native Ktor in Navic:
- `GET /api/sonic_fingerprint/generate?n=` → autoplay seeded from listening habits.
- `POST /api/alchemy {items:[{op:ADD|SUBTRACT,id,type}], n, temperature?, subtract_distance?}` →
  centroid + nearest songs (returns `centroid_2d`). This is the engine behind **Adaptive "Mood Flow"**.
- `POST /api/clap/search` → text→mood search. (`chatPlaylist` intentionally **not** used — no LLM on
  the server.)

All AudioMuse calls are **fail-soft**: a cold index, missing plugin or unreachable hub greys the
feature out and falls back to Tier 1, never errors.

### Preview sidecar — `preview/`, and why it is not in the hub

A separate process (`preview/`) that answers "what does this album I don't own sound
like". It resolves artist+title to an `ext:<provider>:<id>` track and **serves that track's audio
itself**. Full spec: `PROTOCOL.md` §16.

**The hub proxies only the control plane, and that is the round's main architectural decision.**
`/preview/resolve` and `/preview/status` are buffered JSON and fit `HttpProxy` perfectly. The
audio does not and structurally cannot: `HttpProxy.handle` ends in a single `bytes` body coerced
through `AbortHandshake` with a library-computed `Content-Length`, `PROXY_MAX_RESPONSE` is 4 MB
against a measured 11.9 MB for one four-minute track, `PROXY_MAX_INFLIGHT` is 4 slots shared with
every lb-bot call, and the handshake deadline the proxies answer from inside would truncate a long
body mid-stream — the exact bug the slow lb-bot routes were just bitten by. Write that down rather
than re-deriving it: a proxy that relays everything *except* media looks like an omission.

Three things about it that are load-bearing:

- **The stream URL is a capability, minted by the hub.** A Chromecast fetches it itself and sends
  no headers, so the credential lives in the URL — and `HUB_TOKEN` must never be in one, because
  it is the hub's whole administrative surface. `PREVIEW_SECRET` is a *second* secret shared by
  the two processes, authorising exactly one thing: one `ext:` id's audio until `exp`. The sidecar
  returns `streamUrl` empty and the hub fills it in, which is the one place a proxy here
  transforms a body — and it happens **after** the cache, because a resolution is cached six hours
  while a capability lives `PREVIEW_TTL`, so signing before the cache would serve dead URLs for
  the back half of every entry's life.
- **`{}` is an answer.** "No preview found" is a 200, never an error, on the same rule as lb-bot's
  `strict=False` chain. A *wrong* preview is worse than none, so a candidate whose length differs
  by more than the tolerance is rejected whatever its title says.
- **`mime` is resolved, not assumed.** Both clients feed it to their Cast `MediaItemConverter`, so
  a declared type that does not match the bytes is a track that loads and never plays. The sidecar
  extracts the format during `/resolve` and reports what it will actually send — including
  `video/mp4` when no audio-only stream is on offer, which is currently the common case on YouTube
  without a PO token (measured 2026-09-23: exactly one format, a muxed 360p MP4). A muxed
  container plays; one mislabelled as audio does not.


**The extractor needs cookies here, and that is an environment fact rather than a code one.**
Some egress addresses get *"Sign in to confirm you're not a bot"*. Measured 2026-09-23 from one
machine in one second: the same code passed over IPv6 and was challenged over IPv4 — and this
NAS is IPv4-only, so production is on the challenged side while the workstation is not. That
gap is why the first "a real resolve works" claim did not transfer. `PREVIEW_COOKIES` points the
sidecar at a `cookies.txt`; use a **throwaway account**, because the file is bearer access to
whatever exported it and yt-dlp traffic can get an account limited. The player client switches to
`web`/`mweb` when cookies are set — never `android`, which is the pairing that gets accounts
terminated. Cookies expire, so `/status` carries `extractorBlocked` and the hub relays it on
`/preview/status`: without that, a challenge and a genuine no-match are identical from a client
(an empty resolve against a process reporting itself healthy).

**STATE 2026-09-23: deployed, wired, and the extractor is blocked by the egress address.** Both
containers run, `/preview/status` answers `{configured: true, upstreamReachable: true,
previewCastable: true}`, the capability signing and the Range relay are verified end to end on
real audio — and `/preview/resolve` answers `{}` for everything, because YouTube serves this
NAS's IPv4 address **no media formats at all** (only `mhtml` storyboards). Measured from one
machine in one second, twice, on two videos: IPv6 returns format 18, IPv4 returns nothing. It
degraded *during* that session — at 00:20 the unauthenticated `android` client still returned
format 18 from the NAS, by 01:00 it returned nothing — so the address is being progressively
restricted rather than merely challenged.

**Cookies were tried with a real throwaway account and are not the answer.** They work as
designed — the jar is picked up without a restart, yt-dlp refreshes it in place, and the error
moves from `Sign in to confirm you're not a bot` to `Requested format is not available` — but
they fix *authentication*, not *format availability*. Two things learned doing it, both of which
matter if anyone retries:

- **yt-dlp refuses the `android` client outright when a cookie file is configured** ("does not
  support cookies"). So enabling cookies *removes* the only client that ever worked here
  unauthenticated. The client switch in `_ydl` is forced by that, not a preference.
- **The image has no JavaScript runtime**, which is a latent bug rather than anything of
  YouTube's: yt-dlp needs one to solve the signature / `n` challenges for every web-family
  client, and `python:3.12-slim` ships neither Node nor Deno. It stayed invisible because
  `android` serves pre-signed URLs and needs no JS. Installing Deno clears both warnings and
  still yields zero formats, so it is not the blocker — but it is the *next* one, and any
  cookie-based retry needs it (~92 MB onto a 142 MB image).

**The only path with evidence behind it is IPv6**, and it is blocked on something outside this
codebase: the router does not filter inbound v6 (verified from a phone on mobile data against
this workstation's global address), and `docker-proxy` already binds the v6 wildcard for
`:::8899` (lb-bot, **unauthenticated**, answers 200), `:::5432`, `:::6379`, `:::4533`, `:::8000`.
Today those are shielded by nothing but the absence of a routable address. The gateway is a
**Sagemcom** CPE with no local web UI on any port from either machine, so the firewall is not
locally changeable — it is a Proximus app/portal or support matter. The resting fix, when access
exists, is IPv6 **plus** a host firewall (`ip6tables` default-DROP on INPUT/FORWARD, established
and related allowed, persisted in Unraid's `go` script), and then **no cookies and no JS runtime
are needed at all** — `android` works unauthenticated over v6.

`PREVIEW_COOKIES` is commented out in the NAS project's `.env` and the jar has been deleted. The
code path stays: it is tested, it costs nothing while unset, and it is the thing to re-enable if
the egress ever changes. Everything else in this round — `mixes`, the seven lb-bot route
reservations, signing, Range — is deployed and verified.

Unset `PREVIEW_URL` hides the feature entirely, like `LBBOT_URL`. Unset `PREVIEW_PUBLIC_URL`
keeps it working locally but sets `previewCastable: false`, which both clients must honour.

### lb-bot — library-gap intelligence
A separate self-hosted service (its own repository) that indexes each artist's full
MusicBrainz discography, knows which releases the library lacks, and can acquire one from Soulseek
and place it. Reached **only through the hub** (`<hub>/lb/*`, `PROTOCOL.md` §15,
its own Flask API has no authentication and binds
`0.0.0.0:8899`, so unlike AudioMuse there is deliberately **no direct-LAN fallback**. `LBBOT_URL`
unset = the whole surface hides. Whitelisted routes cover the instant discography read, the
explicit "index this artist" scan, album editions/tracklist/similar, the one-tap download, a
scoped download-status poll, and the per-album gap actions below.

Two kinds of gap, and they take different pipelines. A release the library lacks **entirely** is one
`album/download`. An album it holds **partly** (9 of 12 tracks) goes through lb-bot's Fill-gaps
workspace instead, scoped to the review group the discography scan already built for it — so the
`group_id` on an `incomplete` row is a live handle needing no separate scan. `/lb/gap` reads one such
group (missing tracks, ranked sources, the running search); `/lb/gap/{auto,fetch,cancel,rescan}`
act on it. Everything else in that workspace, including the whole-library `/api/gaps` list, stays
off the wire. `/lb/gap` is the one route the hub *projects*: it drops each source's peer file
listing, which is hundreds of KB no client renders.

Missing releases are **not** a shelf of their own: they are grouped by the same release-type key the
owned albums use and rendered inside those sections (an unowned album sits in *Albums*, faded and
dashed, next to the ones you have; a partly-owned one carries its `9/12` count). Navic builds that
shelf **Navidrome-first** — lb-bot's list is its own view of the artist, and an album whose Navidrome
record its matcher couldn't claim has no row at all, so starting from lb-bot's list would hide albums
the user owns. When a fill lands, lb-bot flips its own index row to `present`
and — if `LB_BOT_HUB_URL`/`LB_BOT_HUB_TOKEN` are set — POSTs `<hub>/lb/notify`, the one inbound
route, which the hub fans out as a `library` broadcast so open pages on other devices refresh too
(`PROTOCOL.md` §15.1). Both are needed: without the index flip a filled album shows up twice — and
the page also reconciles missing rows against the albums Navidrome actually holds, since anything
that fills the library *outside* a tracked fill leaves the index row stale until the next rescan.
Downloads take a per-album `quality` (the global Source preference is the wrong granularity), and
the watch survives a restart.

**Deezer, paste-a-link and the wishlist.** Four more route groups, all whitelisted in
`LB_ROUTES` (`PROTOCOL.md` §15). `/lb/deezer/chart` and `/lb/deezer/editorial` are Deezer's free
unauthenticated browse, ownership-marked; `/lb/artist/related` is Deezer as a **third** similarity
source, deliberately its own route rather than more rows on `/lb/artist/similar` — that merge is a
ranking two providers agree on, and folding a third into it would move every existing row's
position. `POST /lb/resolve-link` turns a pasted streaming URL into MBIDs, which is why
`confidence` is on the wire: a MusicBrainz URL resolves with no network call, a Spotify/Deezer id
through that provider's API, and everything else by *searching* MusicBrainz for what was scraped
out of the URL. `/lb/wishlist` is the persisted home for a `no_source` failure — the one state
that deliberately never auto-retries, so a wishlist is where `retryable: false` becomes an action
instead of a dead end.

All four are in `LB_LIBRARY_ROUTES`, but for two different reasons worth keeping straight. The
Deezer rows and `artist/related` are cached six hours because a chart barely moves — what moves is
the ownership badge on each row, and a stale one offers to fetch a record already on disk. The
wishlist is there for the opposite reason: a landing is precisely what takes a row *off* it.

**A download is reviewed, not fired blind.** `/lb/album/sources` returns the ranked Soulseek
folders with coverage paired against the canonical MusicBrainz tracklist (never a file count) and
an explicit "is this the right album" verdict; the client shows those, and the chosen peer rides
along with the download. This exists because the one-tap version fetched the wrong record for a
self-titled album — where every candidate folder's name looks plausible — and nothing before or
after the fact said so. The per-album `quality` is a *ranking* term upstream, not a filter, so
seeing the real format in the source row is the only thing that actually answers "what am I
getting".

**The client sends the release it resolved** (`release_mbid` + artist/title/track count) with both
the source search and the download, and lb-bot prefers it over re-resolving the release-group. Two
reasons: its resolver picks "official, earliest" on its own, so the edition picker was otherwise
decorative; and it caches a transient MusicBrainz failure for five minutes and answers `{}` without
retrying inside that window — one 503 turned into a hard "Could not resolve album" on that album for
every later attempt, with nothing the user could do.

Three things it is easy to get wrong, all settled in the design doc: covers for unowned releases
come **straight from the Cover Art Archive** (lb-bot's `/api/cover` is Navidrome art keyed by a
Navidrome album id); download progress comes from `/lb/album/status`, **not** from the task id
the download returns — that task completes when slskd accepts the enqueue, about a minute before
anything reaches the library; and a client must **not** drop an lb-bot row the moment it stops
saying `missing`. A completed fill flips that row to `present` while the local library cache still
has no album for it, so filtering on `missing` makes an album vanish from the page *because* the
download succeeded.

---

## 6. Features (current)

### Core remote-control (confirmed working)
- Hub + transfer-with-resume; Feishin ⇄ Navic ⇄ Chromecast in any direction, including paused transfers.
- **Feishin unified player**: one player bar + side queue drive local *or* remote via transport
  interception (no separate remote UI); startup runaway-audio watchdog; remote-aware Auto DJ; cast
  bridge with dead-socket recovery + running-session re-adoption.
- **Navic unified player**: a blended `uiState` mirrors the session across mini-player, now-playing,
  queue, and artwork pager; **Android notification/lock-screen/Bluetooth controls drive the remote
  session** (via a `RemoteSessionPlayer` media3 facade).
- Device pickers on both: name + platform + status (Desktop/Android/Cast · playing/online/offline/this
  device), with hide/offline management and a remote volume slider.

### Library & playback features
- Star ratings + "Favorites"; similar-songs radio, artist radio, **Song Journey** (both clients).
- Home rows: Most-played + Newly-added (`getAlbumList2 frequent/newest`).
- **Smart playlists**: Navic editor → Navidrome native `rules` API; Feishin already has a query-builder.
- **Playlist downloads** with quality/format + rolling-vs-permanent cache (both clients). Navic also has
  a **Download Center** (status/queued/failed/retry/repair, per-policy ownership, Wi-Fi-only /
  charging-only / configurable concurrency constraints, download-next-N) and **Saved Queues**
  (auto-saved, session-typed as manual/album/playlist/radio/moodFlow/journey, with restore/resume/
  save-as-Navidrome-playlist).
- **Queue undo** (Navic): short-lived undo for clear/remove/move/play-now-replace, local and remote.
- **Saved Queues + Continue Listening** on both clients, **synced through the hub** (§4): one shared,
  live-updating history (session-kind tagged, resume-at-position, save-as-Navidrome-playlist), the
  current queue highlighted, offline-reconciled. Cached-library ("sync failed") banner on the Navic home.
- Metered/cellular transcode profile (Feishin).

### AudioMuse recommendation layer
- **Tier 1** both clients (Instant Mix, Artist Radio, Song Journey) behind a capability probe.
- **Tier 2** — Navic: Sonic Fingerprint autoplay, Adaptive **Mood Flow** (skip/play-through signals →
  alchemy centroid), Echo/Steady/Transition **character presets**, adaptive visualizer w/ mood-reactive
  palette. Feishin: autoplay-source dropdown (Auto DJ / Fingerprint / Mood Flow), Mood Flow signals,
  blob visualizer palette.
- **CLAP text→mood search** (both); a scoped **generator chip** naming the active generator + centroid tint.
- Autoplay modes (one control, four): **Off / Similar / Sonic Fingerprint / Adaptive** — modes needing
  Tier 2 grey out until configured.

### Known open items (see `TESTING-SETUP.md` §8)
- Feishin mood-palette/Haze/energy-motion parity. *(Mood Flow re-splice loop + character-param wiring
  landed 2026-07-20 — bounded re-centroid passes + Echo/Steady/Transition presets.)*
- Navic **native cast lifecycle re-adoption** after a process restart (crash is fixed; lifecycle isn't).
- Expressive-blur (Haze) expansion beyond NowPlaying.
- Deferred Navic library QoL (need a compiler in the loop): alphabet fast-scroll jump list, recently-added
  **songs** row (no local added-date column), downloaded-only filters on artist/album/playlist lists.
- Navic's tablet side rail is not themed: it sits outside the navigation host, so it cannot read the
  per-screen cover ambient. Cosmetic.
- Android Auto knows nothing about the hub — its stream resolution builds a *local* Navidrome URL, so
  browsing from Auto during a remote session is untested.

---

## 7. Where the code lives (integration points)

### Hub — `hub/hub.py`
Single file. `Hub` class: `handler` (per-connection), `_on_act`, `_on_report`, `_transfer`,
`_disconnect`, `_broadcast_*`. Proxies: an `HttpProxy` base (route whitelist, token check,
concurrency cap, per-route TTL cache) with `SonicProxy`/`SONIC_ROUTES`, `LbProxy`/`LB_ROUTES` and
`PreviewProxy`/`PREVIEW_ROUTES` on top, dispatched in order by `_build_proxy_protocol` (a `websockets` protocol subclass — the legacy
server rejects non-GET and hides the request body from a plain `process_request` callable).
`PreviewProxy` is the one that overrides `call` to rewrite a body (signing `streamUrl` after the
cache — read its docstring before "unifying" it with the others). "Mixed for You" is `self.mixes` +
`_sanitize_mix`/`_mixes_list`/`_broadcast_mixes` and the four `act` branches, modelled line for
line on the saved-queue ones.
`hub/tools/` has manual test scripts (`fake_receiver.py`,
`controller.py`, `test_transfer.py`) and eleven automated suites. Docker via `hub/Dockerfile` +
`docker-compose.yml`.

### Preview sidecar — `preview/preview.py`
Single file, stdlib + `yt-dlp`. `serve_client` is a hand-rolled HTTP/1.1 server on
`asyncio.start_server`, deliberately not a framework: the one hard constraint is that nothing may
buffer a whole response body. `_choose_format`/`_format_mime` decide what is served and what it is
called, and they must stay consistent — see §5.

### Feishin (renderer unless noted)
- **Hub transport (main):** `src/main/features/core/hub/index.ts` (+ preload `src/preload/hub.ts`,
  settings in `settings.store.ts`).
- **Protocol hook:** `src/renderer/features/hub/hooks/use-hub.tsx` (maps `do`→player, reports ~1 Hz,
  startup runaway watchdog).
- **Unified bar (no separate remote bar):** transport interception in
  `features/player/context/player-context.tsx` → `features/hub/utils/remote-queue.ts` (`remoteAct`);
  display via `features/hub/hooks/use-remote-aware.ts`. Side queue: `features/now-playing/components/play-queue.tsx`.
- **Auto DJ / AudioMuse:** `features/player/hooks/use-auto-dj.ts`, `features/player/auto-dj/*`
  (`audio-muse-source.ts`, `mood-flow-signals.ts`), main-process client
  `src/main/features/core/audiomuse/index.ts` (`endpoint()` picks hub vs. direct).
- **Cast bridge:** `src/main/features/core/cast/index.ts` (`CastDeviceBridge`, `adoptRunningSession`,
  `cast-bridged-devices` IPC); cast scrobbling in `features/player/hooks/use-cast-scrobble.ts`.
- **lb-bot:** main-process client `src/main/features/core/lbbot/index.ts` (hub-only; writes answer
  with a `{ok,status,error}` result and log every non-2xx) + preload `src/preload/lbbot.ts`; wire
  shapes in `src/shared/types/lbbot-types.ts`; renderer `features/lbbot/*` — missing-album tiles
  mixed into the artist page's release-type sections by
  `features/artists/hooks/use-artist-albums-grouped.ts`, the two-step `missing-album-modal`, the
  `gap-fill-modal`, their shared `source-list`, and a persisted store holding both album fills and
  gap fills. The gap action hangs off the album context menu
  (`features/context-menu/actions/find-missing-tracks-action.tsx`); incomplete albums carry an
  `N missing` badge from the index row's `present`/`total`.
- **Sonic UI:** `features/sonic/*` (generator chip, CLAP search modal, palette).
- **Visualizer:** `features/visualizer/components/blob/*` + `hooks/use-track-mood.ts`.

### Navic (commonMain unless noted)
Navic lives in **[navic-gaps](https://github.com/illuminator69/navic-gaps)** and keeps its own
`CLAUDE.md`, which is the current map of that tree — the hub client, the ExoPlayer↔`RemoteSessionPlayer`
swap, the hand-rolled castv2 bridge, the lb-bot surface, the cover-colour engine, and the upstream
merge playbook. The integration points from this side:

- **Hub client:** `domain/manager/HubManager.kt` (`act*` helpers, `resolveQueue` 1:1 placeholder
  resolution, remote mirror).
- **Unified player:** `shared/MediaPlayer.kt` (blended `uiState` + raw `localUiState`);
  `androidMain/.../shared/RemoteSessionPlayer.kt` (a media3 `SimpleBasePlayer` facade, so the
  notification, lock screen and Bluetooth keys drive the *remote* session);
  `androidMain/.../shared/MediaPlayer.android.kt` (a `MediaLibraryService` since alpha59).
- **Cast:** `androidMain/.../domain/manager/cast/*` — `NsdManager` discovery plus a hand-rolled
  castv2 client. No Cast SDK, no Play Services. Scrobbling in `domain/manager/CastScrobbler.kt`.
- **AudioMuse/radio:** `domain/manager/{RadioManager,AudioMuseManager}.kt`,
  `domain/models/settings/{AutoplayMode,MoodCharacter}.kt`,
  `ui/screens/nowPlaying/components/controls/{AdaptiveMoodBackground,NowPlayingAutoplaySelector}.kt`.
- **Native API / downloads / saved queues:** `domain/manager/{NativeApiManager,PlaylistDownloadManager}.kt`,
  `ui/screens/settings/DownloadCenterScreen.kt`, `ui/screens/savedqueues/*`.
- **Theming:** `ui/util/CoverColorScheme.kt`,
  `ui/components/common/{CoverAmbientBackground,BlendBackground,blur/ExpressiveBlur}.kt`.

---

## 8. Build & run

### Hub
```
cd hub
# create .env from .env.example: HUB_TOKEN, NAVIDROME_URL, HUB_MIRROR_PLAYQUEUE, HUB_ND_USER/PASS,
#                                AUDIOMUSE_URL + AUDIOMUSE_TOKEN (Tier-2 proxy; unset = disabled),
#                                LBBOT_URL (lb-bot proxy; unset = disabled),
#                                PREVIEW_URL + PREVIEW_PUBLIC_URL + PREVIEW_SECRET (preview
#                                sidecar; unset = disabled). PREVIEW_SECRET is NOT HUB_TOKEN.
docker compose up -d          # or: python hub.py   (Python 3.11+, `websockets`)
```

### Preview sidecar
```
cd preview
# create .env from .env.example. PREVIEW_SECRET is required — it must match the hub's, and it
# must NOT be HUB_TOKEN: /stream is authorised by an HMAC in the URL because a Chromecast sends
# no headers, and HUB_TOKEN is the hub's whole administrative surface.
docker compose up -d          # or: python preview.py   (Python 3.11+, `yt-dlp`)
```

### Feishin (Electron)
In the [feishin-gaps](https://github.com/illuminator69/feishin-gaps) repo. Requires Node 20+ and
`corepack enable` (pnpm is version-pinned by the `packageManager` field), then `pnpm install`.
```
pnpm dev                                          # dev
pnpm run build && pnpm exec electron-builder --win --x64 --dir   # portable → dist/win-unpacked/Feishin.exe
```
- **Typecheck WITHOUT a deps re-check** (important — `pnpm run typecheck` re-checks deps and has broken
  the lockfile before):
  `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (renderer) and
  `... -p tsconfig.node.json ...` (main).
- **Installer is not viable** (Defender quarantines the unsigned NSIS build) → ship the **portable
  `dist/win-unpacked/`** folder. Proper fix would be code-signing.
- In Feishin settings, set hub **URL** (default `ws://localhost:4790`), **token**, device **name**, and
  **Public server URL** = `https://music.example.com` (rewrites stream/image origins for the cast bridge).

### Navic (Android / KMP)
In the [navic-gaps](https://github.com/illuminator69/navic-gaps) repo, branch `navi-connect`.
**`:androidApp`** is the Android *application* module; **`:composeApp`** is the shared KMP library.
Gradle provisions its own JDK 21 toolchain — don't set `JAVA_HOME`.
```
./gradlew :androidApp:assembleRelease      # → androidApp/build/outputs/apk/release/Navic.apk
```
**Test a *release* build** (debug Compose is misleadingly choppy). commonMain changes must still
compile for iOS (out of scope otherwise). With no `SIGNING_*` env vars set the APK is signed with
the debug key, which is what every installed copy trusts — see that repo's `CLAUDE.md` §3 before
assuming an install failure is anything else.

---

## 9. Conventions & gotchas
- Navic files use **tabs**; multi-line `Edit` matches are fragile — prefer matching single bare lines.
- Keep Tier-2 AudioMuse **fail-soft** (grey out + fall back to Tier 1 when the plugin/index is missing).
- Same for lb-bot, but stricter: not configured / unreachable / unindexed all render **nothing**.
  The artist page must look exactly as it does today whenever that layer is absent.
- Cast requires **publicly reachable** stream/cover URLs (`https://music.example.com`, not Tailscale/LAN IPs).
  The same applies to a **preview**: `PREVIEW_PUBLIC_URL` is what makes one castable, and without it
  the hub advertises `previewCastable: false` so both clients refuse the transfer *with a reason*
  rather than letting the speaker play silence.
- **`PreviewProxy` rewrites a response body, and that is against the house rule on purpose.** It
  signs `streamUrl` so `PREVIEW_SECRET` never leaves the two servers, and it does so in an override
  of `call` **after** `super().call` — i.e. after the cache. A resolution is cached six hours
  because an `ext:` id is stable; a capability is only good for `PREVIEW_TTL`. Signing before the
  cache stores the signature too, and every hit past the TTL serves a URL the sidecar rejects.
- **A mix has no tombstone and a saved queue does.** Not an oversight: a saved queue is published
  concurrently by every receiver that plays it, so a delete races a re-publish; a mix is written
  only by the user, through an explicit act, and nothing republishes it. `touchMix` likewise moves
  `lastPlayedAt` and never `updatedAt` — that is the sort and eviction key.

---

## 10. Directory map
```
navi-connect/
  README.md                      the project description (start there)
  TESTING-SETUP.md               prerequisites, step-by-step setup, smoke test, known bugs/caveats
  PROTOCOL.md                    wire protocol spec
  CLAUDE.md                      working notes for coding agents
  docs/ARCHITECTURE.md           ← this file
  docs/screenshots/              images used by the README
  hub/                           Python relay hub — hub.py, Dockerfile, docker-compose.yml, tools/
  preview/                       yt-dlp preview sidecar — preview.py, Dockerfile, docker-compose.yml,
                                 tools/. The MEDIA origin; the hub proxies its control plane only
```
Both clients and lb-bot live in their own repositories; see **Repositories** in the
[README](../README.md).


