# navi-connect

**Your music library, finished and shared.** navi-connect joins two self-hosted projects into one:
a Spotify-Connect–style **shared playback session** across every device, and **lb-bot**, which knows
every album your library is missing and can fetch it — from inside the player, on the artist page
you're already looking at.

It sits on top of [Navidrome](https://navidrome.org) and needs nothing hosted by anyone else.

### ▶ [**Setup & testing guide →**](TESTING-SETUP.md)

**If you want to run this, start there, not here.** It covers prerequisites, step-by-step setup, a
smoke test that isolates failures, and every known bug and caveat. Prebuilt clients are on the
[Releases](../../releases) page — no toolchain required.

---

## What you get

### The missing half of your library, visible

Open an artist and the albums you *don't* have sit right there among the ones you do — greyed out and
marked, a partly-owned album carrying its missing count. Nothing is a separate "wanted" list; the gaps
live where you'd notice them.

<p align="center">
  <img src="docs/screenshots/feishin-artist-missing-albums.png" width="720"
       alt="Feishin artist page showing owned albums alongside greyed-out ones marked NOT IN LIBRARY, with a +38 MISSING badge" />
</p>

**A download is reviewed, not fired blind.** Pick the edition, check it against the canonical
MusicBrainz tracklist, choose a quality, and only then go looking for sources.

<p align="center">
  <img src="docs/screenshots/feishin-missing-album-review.png" width="720"
       alt="Missing-album dialog in Feishin showing edition tabs, media-format tabs, the full tracklist, a quality selector and a Find sources button" />
</p>

The same surface on Android — note the mini-player reading **Playing on Feishin**: the phone is
controlling a session running on the desktop.

<p align="center">
  <img src="docs/screenshots/navic-artist-missing-albums.png" width="290"
       alt="Navic artist page with albums marked 7 missing, 3 missing and Not in your library" />
  <img src="docs/screenshots/navic-missing-album-review.png" width="290"
       alt="Navic bottom sheet showing edition options, tracklist, quality preference and Find sources" />
</p>

Behind it, lb-bot's own workspace handles the per-track gap filling — which tracks are absent, which
sources are ready, and what it's working on.

<p align="center">
  <img src="docs/screenshots/lb-bot-fill-gaps.png" width="720"
       alt="lb-bot Fill gaps workspace showing an album with 14 of 16 present, 2 tracks missing, and the missing track list" />
</p>

### Everything else

- **One playback session, every device.** Control what's playing on one device from another; move
  playback mid-song to your desktop, your phone, or a Chromecast, and it resumes on the same beat.
- **Recommendations that follow you.** AudioMuse-AI similar-songs radio, artist radio, Song Journey,
  text→mood search, and an adaptive "Mood Flow" that reacts to what you skip — playing on whichever
  device is currently active.
- **Continue Listening across clients.** A shared, hub-owned queue history, so what you started on
  your phone is waiting on your desktop.

Both halves are optional and fail soft: no lb-bot means the discography surface simply isn't there,
no AudioMuse means those features grey out. The core remote-control layer needs neither.

---

## How it fits together

Four pieces, three of them optional:

| | |
|---|---|
| **Hub** | A headless relay that owns the *session intent* — queue, position, which device is active — and never touches audio. Python, one file, ~200 lines of protocol. Lives in this repo under `hub/`. |
| **Clients** | Every client is both a controller and a receiver. **Feishin** (desktop) and **Navic** (Android) each connect over WebSocket and can play, or hand playback to someone else. |
| **AudioMuse-AI** | Optional. The recommendation engine behind radio, Song Journey and Mood Flow. Reached through the hub, so no device carries its credentials. |
| **lb-bot** | Optional. Indexes each artist's full MusicBrainz discography, knows what the library lacks, and can acquire it. Also reached only through the hub. |
| **Preview sidecar** | Optional. Lets you hear a track the library doesn't have, in the ordinary queue. Its own process under `preview/`, because it is the one thing that serves *audio* — the hub proxies its control plane and never a byte of media. |

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the whole design, and
[`PROTOCOL.md`](PROTOCOL.md) for the wire spec.

## Repositories

| Repo | What's in it |
|---|---|
| **[navi-connect](https://github.com/illuminator69/navi-connect)** (you are here) | The hub, the preview sidecar, the docs, and the [prebuilt releases](../../releases). |
| **[navic-gaps](https://github.com/illuminator69/navic-gaps)** | The Android client — a fork of [ssalggnikool/Navic](https://github.com/ssalggnikool/Navic). Kept separate so upstream Navic releases can still be merged with `git merge v1.0.0-alphaNN`. GPL-3.0. |
| **[feishin-gaps](https://github.com/illuminator69/feishin-gaps)** | The desktop client — a fork of [Feishin](https://github.com/jeffvli/feishin). Separate for the same reason: flattening ~4,700 commits of upstream history into this tree would destroy the merge path. GPL-3.0; this is also where the binaries' source lives. |
| **[lb-bot](https://github.com/illuminator69/lb-bot)** | The library-gap filler. An independent service with its own release cycle, useful on its own, and optional here. |

## Contents of this repo

```
hub/                  the relay — hub.py, Dockerfile, docker-compose.yml, tools/
preview/              the preview sidecar — preview.py, Dockerfile, docker-compose.yml, tools/
docs/ARCHITECTURE.md  what each piece is, how they talk, and where the code lives
docs/screenshots/     images used by this file
PROTOCOL.md           the wire protocol spec
TESTING-SETUP.md      prerequisites, setup, smoke test, known bugs and caveats
```

## Licence

The hub and the docs are this project's own work. Both clients are GPL-3.0 forks and carry their
upstream projects' licences — all credit for Navic belongs to
[paige](https://github.com/ssalggnikool), and for Feishin to
[jeffvli](https://github.com/jeffvli) and their contributors.
