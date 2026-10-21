# navi-connect hub

Headless relay that gives Navidrome clients Spotify-Connect-style behaviour:
real-time queue control and transfer-with-resume between devices. The hub holds
the *session intent* and routes commands; **audio never flows through it** —
receivers stream from Navidrome themselves. Protocol: [`../PROTOCOL.md`](../PROTOCOL.md).

## Run

```bash
cp .env.example .env        # set HUB_TOKEN + Navidrome creds for the mirror
docker compose pull && docker compose up -d
```

The compose file pulls the image CI publishes to GHCR on every push to `main`
(`ghcr.io/illuminator69/navi-connect-hub`); the same two commands update it. To
build locally instead, swap its `image:` line for `build: .`.

Or, matching the bot's `docker run` style on Unraid:

```bash
docker build -t navi-connect-hub .
docker run -d --name navi-connect-hub --restart unless-stopped \
  --network media -p 4790:4790 --env-file .env \
  -v /mnt/user/appdata/navi-connect:/data \
  navi-connect-hub
```

Clients then connect to `ws://<unraid-host>:4790`.

## Configuration (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `HUB_TOKEN` | *(empty)* | shared secret; clients must send it in `hello`. **Required**: the hub refuses to start without it |
| `HUB_ALLOW_INSECURE_NO_AUTH` | *(unset)* | `true` lets the hub run with no token, and only when `HUB_HOST` is a loopback address. For local experiments |
| `HUB_HOST` | `0.0.0.0` | listen address |
| `HUB_PORT` | `4790` | listen port |
| `HUB_HEALTH_PORT` | `HUB_PORT`+1 | plain-HTTP health endpoint, for Docker healthchecks |
| `HUB_STATE` | `/data/state.json` | persisted session + device registry |
| `HUB_DEVICE_TTL_DAYS` | `0` | drop devices unseen for this many days; `0` keeps them forever |
| `HUB_DEBUG` | `false` | per-frame logging at ~1 Hz; very noisy |
| `NAVIDROME_URL` | *(empty)* | your Navidrome base URL; required for the savePlayQueue mirror |
| `HUB_MIRROR_PLAYQUEUE` | `true` | mirror the queue to Navidrome so unmodified Subsonic clients can resume |
| `HUB_ND_USER` / `HUB_ND_PASS` | *(empty)* | Navidrome creds the mirror authenticates with (single-user); the AudioMuse proxy reuses them |
| `AUDIOMUSE_URL` | *(empty)* | AudioMuse-AI core API; unset disables the Tier-2 proxy |
| `AUDIOMUSE_TOKEN` | *(empty)* | AudioMuse API token — server-side only, never sent to clients |
| `LBBOT_URL` | *(empty)* | lb-bot base URL; unset disables the `/lb/*` proxy and hides the surface in every client |
| `LBBOT_WEB_URL` | `LBBOT_URL` | where a person opens lb-bot's web UI, for the clients' "Open in lb-bot" links. Set it when `LBBOT_URL` is a Docker-internal name |
| `PREVIEW_URL` | *(empty)* | the preview sidecar as the hub reaches it; unset disables `/preview/*` (`../preview/README.md`) |
| `PREVIEW_PUBLIC_URL` | *(empty)* | the sidecar as a client or a Chromecast reaches it; in practice required with `PREVIEW_URL` |
| `PREVIEW_SECRET` | *(empty)* | signs preview stream URLs; must match the sidecar's and must not be `HUB_TOKEN` |
| `PREVIEW_TTL` | `21600` | seconds a signed preview stream URL stays valid |
| `HUB_STATION_HISTORY` | `1` | `0` keeps the stations' play history (PROTOCOL §18.4) in memory only; otherwise it is written to `<HUB_STATE>.history` |
| `PROXY_QUEUE_TIMEOUT_FAST` | `10` | seconds a proxied request may wait for a free slot before the hub answers 503 `busy` |
| `HUB_SLEEP_MINUTE_S` | `60` | how many seconds one minute of the sleep timer lasts. Leave it alone: it exists so the tests can watch a timer run out in seconds |
| `HUB_MOOD_RETRY_S` | `60` | how soon a mood list whose build failed is tried again. Also a test knob |

