# navi-connect — setup & testing guide

How to stand the whole stack up from nothing, in the order that works, plus the caveats you should
know *before* you start blaming your config. Written for a fresh tester on a fresh machine.

Read `README.md` first if you want to know what the project *is*. This file only covers getting it
running and what to expect once it is.

---

## Contents

- [0. Read this first](#0-read-this-first)
- [1. The shape of the thing](#1-the-shape-of-the-thing)
- [2. Prerequisites](#2-prerequisites)
  - [2.1 Server side (a NAS, a Linux box, Unraid — anything that runs Docker)](#21-server-side-a-nas-a-linux-box-unraid--anything-that-runs-docker)
  - [2.2 Build machine (Windows, for the clients)](#22-build-machine-windows-for-the-clients)
  - [2.3 The public-URL requirement (read this one)](#23-the-public-url-requirement-read-this-one)
- [3. Step 1 — the hub (start here)](#3-step-1--the-hub-start-here)
- [4. Step 2 — AudioMuse (optional, for recommendations)](#4-step-2--audiomuse-optional-for-recommendations)
- [5. Step 3 — lb-bot (optional, for library-gap filling)](#5-step-3--lb-bot-optional-for-library-gap-filling)
- [6. Step 4 — the clients](#6-step-4--the-clients)
  - [Option A: download prebuilt (recommended for testing)](#option-a-download-prebuilt-recommended-for-testing)
  - [Option B: build from source](#option-b-build-from-source)
  - [Feishin (Windows desktop)](#feishin-windows-desktop)
  - [Navic (Android)](#navic-android)
- [7. Smoke test — the order that isolates failures](#7-smoke-test--the-order-that-isolates-failures)
- [8. Known issues, caveats, and what's untested](#8-known-issues-caveats-and-whats-untested)
  - [8.1 Confirmed working](#81-confirmed-working)
  - [8.2 Known bugs and limitations](#82-known-bugs-and-limitations)
  - [8.3 Built but NOT yet tested — the highest-value places to look](#83-built-but-not-yet-tested--the-highest-value-places-to-look)
- [9. Security notes](#9-security-notes)
- [10. Credential hygiene when you deploy this](#10-credential-hygiene-when-you-deploy-this)

---

## 0. Read this first

- **This is a single-user, personal-infrastructure project.** There is no multi-tenant story, no
  account system, no hosted instance. Every component assumes it is the only one, talking to one
  Navidrome account.
- **Nothing here is signed.** The Feishin binary is unsigned, so Windows Defender objects and the
  NSIS installer is unusable (§6); the Navic APK is signed with a debug key unless you supply your
  own.
- **Several major subsystems are built but not yet field-tested** — most of the Chromecast recovery
  work and the whole lb-bot client surface. §8 lists exactly which. If you are testing, those are
  the interesting parts.
- **Two components live in sibling repositories** — the Feishin fork and lb-bot. See the README for
  why, and §6 for prebuilt binaries if you'd rather not build them.

---

## 1. The shape of the thing

```
                        ┌─────────────┐
                        │  Navidrome  │  music server + library (required)
                        └──────┬──────┘
                               │ Subsonic API + direct audio streams
        ┌──────────────────────┼──────────────────────────┐
        │                      │                          │
   ┌────┴────┐           ┌─────┴─────┐              ┌─────┴─────┐
   │ Feishin │           │   Navic   │              │Chromecast │
   │ desktop │           │  Android  │              │ (speaker) │
   └────┬────┘           └─────┬─────┘              └─────┬─────┘
        │                      │                          │
        │  WebSocket :4790     │                     bridged by whichever
        └──────────┬───────────┴──────────────────────────┘  client saw it first
                   │
             ┌─────┴──────┐
             │    Hub     │  session state + command routing. NO audio.
             └─────┬──────┘
                   │  HTTP proxy, credentials injected server-side
        ┌──────────┴──────────┐
   ┌────┴─────┐         ┌─────┴────┐
   │AudioMuse │         │  lb-bot  │   both optional
   └──────────┘         └──────────┘
```

**Only Navidrome and the hub are required.** AudioMuse and lb-bot are independently optional — without
them the features they power grey out or disappear entirely, by design. Start without them.

---

## 2. Prerequisites

### 2.1 Server side (a NAS, a Linux box, Unraid — anything that runs Docker)

| Thing | Required? | Notes |
|---|---|---|
| **Docker** + an external network named `media` | yes | `docker network create media` if it doesn't exist. Both compose files declare it `external`. |
| **Navidrome** ≥ 0.62.0 | yes | With at least one user and a scanned library. |
| **A publicly reachable HTTPS URL for Navidrome** | only for Chromecast | See §2.3 — this is the single most common setup failure. |
| **AudioMuse-AI** | no | Two tiers, see §4. Tier 1 is a Navidrome plugin; Tier 2 is its own container. |
| **slskd** | only for lb-bot | Soulseek client; lb-bot's acquisition backend. |
| **lb-bot** | no | Its own repository (linked from the README). Needs slskd + a writable library mount. |

### 2.2 Build machine (Windows, for the clients)

| Thing | For | Version |
|---|---|---|
| **Node.js** | Feishin | 20 LTS, then `corepack enable` (pnpm 11.5.2 is pinned via `packageManager`) |
| **JDK** | Navic | **21** — the build sets `JavaVersion.VERSION_21` / `JVM_21`. Microsoft OpenJDK 21 works. |
| **Android SDK** | Navic | `compileSdk`/`targetSdk` **37**, `minSdk` 24, AGP 9.2.1, Kotlin 2.3.21. Path goes in `navic/local.properties` (gitignored). |
| **Python** | hub (non-Docker) | 3.11+ with `websockets`. Skip if you run the hub in Docker. |
| An Android device | Navic | Android 7.0+ (minSdk 24). A **release** build — see §6. |

### 2.3 The public-URL requirement (read this one)

A Chromecast fetches audio **itself**, directly from Navidrome. It is not on your Tailscale network
and it does not know your LAN names. If Navidrome's URL as the clients know it is a LAN IP or a
Tailscale address, casting will fail — usually silently, or as a track that loads and immediately
ends.

If you want to test casting, Navidrome needs a URL reachable from the speaker, and Feishin's
**Public server URL** setting must be set to it. If you don't care about casting, ignore all of this
and use a LAN address everywhere.

---

## 3. Step 1 — the hub (start here)

```bash
cd hub
cp .env.example .env      # then edit it
docker compose up -d      # or: pip install -r requirements.txt && python hub.py
```

The compose file pulls a prebuilt image from **GHCR** (`ghcr.io/<owner>/navi-connect-hub`), published
automatically on every push to `main`. To update:

```bash
docker compose pull && docker compose up -d
```

To build from source instead, comment out the `image:` line and uncomment `build: .`.

Minimum viable `.env` — everything else can stay commented out:

```ini
HUB_TOKEN=<a long random string you invent>
HUB_PORT=4790
```

`HUB_TOKEN` is the shared secret every client presents. There is no other auth. Generate something
real (`openssl rand -hex 32`); the proxies in §4 and §5 **refuse to start** if it's empty, which is
deliberate.

Optional, and worth adding once the basics work:

| Variable | Does what |
|---|---|
| `NAVIDROME_URL`, `HUB_ND_USER`, `HUB_ND_PASS`, `HUB_MIRROR_PLAYQUEUE=true` | Mirrors the session to Navidrome's `savePlayQueue` so unmodified Subsonic clients resume where you left off. |
| `HUB_HEALTH_PORT` (default `HUB_PORT`+1) | Plain-HTTP health endpoint for Docker healthchecks. |
| `HUB_DEVICE_TTL_DAYS` | Drops devices unseen for N days at startup. `0` = keep forever. Set it to something once your picker fills with dead test devices. |
| `HUB_DEBUG=true` | Per-frame logging at ~1 Hz. Very noisy. Turn it on when playback state misbehaves, off otherwise. |

The hub persists session + device state to `./data`. To reset a test to a clean slate, stop the
container and delete that directory.

**Verify:** `docker logs navi-connect-hub` shows it listening on 4790. `hub/tools/fake_receiver.py`
and `hub/tools/controller.py` let you drive a session with no real client at all — the fastest way to
tell a hub problem from a client problem.

---

## 4. Step 2 — AudioMuse (optional, for recommendations)

Two independent tiers. You can have either, both, or neither.

**Tier 1** — an AudioMuse plugin loaded into Navidrome. Zero config on our side: the clients probe
`getOpenSubsonicExtensions` and light up Instant Mix, Artist Radio and Song Journey if the server
advertises `sonicSimilarity`. Instant Mix works against vanilla Navidrome even without the plugin.

**Tier 2** — the AudioMuse core HTTP API, its own container. **Configure it on the hub, not on the
clients:**

```ini
AUDIOMUSE_URL=http://audiomuse:8000
AUDIOMUSE_TOKEN=<your audiomuse api token>
```

The hub proxies a five-route whitelist on `/sonic/*` and injects the token and the Navidrome
password server-side, so no phone or desktop ever holds them. Leave `AUDIOMUSE_URL` unset to disable.

> Each client also keeps an *older* direct-to-AudioMuse config (host + bearer token in client
> settings) as a LAN fallback. Prefer the hub route. The direct fallback puts the API token on the
> device, which is exactly what the proxy exists to avoid.

**Caveats:**
- Tier 2 is **desktop-only in Feishin** — it runs through the Electron main process to dodge CORS.
  The web build has no Tier 2 and never will without a proxy.
- The index has to be **warm**. A cold AudioMuse answers the sonic endpoints with 404s and every
  Tier-2 feature greys out. That's fail-soft behaviour working correctly, not a bug — check the
  server before debugging the client.

---

## 5. Step 3 — lb-bot (optional, for library-gap filling)

lb-bot is a **separate repository**, not part of this one. It indexes each artist's full
MusicBrainz discography, knows what your library is missing, and can acquire it from Soulseek.

Its own Flask API **has no authentication and binds `0.0.0.0:8899`**. Because of that there is
deliberately **no direct-LAN fallback** in the clients: they reach it only through the hub, or not at
all. Don't expose port 8899 beyond your LAN.

lb-bot also publishes a prebuilt image to **GHCR** (`ghcr.io/<owner>/lb-bot`), so it updates the same
way the hub does — `docker compose pull && docker compose up -d`.

On the hub:
```ini
LBBOT_URL=http://lb-bot:8899
```
Unset = the entire discography/missing-album surface disappears from both clients. That is the
intended "off" state and the artist pages should look untouched.

On lb-bot, copy its **`.env.example`** to `.env` beside `docker-compose.yml` and fill it in —
Compose reads it automatically:

```bash
cp .env.example .env    # then edit
docker compose up -d
```

The variables that matter:

| Variable | Notes |
|---|---|
| `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` | Library truth + placement oracle |
| `SLSKD_URL`, `SLSKD_API_KEY` | slskd → Options → API Keys |
| `MBZ_CONTACT` | **Required by the MusicBrainz ToS.** A contact email. |
| `LISTENBRAINZ_USER` | Playlist/fresh-release sources |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Its Telegram control surface |
| `LB_BOT_HUB_URL`, `LB_BOT_HUB_TOKEN` | **Set these.** The hub's **http://** address (not the `ws://` one clients use) and the same token as `HUB_TOKEN`. Without them a finished download won't push a refresh to open client pages — you'll see it on the next manual read instead. |
| `LASTFM_API_KEY`, `SPOTIFY_CLIENT_ID/SECRET` | Optional enrichment |

**Deployment gotchas that will bite you** (all from lb-bot's own docs):
- The container runs as `99:100` (Unraid `nobody:users`). All three mounts — `/config`, `/downloads`,
  `/music` — must be writable by that uid, `/downloads` included, because placement *unlinks* the
  source after copying.
- Files left by an earlier root-owned run cause `attempt to write a readonly database`. One-time fix:
  `chown -R 99:100 /mnt/user/appdata/lb-bot`.
- Set `ND_RECENTLYADDEDBYMODTIME=true` **on the Navidrome container**, or gap-filled albums never
  surface in "newest" — Navidrome derives `album.created_at` from the oldest file in the folder.

---

## 6. Step 4 — the clients

### Option A: download prebuilt (recommended for testing)

Both clients are attached to this repository's **Releases** page, so you don't need Node, a JDK, or
the Android SDK just to try the thing:

| Asset | What it is |
|---|---|
| `Feishin-portable-win-x64.zip` | Unzip anywhere, run `Feishin.exe`. No installer — see the note below. |
| `Navic.apk` | Sideload it. Signed with a debug key, so Android will warn about an unknown source. |

Two things to expect, both benign and both consequences of not paying for code-signing certificates:

- **Windows Defender / SmartScreen will complain** about the unsigned Feishin binary. This is also
  why there's no installer at all — see Option B.
- **Android will warn** about installing from an unknown source, and you may need to allow it for
  whatever app you're sideloading from.

Then skip to the configuration bullets at the end of each Option B subsection below — you still need
to point each client at your hub.

> **Source availability:** both clients are **GPL-3.0**. Navic's source is in `navic/` in this repo;
> Feishin's is in its own repository, linked from the README. The GPL entitles you to the
> corresponding source for any binary here, and that's where it is.

### Option B: build from source

### Feishin (Windows desktop)

```bash
cd feishin
pnpm install
pnpm dev                                            # development
pnpm run build && pnpm exec electron-builder --win --x64 --dir   # → dist/win-unpacked/Feishin.exe
```

**Use the portable `--dir` build.** The NSIS installer produces a working exe, but Windows Defender
quarantines the unsigned 223 MB binary during temp-extraction, even with install-folder exclusions.
Copy `dist/win-unpacked/` wherever you like and run `Feishin.exe`. The real fix is code-signing,
which isn't done.

Then in Settings:
- **Hub URL** — defaults to `ws://localhost:4790`; point it at your hub.
- **Hub token** — must match `HUB_TOKEN`.
- **Device name** — how it shows in every picker. Name it something you'll recognise.
- **Public server URL** — your public Navidrome URL, only if you're testing Chromecast (§2.3).

If you touch the code: typecheck with
`.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and the same for
`tsconfig.node.json`). **Do not use `pnpm run typecheck`** — it re-checks deps and has broken the
lockfile before.

### Navic (Android)

```bash
set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot
.\gradlew :androidApp:assembleRelease
```

Module layout catches people out: **`:androidApp`** is the Android application module, **`:composeApp`**
is the shared KMP library. The release task is `:androidApp:assembleRelease` (~3 min, ~12 MB APK).

- **Test the release build, not debug.** Debug Compose is dramatically choppier and will send you
  chasing performance problems that don't exist.
- With no `SIGNING_*` env vars, the APK is signed with the debug key. Fine for testing.
- Configure hub URL + token under Settings → navi-connect. The hub URL now defaults to **empty** —
  set it, or the client simply never connects (that's the intended off state, not a hang).
- commonMain must still *compile* for iOS. iOS is otherwise out of scope — no features, no testing,
  and the iOS target is **currently unverified** because there's no Kotlin/Native toolchain here.

---

## 7. Smoke test — the order that isolates failures

Do these in order. Each one only makes sense if the previous passed.

1. **Hub alone.** `fake_receiver.py` + `controller.py` from `hub/tools/`. Play, pause, next. If this
   fails, nothing else will.
2. **One real client.** Feishin connects, appears in its own device picker, plays local audio.
3. **Two clients.** Navic connects; each sees the other in the picker with the right platform and
   status.
4. **Transfer with resume.** Play on Feishin, transfer to Navic → same track, same position, still
   playing. Then reverse it. Then transfer while *paused* — that path is separate and has broken
   independently before.
5. **Queue edits from the far side.** Reorder, remove, play-next from the non-playing device.
6. **Android system controls.** Lock screen / notification / Bluetooth buttons should drive the
   *remote* session, not local playback.
7. **Saved queues.** Play an album → exactly **one** card appears on **both** clients, named after
   the album. Reorder and replay it → still one card, count doesn't grow.
8. **AudioMuse**, if configured. Instant Mix first (Tier 1), then autoplay modes (Tier 2).
9. **Chromecast**, if configured. Appears as `📺 <name>` in every picker; casting is just a transfer.
10. **lb-bot**, if configured. Artist page shows faded/dashed unowned albums inline with owned ones.

> **Restart the hub and lb-bot before testing the lb-bot surface.** Several of the newer routes
> silently do nothing against an older running hub — no error, just an empty result.

---

## 8. Known issues, caveats, and what's untested

### 8.1 Confirmed working
Hub and transfer-with-resume in every direction; Feishin's unified player bar and side queue; Navic's
unified player and Android media controls; ratings/favorites; similar-songs and artist radio;
playlist downloads; the Download Center; saved queues across both clients; native Chromecast from
Navic (basic operation confirmed live).

### 8.2 Known bugs and limitations

**Packaging**
- Feishin's NSIS installer is unusable — Defender quarantines the unsigned binary. Portable build only.

**Playback**
- **Navic:** starting *brand-new local* playback while another device is active is blocked by the
  media-session facade. Transfer to Navic first. This is by design but reads as a bug the first time.
- **Navic:** resume position derives from a normalized progress float × duration, not from an actual
  `positionMs` field — so resume is accurate to about duration/precision, not to the millisecond.
- **Feishin:** the remote favorite heart isn't optimistic; it reflects hub-published state, so it
  lags a beat. The star itself applies correctly.
- **Feishin:** remote queue rows have limited go-to-album/artist and remove/reorder. Jump and
  add-to-queue work.
- **Navic:** the remote *display* mirror still uses drop-based `resolveSongs` rather than the 1:1
  `resolveQueue` used for playback. If you ever see a display/playback mismatch on un-synced songs,
  that's where it is.

**Chromecast**
- Stream and cover URLs must be publicly reachable. A Tailscale/LAN server config breaks casting.
  See §2.3.
- A speaker genuinely powered off mid-session keeps its bridge for up to 90 s plus one failed TCP
  probe before the hub notices. Deliberate: a phantom receiver for 90 s is much cheaper than dropping
  a live one, which is what the old behaviour did every time the screen went off.
- Cast re-adoption is **join-only**. A receiver torn down while our session was *paused* is not
  rejoined on restart. There's no way to distinguish "our paused session" from "someone else's
  speaker" without launching the receiver, and launching it is the harmful act.
- Navic's native cast lifecycle after a **process restart** is still open (the crash itself is fixed).

**Saved queues**
- The 80 % track-overlap merge rule will fold two genuinely different queues into one card if they
  share most of their tracks (an album, and that album minus two songs). Intended trade — it's what
  stops duplicate cards — and it matches Feishin.

**lb-bot surface**
- Download progress comes from `/lb/album/status`, **not** from the task id the download returns.
  That task completes when slskd accepts the enqueue — roughly a minute before anything reaches the
  library.
- A completed fill flips lb-bot's index row to `present` while the local library cache still has no
  album for it. Briefly, an album can look like it vanished *because* the download succeeded.
- The per-album `quality` setting is a **ranking** term upstream, not a filter. You may not get the
  format you picked; the source row shows what you're actually getting.

**Performance**
- Debug Navic builds are misleadingly choppy. Always test release.
- `HUB_DEBUG=true` logs at ~1 Hz per device. Leave it off unless you're actively debugging.

### 8.3 Built but NOT yet tested — the highest-value places to look

- **Casting through a phone lock.** The whole point of the most recent work. Cast from Navic, lock
  the phone, wait past several minutes, reopen. Expected: the speaker never left the picker, the
  session is still live, nothing rewound. Watch whether **the queue advances while locked** at the
  same time — that answers an open question about whether Android is freezing the process at all.
- **Cast arbitration against a live Feishin.** Two clients that can both bridge, one speaker.
- Cast adoption after force-stop; paused idle-out; a DHCP address change mid-session.
- **Cast scrobbling**, including the negative case: with Feishin also running, Navic should be
  `BRIDGED_ELSEWHERE` and must **not** scrobble. Double-counting is the failure mode.
- **The lb-bot edition override end to end**, and the rescan spinner surviving until `scanned_at`
  moves. Needs hub + lb-bot restarted first.
- **The full saved-queue walk-through** with hub + Feishin + a fresh Navic APK together.
- **iOS compilation.** Mechanical edits, entirely unverified.

---

## 9. Security notes

This is personal infrastructure with a single shared secret, and it was never designed to be exposed.
Some specifics worth knowing before you deploy it anywhere:

- **`HUB_TOKEN` is the only authentication in the system.** It guards the WebSocket, the `/sonic/*`
  proxy and the `/lb/*` proxy alike. Make it long and random. Anyone holding it controls your
  playback and can drive lb-bot's downloads.
- **lb-bot's own API has no auth at all** and binds `0.0.0.0:8899`. Keep it off the public internet;
  reach it through the hub. The clients have no direct-LAN fallback for it precisely because of this.
- **Saved-queue records persist credentialed Subsonic stream URLs** in the hub's `state.json`,
  because the Chromecast bridge needs them. Treat that file as a secret. Stripping them from
  *history* records while keeping them on the live queue is a known, safe hygiene win — not done yet,
  because it changes what the cast bridge sees on a restore path and wants its own test.
- **The hub is the right place for credentials.** It holds the AudioMuse token and the Navidrome
  password server-side so no device carries them. Prefer the proxied routes over the direct-client
  fallbacks for exactly this reason.

---

## 10. Credential hygiene when you deploy this

Everything in this repository is configured from the environment — there are no credentials in the
source, and every secret-bearing variable defaults to empty or to a Docker service name. When you
stand up your own instance, keep it that way:

- **Never commit your `.env`.** `hub/.env.example` is the template; the real file is gitignored.
- **Don't hardcode your server address** in client defaults or placeholders. The clients ship with a
  blank hub URL and generic `192.168.1.10` examples on purpose.
- **`hub/data/` is gitignored** and should stay that way — it holds session state including
  credentialed stream URLs (see §9).
- If you fork this and push, **set a GitHub noreply address** on the repo
  (`git config user.email <id>+<user>@users.noreply.github.com`) before your first commit, or every
  commit will publish your real email.
