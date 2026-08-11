# navi-connect hub

Headless relay that gives Navidrome clients Spotify-Connect-style behaviour:
real-time queue control and transfer-with-resume between devices. The hub holds
the *session intent* and routes commands; **audio never flows through it** —
receivers stream from Navidrome themselves. Protocol: [`../PROTOCOL.md`](../PROTOCOL.md).

## Run

```bash
cp .env.example .env        # set HUB_TOKEN + Navidrome creds for the mirror
docker compose up -d --build
```

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
| `HUB_TOKEN` | *(empty)* | shared secret; clients must send it in `hello`. **Set it.** |
| `HUB_PORT` | `4790` | listen port |
| `HUB_STATE` | `/data/state.json` | persisted session + device registry |
| `NAVIDROME_URL` | *(empty)* | your Navidrome base URL; required for the savePlayQueue mirror |
| `HUB_MIRROR_PLAYQUEUE` | `true` | mirror the queue to Navidrome so unmodified Subsonic clients can resume |
| `HUB_ND_USER` / `HUB_ND_PASS` | *(empty)* | Navidrome creds the mirror authenticates with (single-user); the AudioMuse proxy reuses them |
| `AUDIOMUSE_URL` | *(empty)* | AudioMuse-AI core API; unset disables the Tier-2 proxy |
| `AUDIOMUSE_TOKEN` | *(empty)* | AudioMuse API token — server-side only, never sent to clients |
| `LBBOT_URL` | *(empty)* | lb-bot base URL; unset disables the `/lb/*` proxy and hides the surface in every client |

If the mirror creds are unset the hub still works fully; it just skips the
Navidrome write.

### AudioMuse Tier-2 proxy (`/sonic/*`)

With `AUDIOMUSE_URL` set, the same port also answers plain HTTP on a whitelist of
five AudioMuse routes, authenticated with `Authorization: Bearer $HUB_TOKEN` — so
no client holds the AudioMuse address, its token or the Navidrome password, and the
core API needs no exposure of its own. Routes, limits and the probe contract:
`../PROTOCOL.md` §14. Disabled (503) when `HUB_TOKEN` is empty, which would make it
an open relay.

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" http://<host>:4790/sonic/clap/stats
```

### lb-bot proxy (`/lb/*`)

With `LBBOT_URL` set, the same port proxies a whitelist of lb-bot's library-gap
routes: the artist discography read and scan, album editions/tracklist/sources,
the download and its progress poll, and the per-album gap actions
(`/lb/gap` + `auto`/`fetch`/`cancel`/`rescan`). Routes and rationale:
`../PROTOCOL.md` §15.

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

### Automated check

```bash
pip install -r requirements.txt
python tools/test_transfer.py     # asserts resume at the same index + position
```

## What clients must implement

Each client (Feishin fork, Navic fork) is both controller and receiver:

- connect, send `hello` with a stable device id + `caps`;
- as **receiver**: obey `do` (`load/play/pause/jump/seek/queueChanged/setVolume/
  setRepeat/setShuffle/release`) and emit `report` ~1 Hz while playing;
- as **controller**: send `act` (`setQueue/play/pause/next/previous/seek/jump/
  repeat/shuffle/volume/transfer/...`).

See `tools/fake_receiver.py` for the minimal receiver contract.