`.env.example` explains each of these at more length.

If the mirror creds are unset the hub still works fully; it just skips the
Navidrome write.

Beside `state.json` the hub keeps `state.json.bak`: an fsynced copy, rewritten only
when a "Mixed for You" recipe or a saved-queue deletion changes — the two things no
client can send again. If `state.json` is empty or missing at start, the backup is
loaded instead. If it cannot be parsed it is first renamed to
`state.json.corrupt-<UTC timestamp>` and left for you to inspect; nothing reads or
removes those. `state.json.history` is the stations' play history (ids, times and a
hashed title key, no song names) and `state.json.stations` the stations themselves,
each written at most every 30 s. To reset the hub, delete all of them, not
`state.json` alone.

### AudioMuse Tier-2 proxy (`/sonic/*`)

With `AUDIOMUSE_URL` set, the same port also answers plain HTTP on a whitelist of
AudioMuse routes, authenticated with `Authorization: Bearer $HUB_TOKEN` — so
no client holds the AudioMuse address, its token or the Navidrome password, and the
core API needs no exposure of its own. Routes, limits and the probe contract:
`../PROTOCOL.md` §14. Disabled (503) when `HUB_TOKEN` is empty, which would make it
an open relay.

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" http://<host>:4790/sonic/clap/stats
```

### Stations (`/sonic/station/*`)

Radio and autoplay, computed here (`../PROTOCOL.md` §18): a station is a seed plus a
character (Close, Balanced, Explore), and the hub answers "the next N tracks" from
AudioMuse's own neighbourhood of the seed, falling back to Navidrome's similar-songs
agent when AudioMuse is down. It keeps what each station served, the session's skips
and play-throughs, and a 14-day play history, so a station never repeats itself or
what you played this fortnight. Enabled with `HUB_TOKEN` and either `AUDIOMUSE_URL`
or `NAVIDROME_URL` + `HUB_ND_USER` + `HUB_ND_PASS`; otherwise the probe answers
`configured: false` and the clients run their own fallback.

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" http://<host>:4790/sonic/station/characters
```

`tools/station_probe.py --env FILE --title "Cinnamon Girl" --artist "Lana Del Rey"` runs
the engine in-process against a real AudioMuse and Navidrome (read-only routes only)
and prints every batch with why each track was chosen.

A **custom radio** is a station seeded from up to eight artists, with a variety
(`low`, `medium`, `high`) and an optional mood filter. It is saved as a "Mixed for
You" recipe (`../PROTOCOL.md` §17.1).

### Moods (`/sonic/mood/*`)

Thirteen moods, each a list of your own songs ranked by AudioMuse's CLAP text search
(`../PROTOCOL.md` §18.5). Ten of them have four tiles each for Navic's Home. All
thirteen feed the queue's mood chips, and eight are offered as the radio's mood filter. The catalogue lives
in `hub.py` (`MOODS`), so a prompt can be reworded without a client release. Each list
is rebuilt every six hours. Enabled with `HUB_TOKEN` and `AUDIOMUSE_URL`, and needs
CLAP loaded in AudioMuse. Otherwise the catalogue answers `configured: false` or
`available: false`, and the clients show no moods.

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" http://<host>:4790/sonic/mood/catalogue
```

### lb-bot proxy (`/lb/*`)

With `LBBOT_URL` set, the same port proxies a whitelist of lb-bot's routes:
the artist discography read and scan, album editions/tracklist/sources, the
download and its progress poll, the per-album gap actions (`/lb/gap` +
`auto`/`fetch`/`cancel`/`rescan`), the index mirror, the overrides, the
listening mirror, and the read-only Needs attention and Source history
(`/lb/attention`, `/lb/activity`). Routes and rationale: `../PROTOCOL.md` §15.

**This whitelist is the only access control that exists.** lb-bot's Flask API has
no authentication at all and binds `0.0.0.0:8899`, and it exposes delete-file,
trash and preference-writing routes — so there is deliberately no direct-LAN
fallback in any client, no wildcard in the table, and `/api/tasks*` and the
whole-library `/api/gaps` list stay off it. Disabled (503) when `HUB_TOKEN` is
empty, for the same reason as the AudioMuse proxy.

`GET /lb/status` answers even when `LBBOT_URL` is unset, and lists the routes this
hub can proxy — a client that ships ahead of the hub can then say so instead of
failing on a silent 404. **Restart the hub after updating it**, or new routes stay
unrouted.

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" http://<host>:4790/lb/status
```

## Test it without real clients

Three terminals (uses `tools/`):

```bash
# 1. the hub
HUB_TOKEN=dev python hub.py
# 2. two simulated receivers
HUB_TOKEN=dev python tools/fake_receiver.py --name Living-Room
HUB_TOKEN=dev python tools/fake_receiver.py --name Bedroom
# 3. drive it
HUB_TOKEN=dev python tools/controller.py
#   > queue          (load + play on the first receiver)
#   > seek 65000
#   > devices        (note the index of "Bedroom")
#   > transfer 1     (Bedroom resumes at 65s — the Spotify handoff)
```

`fake_receiver.py --echo` takes the cap `cmdEcho` and keeps the receiver's side of
the command echo (PROTOCOL §5.3); `--stale-first`, `--settle-ms N`, `--never-settle`
and `--playing-after-clear` each break that contract in one way, so the hub's answer
can be watched. `--kind speaker --volume 35` makes it say what it is and how loud it
is (PROTOCOL §3.1, §5.3): the row in `controller.py`'s device list shows both, and
follows the level when the receiver changes it. With `--idle-volume` it also takes
the cap `idleVolume`, and `controller.py`'s `volume 30 --target <index>` then turns
it down while another receiver plays (PROTOCOL §5.1). `sleep 30` sets the session's
sleep timer, `sleep 0` clears it, and the controller prints what is left of it with
every session it is sent. `lag_proxy.py 4791 ws://127.0.0.1:4790 --delay-ms 300` relays the
WebSocket with a delay each way: point a client at the proxy to see what a control
does on a slow link (the hub's HTTP routes are not relayed).

### Automated check

```bash
pip install -r requirements.txt
for t in tools/test_*.py; do python "$t" || echo "FAILED: $t"; done
```

Every `tools/test_*.py` is a script that starts its own hub on a free port and exits
non-zero on failure. A few to start from: `test_transfer.py` (resume at the same
index and position), `test_cmd_echo.py` (`do.seq` / `report.applied`, reconnect, the
backstop), `test_act_ack.py` (`act.cid` / `ack`), `test_station_engine.py` (stations
against a fake AudioMuse and Navidrome).

## What clients must implement

Each client (Feishin fork, Navic fork) is both controller and receiver:

- connect, send `hello` with a stable device id + `caps`, and `kind` (what the
  device is: `computer`, `phone`, `tablet`, `speaker`, `tv`, `group`);
- report its own volume in `deviceState {volume}` after `welcome` and on every
  change (PROTOCOL §5.3);
- as **receiver**: obey `do` (`load/play/pause/jump/seek/queueChanged/clear/
  setVolume/setRepeat/setShuffle/release`) and emit `report` ~1 Hz while playing;
  with the cap `cmdEcho`, echo `do.seq` in `report.applied` (PROTOCOL §5.3); with
  the cap `idleVolume`, apply `do:setVolume` also while not the playing device;
- as **controller**: send `act` (`setQueue/play/pause/next/previous/seek/jump/
  repeat/shuffle/volume/sleepTimer/transfer/...`); with `"actAck"` in
  `welcome.features`, put a `cid` on an act to get an `ack` for it (PROTOCOL §5.4).
  `tools/controller.py` does, and prints each ack.

See `tools/fake_receiver.py` for the minimal receiver contract.
