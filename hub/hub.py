#!/usr/bin/env python3
"""
navi-connect hub — a headless Spotify-Connect-style relay for Navidrome clients.

See ../PROTOCOL.md for the wire protocol. The hub owns the *session intent*
(queue, order, repeat/shuffle, which device is active) and routes commands to
the active receiver; audio never flows through here — receivers stream from
Navidrome themselves. State is persisted so the queue survives restarts, and
(optionally) mirrored to Navidrome's native savePlayQueue for other clients.

Stdlib + `websockets` only. Python 3.11+.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http
import json
import math
import os
import random
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets

# --------------------------------------------------------------------------- #
# Config (all via env so the container is 12-factor)
# --------------------------------------------------------------------------- #
HOST = os.environ.get("HUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("HUB_PORT", "4790"))
# Plain-HTTP health endpoint (for Docker HEALTHCHECK / uptime probes). Defaults to PORT+1.
HEALTH_PORT = int(os.environ.get("HUB_HEALTH_PORT", str(PORT + 1)))
TOKEN = os.environ.get("HUB_TOKEN", "")
# Running with no token used to be a warning. It shouldn't be: the WebSocket is a full
# administrative surface even when the HTTP proxies correctly disable themselves without
# one. Anybody who can reach the port can enumerate devices and sessions, rewrite the
# queue, transfer playback, and claim an existing device id — registration deliberately
# supersedes the socket already holding it (close 4003). This hub is documented as sitting
# behind a publicly reachable server, so the default has to be "refuse to start".
#
# The escape hatch is explicit and loopback-only: a LAN experiment can set
# HUB_ALLOW_INSECURE_NO_AUTH=true, and then HUB_HOST must be a loopback address.
ALLOW_INSECURE_NO_AUTH = os.environ.get("HUB_ALLOW_INSECURE_NO_AUTH", "").lower() in (
    "1", "true", "yes", "on")
STATE_PATH = os.environ.get("HUB_STATE", "/data/state.json")
# Drop known devices not seen within this many days. 0 = keep forever.
# Runs on load AND on a periodic sweep (DEVICE_SWEEP_INTERVAL): without the sweep a
# long-lived hub accretes a picker row per device that ever connected — including a
# `cast-<id>` for every speaker ever seen on any network the bridging client visited.
DEVICE_TTL_DAYS = float(os.environ.get("HUB_DEVICE_TTL_DAYS", "7") or "0")
DEVICE_SWEEP_INTERVAL = 3600.0  # seconds between stale-device sweeps

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
MIRROR_PLAYQUEUE = os.environ.get("HUB_MIRROR_PLAYQUEUE", "true").lower() == "true"
ND_USER = os.environ.get("HUB_ND_USER", "")
ND_PASS = os.environ.get("HUB_ND_PASS", "")

# AudioMuse-AI core (Tier 2) — proxied over this same port so clients never hold
# the AudioMuse token or the Navidrome password. Unset URL = proxy disabled.
AUDIOMUSE_URL = os.environ.get("AUDIOMUSE_URL", "").rstrip("/")
AUDIOMUSE_TOKEN = os.environ.get("AUDIOMUSE_TOKEN", "")

# lb-bot (library-gap intelligence) — proxied for the same reasons plus one more:
# its Flask API has no authentication at all and binds 0.0.0.0:8899, so it can
# never be exposed directly. Unset URL = proxy disabled, clients hide the feature.
LBBOT_URL = os.environ.get("LBBOT_URL", "").rstrip("/")
# Where a PERSON opens lb-bot's own web UI — advertised to clients as `webUrl`
# (in `welcome.lb`, the `lb` frame and /lb/status) so a screen that has to hand
# the user over to lb-bot's workspace can link straight to it. Defaults to
# LBBOT_URL, which on a LAN setup is the same address; set it when people reach
# lb-bot through something else (a reverse proxy, a hostname). It is only a
# link: nothing is proxied through it and it grants nothing the hub token does
# not already reach on the same network.
LBBOT_WEB_URL = os.environ.get("LBBOT_WEB_URL", "").rstrip("/")

# yt-dlp preview sidecar — turns "what does this album I don't own sound like"
# into a playable track. The hub proxies its CONTROL plane only (/preview/resolve,
# /preview/status): ordinary buffered JSON, which is exactly what HttpProxy is for.
# The AUDIO never enters this process; clients fetch it from the sidecar directly.
# The reason is measured, not stylistic — see the comment on PreviewProxy.
PREVIEW_URL = os.environ.get("PREVIEW_URL", "").rstrip("/")
# The address a CLIENT can reach the sidecar's /stream on, which is not the same
# thing as the address the HUB reaches it on: PREVIEW_URL is typically an internal
# Docker hostname. Unset means previews still play for a client that can route to
# PREVIEW_URL itself, but a Chromecast cannot — so the hub advertises
# `previewCastable: false` and both clients refuse a cast transfer while an `ext:`
# track is queued, rather than letting the speaker play silence.
PREVIEW_PUBLIC_URL = os.environ.get("PREVIEW_PUBLIC_URL", "").rstrip("/")
# The stream-URL capability secret, shared with the sidecar and DELIBERATELY NOT
# HUB_TOKEN. A Chromecast fetches streamUrl itself and sends no headers, so the
# credential has to live in the URL — and HUB_TOKEN is the whole administrative
# surface of this hub, which must never appear in one. This secret authorises
# exactly one thing: reading one `ext:` id's audio until `exp`.
PREVIEW_SECRET = os.environ.get("PREVIEW_SECRET", "")
PREVIEW_TTL = int(os.environ.get("PREVIEW_TTL", str(6 * 3600)))

DEBUG = os.environ.get("HUB_DEBUG", "").lower() in ("1", "true", "yes")

# How long a bridge's reachability verdict stays authoritative. Bridges re-assert
# every DEVICE_STATE_INTERVAL (30 s, client-side); past this the last verdict is
# treated as unknown rather than kept asserting a speaker is up or down forever.
REACHABLE_TTL_MS = 90_000

PING_INTERVAL = 10  # seconds (matches Feishin's heartbeat)
PING_TIMEOUT = 10
RELEASE_TIMEOUT = 1.5  # seconds to wait for an old device to hand off
# Seconds to wait for a transfer target to confirm it actually started the queue.
# Generous on purpose: a Chromecast bridge may have to connect, LAUNCH the receiver
# app and fetch the first bytes. Shorter than the ~22 s a failed cast load can take,
# which is precisely the window this exists to close.
LOAD_TIMEOUT = 10.0
PROGRESS_THROTTLE = 1.0  # seconds between fanned-out progress broadcasts
INTENT_GRACE = 2.0  # seconds during which receiver reports can't contradict a
                    # fresh user play/pause intent (guards against stale
                    # in-flight 1 Hz reports flipping the state back)
# The command echo (PROTOCOL §5.2-5.3). A receiver advertising the cap "cmdEcho" gets
# a `seq` on every directive that changes what its next report says, and names the
# last one it has carried out in `report.applied`. A report is then judged by that
# number instead of by INTENT_GRACE: the clock guessed how long a receiver needs,
# and was wrong both ways (it believed a late stale sample and distrusted an early
# true one). The clock stays for every device without the cap.
ECHO_CAP = "cmdEcho"
ECHO_CMDS = frozenset({"load", "play", "pause", "seek", "jump", "queueChanged", "clear"})
# A receiver that never confirms a directive must not be distrusted for ever. Once
# the OLDEST unconfirmed one is this old the device goes back to the INTENT_GRACE
# rule until it catches up. A guard against a receiver that breaks its contract,
# not the mechanism: both clients can hold a pause behind a load for ~8.5 s.
ECHO_BACKSTOP = 12.0
# What this hub understands beyond the base protocol, advertised in `welcome` so a
# client can tell a hub that will echo, and will ack, from one that silently
# ignores the cap and the `cid`. The last two matter the same way: an older hub
# reads `act:volume` with a `target` as "the ACTIVE device's volume" and answers
# `act:sleepTimer` with `bad_action`, so a client sends neither without them.
# `station` (§18): the station engine, `session.stationId`, `setQueue.stationId`
# and the `station` frame. Without it a client runs its own radio and autoplay.
HUB_FEATURES = ["cmdEcho", "actAck", "volumeTarget", "sleepTimer", "station"]
# "I apply `do:setVolume` while I am not the playing device." Only a device that
# says so on its current connection is sent one while idle (PROTOCOL §5.1): to
# anything else the directive would be a level recorded here and applied nowhere.
IDLE_VOLUME_CAP = "idleVolume"
# The sleep timer (PROTOCOL §5.1): one for the whole session, held here, so it is
# the same timer on every device and runs out whether or not the device that set
# it is still awake. A day is the most it is set for.
SLEEP_MINUTES_MAX = 1440
# How long one of its minutes lasts. 60 everywhere but in a test, which cannot
# wait a minute per case.
SLEEP_MINUTE_S = float(os.environ.get("HUB_SLEEP_MINUTE_S", "60") or "60")
# Acknowledged acts (PROTOCOL §5.1, §5.4). An act carrying `cid` is answered with
# an `ack` to its sender. Never `id`: eight actions use that for a track, a saved
# queue or a mix.
CID_MAX = 64
# The acts that change the session. While a transfer is being carried out they are
# HELD (PROTOCOL §7): applied in the middle of one they are sent to the device
# being abandoned and then overwritten when the handoff commits, which is an act
# accepted and then undone.
HELD_ACTIONS = frozenset({"setQueue", "enqueue", "clear", "remove", "move", "play",
                          "pause", "playpause", "next", "previous", "jump", "seek",
                          "repeat", "shuffle"})
# Of those, the ones a handoff can simply carry: the load it sends takes a play
# state and a position, so the new device never starts what was already stopped.
FOLDED_ACTIONS = frozenset({"play", "pause", "playpause", "seek"})
MIRROR_DEBOUNCE = 2.5  # seconds to coalesce rapid savePlayQueue mirror writes
SAVED_QUEUES_MAX = 20  # rolling saved-queue history cap (matches both clients)
# "Mixed for You" — persistent regenerating recipes (PROTOCOL §17). A saved queue
# stores a RESULT; a mix stores a RECIPE, and each client regenerates it with the
# engine it has. Since §18 (2026-10-16) the hub has one: a `station` recipe, and on
# a client with stations `similar`/`fingerprint`/`adaptive` too, play through a
# station computed here. The hub still stores the recipe only, never its result.
MIXES_MAX = 30  # cap, evicted by updatedAt exactly like saved queues
# The generator each recipe names. A client that meets an unknown kind hides the
# row rather than guessing, so this list is additive-only.
MIX_KINDS = ("similar", "fingerprint", "adaptive", "genre", "artist", "station")
# AudioMuse's three Mood Flow characters, the legacy names of the station
# characters (§17.1, §18): a mix carries `character` and this name beside it.
MIX_MOODS = ("EchoMatch", "SteadyVibes", "TransitionMaestro")
MIX_COUNT_MAX = 500  # a recipe asks for a queue, not a library dump
SQ_PROGRESS_THROTTLE = 5.0  # seconds between cursor writes to the current saved-queue record
TOMBSTONE_MAX = 200  # remembered saved-queue deletions (so a client re-sync can't resurrect)
SAVED_QUEUE_SONGS_MAX = 1000  # per-record track cap for CLIENT-SUPPLIED history (syncSavedQueues)
# Reload ceiling. Records the hub built itself from a live queue are uncapped (the session
# queue is), so re-reading state.json must not truncate one — this is only a sanity bound.
SAVED_QUEUE_SONGS_HARD_MAX = 20000
SQ_STR_MAX = 512  # longest accepted string field inside a client-supplied saved-queue record
POSITION_SAVE_THROTTLE = 10.0  # seconds between state writes driven by position-only reports
# state.json is rewritten whole, unsynced, on every session broadcast — fine for
# what clients re-push or the hub rebuilds (saved queues, session, devices,
# lbIndex), not for the two collections that exist nowhere else: `mixes` and the
# `deletedSavedQueues` tombstones. A second copy of the file is therefore kept
# beside it and fsynced, rewritten only when one of those two changes. Same
# format as state.json, so `_load` reads either; an older hub ignores it.
STATE_BACKUP_SUFFIX = ".bak"
STATE_BACKUP_FLUSH_TIMEOUT = 3.0  # seconds a graceful stop waits for a backup in flight

# --- proxy tuning (shared by the AudioMuse and lb-bot proxies) ---
PROXY_MAX_INFLIGHT = 4      # concurrent upstream calls PER PROXY (each of the three
                            # proxies has its own pool); a hung core must not eat the
                            # default thread pool and stall the 1 Hz progress fan-out
# A second, small pool for the routes a client POLLS (`/lb/album/status`,
# `/lb/fills`, `/lb/gap`) and for cancels. The default pool is first-come with
# no priority, so four `/lb/album/sources` fan-outs — up to 150 s each — held
# every slot and every status poll behind them queued until the handshake
# deadline dropped the socket with no answer at all. A poll must never wait on
# a search.
PROXY_FAST_INFLIGHT = 2
# A third pool, ONE slot, for the index mirror's sync pulls (`/lb/index/*`).
# Not the default pool, where a pull would queue behind 150 s source searches;
# not `fast`, where a client pulling its mirror from zero — dozens of 2 MB pages
# back to back — would starve the status polls that pool exists to protect.
# One slot because the pulls are sequential per client anyway and lb-bot reads
# every page under one lock; a second device pulling at the same moment waits
# (or is told `busy` and backs off with its cursor untouched), which is cheaper
# than two page reads contending for that lock.
PROXY_SYNC_INFLIGHT = 1
# How long a request may wait for a slot before the hub answers 503 `busy`
# itself. Waiting past the handshake deadline is the one outcome a client can
# do nothing with; a 503 it can show. Applies to the `fast` and `sync` pools.
PROXY_QUEUE_TIMEOUT_FAST = float(os.environ.get("PROXY_QUEUE_TIMEOUT_FAST", "10"))
PROXY_TIMEOUT = 20          # seconds per upstream socket op (urllib has one knob for
                            # connect+read); Tier 2 is in-memory lookups, so fail fast
PROXY_SLOW_TIMEOUT = 45     # for routes that are known to sit on a rate-limited third
                            # party (lb-bot's MusicBrainz-backed album lookups)
PROXY_SEARCH_TIMEOUT = 150  # for /lb/album/sources alone: it BLOCKS on a live
                            # Soulseek search while slskd fans out to peers and
                            # waits on them. Measured at 45.4s for one release
                            # group and documented in both clients as 30-90s, so
                            # PROXY_SLOW_TIMEOUT was not merely tight, it was
                            # below the median -- and a timeout here reads to the
                            # user as "lb-bot is busy", sending them to look at a
                            # service that is healthy and still searching.
                            # NOT /lb/gap/search: that starts a background task
                            # and returns at once, and its own long timeout is
                            # about contending for lb-bot's _review_lock.
PROXY_CACHE_TTL = 60.0      # shared result cache — both clients asking the same
                            # question cost one upstream call
PROXY_CACHE_TTL_LONG = 6 * 3600.0  # for effectively immutable upstream answers (a
                            # release's tracklist and editions do not change)
# Entries, across every route of one proxy — not bytes. 64 was sized for
# surfaces that ask one question per page open; a Discover screen asks several,
# and rotates the artist it seeds its similar-artists row from, so each rotation
# is a fresh key. Eviction drops the soonest-to-expire first, which means the
# 60s entries absorb the churn and the 6h ones (album/similar, album/releases,
# meta/*) survive it — but only while there is room for them at all.
PROXY_CACHE_MAX = 128
# Query params that mean "do not answer this from a cache". The client is
# explicitly asking for a re-fetch, and a cache hit is the precise thing it is
# trying to get past. lb-bot's own `?refresh=1` escape hatch was unreachable
# through the proxy twice over: `_filtered_params` drops any param a route does
# not list, and merely whitelisting it would not have been enough either — see
# the cache-key note in `HttpProxy.call`.
PROXY_NO_CACHE_PARAMS = ("refresh",)

PROXY_MAX_BODY = 256 * 1024        # largest client request body accepted
PROXY_MAX_RESPONSE = 4 * 1024 * 1024

# The hub's own view of whether lb-bot is up, carried in `welcome` so a client
# can gate its lb-bot surfaces without first awaiting a `/lb/status` round trip.
LB_PROBE_INTERVAL = 60.0
# The hub's own probe asks `/api/health`, which answers in milliseconds, so it
# gets a short deadline of its own rather than PROXY_TIMEOUT: an lb-bot that
# HANGS must read as down within seconds, not after 20. Only the health call is
# bounded this tightly — the `/api/summary` fallback for an older lb-bot is a
# genuinely heavy read and keeps the route's own timeout.
LB_PROBE_TIMEOUT = 4.0
# Until the first probe has answered, a `welcome` waits up to this long for it.
# Without it, a client reconnecting the instant a restarted hub is listening
# reads `available: false` from a probe that simply has not answered yet — and
# hides every lb-bot feature on a healthy lb-bot. The WELCOME waits, not the
# listener: the proxies and the health port answer from the first moment, and
# once the first verdict is in the wait costs nothing. Bounded, so an lb-bot
# that hangs delays a first handshake by seconds rather than holding it.
LB_PROBE_STARTUP_WAIT = 3.0
# `lbIndex` is only a hint — clients pull on every `welcome` regardless — and a
# bulk build pushes every 2 s, so persisting each one would rewrite state.json
# (every saved queue in it) at that rate. Leading edge, like the position saves;
# a new epoch is always written at once, and a graceful stop flushes the rest.
LB_INDEX_SAVE_THROTTLE = 10.0

# --- stations (PROTOCOL §18): radio and autoplay, computed here ---
# The play history the stations keep (the last STATION_HISTORY_MAX plays from the
# last STATION_HISTORY_DAYS days) lives in its own file beside state.json. "0"
# keeps it in memory only: the stations still avoid what was played, until a
# restart forgets it.
STATION_HISTORY_ON = os.environ.get("HUB_STATION_HISTORY", "1").strip().lower() not in (
    "0", "false", "no", "off")
# The characters (§18.1). Their order is the old MoodCharacter order (EchoMatch,
# SteadyVibes, TransitionMaestro), so a preference stored as an ordinal keeps its
# meaning, and their words are the catalogue both clients render.
STATION_CHARACTERS = ("close", "balanced", "explore")
STATION_DEFAULT_CHARACTER = "balanced"
STATION_LEGACY = {"close": "EchoMatch", "balanced": "SteadyVibes",
                  "explore": "TransitionMaestro"}
STATION_FROM_LEGACY = {v: k for k, v in STATION_LEGACY.items()}
STATION_CATALOGUE = (
    {"id": "close", "label": "Close", "legacy": "EchoMatch",
     "description": "Stays right next to what you started with."},
    {"id": "balanced", "label": "Balanced", "legacy": "SteadyVibes",
     "description": "Near what you started with, with room to move."},
    {"id": "explore", "label": "Explore", "legacy": "TransitionMaestro",
     "description": "Wanders further, steered by what you skip and like."},
)
STATION_MAX = 8                       # stations held at once, least recently used evicted
STATION_IDLE_MS = 24 * 3600 * 1000    # a station unused this long is dropped
STATION_SERVED_MAX = 2000             # served ids a station remembers (oldest dropped)
STATION_TRAIL_MAX = 20                # artist keys kept for the spacing rule
# One request's budget, end to end. Under Feishin's 15 s IPC timeout and Navic's
# 20 s; what is found by then is answered with `partial: true`.
STATION_BUDGET_S = 12.0
STATION_CALL_TIMEOUT = 8.0            # one AudioMuse or Navidrome call
STATION_COMPUTING_MAX = 2             # stations computing at once
STATION_CALLS_PER_REQUEST = 2         # upstream calls in flight for one request
STATION_PROBE_INTERVAL = 600.0        # the tiers are re-probed this often...
STATION_PROBE_RETRY = 15.0            # ...and this long after a call failed
STATION_PROBE_STARTUP_WAIT = 3.0      # a probe route waits this long for the first verdict
# AudioMuse answers a seed's nearest list the same way every time (it caches per
# seed), so a list is fetched once and walked by cursor: re-asking never yields new
# rows. Cached in SONIC's cache for this long.
STATION_LIST_TTL = 1800.0
# The depths a nearest list is fetched at. A ceiling is rounded up to one of these,
# so a station widening from 150 to 225 to 338 asks AudioMuse twice, not three times.
# 2000 rows measured 810 KB on the live AudioMuse (2026-10-16), under PROXY_MAX_RESPONSE.
STATION_LIST_DEPTHS = (150, 200, 400, 1000, 2000)
STATION_WIDEN = 1.5                   # the ladder's rank-ceiling step
STATION_DRIFT_CEILING = 200           # Explore: rank in a played-through track's own list
STATION_DRIFT_SOURCES = 5             # the last N played-through tracks drift from
STATION_TIER1_MAX = 500               # deepest Navidrome similar list asked for
STATION_ALCHEMY_N = 100
STATION_SIMILAR_ARTISTS_N = 8
STATION_SEED_ARTISTS = 2              # seed artists Explore asks similar artists for
STATION_COOL_MS = 2 * 3600 * 1000     # an artist skipped enough is cooled this long
STATION_SKIP_TTL_MS = 2 * 3600 * 1000 # a skip stays in the alchemy subtract set this long
STATION_FINGERPRINT_N = 100
STATION_MIX_ANCHORS = 5               # Your mix: at most this many of the top rows
STATION_SEEDS_MAX = 5
STATION_EXCLUDE_MAX = 1000
STATION_HISTORY_MAX = 2000
STATION_HISTORY_DAYS = 14
STATION_HISTORY_RELAXED_DAYS = 3      # the ladder's history rung
STATION_HISTORY_FLUSH_S = 30.0        # the history file is written at most this often
STATION_HISTORY_SUFFIX = ".history"
STATION_HISTORY_REPEAT_MS = 30 * 60 * 1000  # one id again within this is the same play
# Judging the track just left (§18.3).
STATION_PLAYED_FRACTION = 0.85        # played through at 85 % ...
STATION_PLAYED_TAIL_MS = 15_000       # ... or with under 15 s to go
STATION_SKIP_MS = 30_000              # a skip: a step forward before min(30 s, 40 %)
STATION_SKIP_FRACTION = 0.40
STATION_PLAY_FRACTION = 0.50          # a play, for the history: 50 % ...
STATION_PLAY_MS = 240_000             # ... or 4 min
# The six features AudioMuse carries in `other_features`. Compressed (every value
# sits near 0.6 on the live library), so they ORDER a batch for smooth transitions
# and never gate one: a feature gate let Lana Del Rey -> OsamaSon through.
STATION_FEATURES = ("danceable", "aggressive", "happy", "party", "relaxed", "sad")


def log(*a: Any) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def dlog(*a: Any) -> None:
    """Verbose diagnostic log, gated behind HUB_DEBUG (chatty at 1 Hz)."""
    if DEBUG:
        log(*a)


# --------------------------------------------------------------------------- #
# Coercion helpers for client-supplied data
#
# Everything a client sends is untrusted JSON, but saved-queue records are the one
# payload the hub *stores and re-broadcasts verbatim*: a bad value doesn't just fail
# one act, it lands in state.json and then breaks every later _save(). Hence coerce
# on the way in rather than validating at the point of use.
# --------------------------------------------------------------------------- #
def _as_int(v: Any, default: int = 0, lo: Optional[int] = None,
            hi: Optional[int] = None) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _as_str(v: Any, max_len: int = SQ_STR_MAX) -> Optional[str]:
    """A string or None — never a number/dict/list that would surprise a client renderer."""
    if not isinstance(v, str):
        return None
    return v[:max_len]


# What a device says it is (`hello.device.kind`, PROTOCOL §3.1): the icon a picker
# draws. The hub never interprets it, so the vocabulary can grow without a hub
# release; only the shape is checked, because the value is stored and re-sent to
# every client on every `devices` frame.
_KIND_RE = re.compile(r"[a-z]{1,16}")


def _as_kind(v: Any) -> Optional[str]:
    """A device kind (1 to 16 lowercase letters), or None for anything else."""
    return v if isinstance(v, str) and _KIND_RE.fullmatch(v) else None


def _as_level(v: Any) -> Optional[int]:
    """A volume level a device reported, clamped to 0..100, or None if it is not
    a number. None and never a default: a report that cannot be read must leave
    the row as it was, not set it to something."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return max(0, min(100, round(v)))
    except (ValueError, OverflowError):  # NaN and Infinity, which json.loads accepts
        return None


# --------------------------------------------------------------------------- #
# State file: reading it defensively, and the fsynced backup beside it
# --------------------------------------------------------------------------- #
def _read_state_file(path: str) -> tuple[str, Optional[dict]]:
    """Read one state file. Returns `(status, data)`, status being one of
    `ok`, `missing`, `empty` (zero bytes or whitespace) or `corrupt`.

    `empty` and `corrupt` are kept apart on purpose. An empty file carries nothing
    worth keeping — every subprocess suite starts from a zero-byte temp file —
    while a corrupt one may be the only copy of something, so the caller sets it
    aside instead of letting the next save overwrite it.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "missing", None
    except OSError as e:
        log(f"cannot read {path}:", e)
        return "corrupt", None
    if not raw.strip():
        return "empty", None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — bad UTF-8, bad JSON, absurd nesting
        log(f"cannot parse {path}:", e)
        return "corrupt", None
    if not isinstance(data, dict):
        log(f"cannot use {path}: top level is {type(data).__name__}, not an object")
        return "corrupt", None
    return "ok", data


def _set_aside_state_file(path: str) -> Optional[str]:
    """Rename an unreadable state file to `<path>.corrupt-<UTC timestamp>` so its
    bytes survive the next save. Returns the new name, or None if the rename failed."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = f"{path}.corrupt-{stamp}"
    n = 1
    while os.path.exists(target):  # two set-asides inside one second
        n += 1
        target = f"{path}.corrupt-{stamp}-{n}"
    try:
        os.replace(path, target)
    except OSError as e:
        log(f"could not set {path} aside:", e)
        return None
    return target


def _durable_signature(mixes: Any, tombstones: Any) -> Optional[str]:
    """A comparable fingerprint of the part of the state nothing else can restore.

    Takes the two collections rather than a Hub so the same function fingerprints
    the live state and a parsed backup file. None means "cannot tell", which never
    equals a real signature — so an unreadable backup is simply rewritten.
    """
    try:
        return json.dumps([mixes or [], sorted((tombstones or {}).items())],
                          sort_keys=True)
    except Exception:  # noqa: BLE001 — a hand-edited backup with the wrong shapes
        return None


def _write_state_backup_blocking(path: str, payload: str) -> None:
    """Write the backup durably: own temp name, fsync, rename, fsync the directory.

    Blocking — called through `asyncio.to_thread`, or directly at boot before
    anything is being served. Its temp file is not state.json's, so this never
    races `_save()`'s own unsynced write.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # The rename is only durable once the directory is. Best-effort: the production
    # volume is a FUSE share, where fsync on a directory can simply be refused.
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# State model
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    rev: int = 0
    active_device_id: Optional[str] = None
    queue: list[dict] = field(default_factory=list)  # list of Track dicts (id, title, ...)
    index: int = 0                                    # current item, pre-shuffle order
    order: Optional[list[int]] = None                 # shuffled play order; None = sequential
    position_ms: int = 0
    is_playing: bool = False
    repeat: str = "none"                              # none | all | one
    shuffle: bool = False
    updated_at: int = 0
    # Identity of the saved-queue history record this live session corresponds to.
    # None = a transient/legacy queue not tracked in history. Set by setQueue.
    saved_queue_id: Optional[str] = None
    source_kind: str = "manual"                       # album|playlist|radio|moodFlow|journey|manual
    source_name: Optional[str] = None
    # The station this queue continues as (PROTOCOL §18.2): set by setQueue's
    # `stationId` or a station `start` with `bindSession`, cleared by a setQueue
    # without one and by `clear`. What lets the hub hear the station's skips.
    station_id: Optional[str] = None
    # Bumped on EVERY assignment to active_device_id, so "is the slot still where I
    # left it" is answerable. The device id alone can't answer it: A -> B -> A leaves
    # the same id with a different meaning, and a timed-out transfer to A that only
    # compared ids would roll back the *later* transfer that legitimately won A back.
    # Runtime only — a restart clears the active slot anyway (see _load).
    active_gen: int = 0

    def set_active(self, device_id: Optional[str]) -> int:
        """Assign the active slot and return the generation that assignment created."""
        self.active_device_id = device_id
        self.active_gen += 1
        return self.active_gen

    def snapshot(self) -> dict:
        return {
            "rev": self.rev,
            "activeDeviceId": self.active_device_id,
            "queue": self.queue,
            "index": self.index,
            "order": self.order,
            "positionMs": self.position_ms,
            "isPlaying": self.is_playing,
            "repeat": self.repeat,
            "shuffle": self.shuffle,
            "updatedAt": self.updated_at,
            "savedQueueId": self.saved_queue_id,
            "sourceKind": self.source_kind,
            "sourceName": self.source_name,
            "stationId": self.station_id,
        }

    def bump(self) -> None:
        self.rev += 1
        self.updated_at = int(time.time() * 1000)


@dataclass
class Device:
    id: str
    name: str = "Unknown"
    platform: str = "unknown"
    caps: list[str] = field(default_factory=lambda: ["controller"])
    # What the device says it is (see _as_kind); None = it never said. Persisted,
    # so a row keeps its icon while the device is offline and across a restart.
    kind: Optional[str] = None
    # PRESENCE: a WebSocket claiming this id is attached right now. For a bridged
    # Chromecast that is the BRIDGE's socket — it says nothing about the speaker.
    online: bool = False
    # What the device last said its level is (`deviceState.volume`), else what it
    # was last told (`act:volume`). Always an int: a client reads a null as 100.
    volume: int = 100
    last_seen: int = 0
    ws: Any = None                       # live websocket, not persisted
    release_future: Any = None           # set during a transfer handoff
    load_future: Any = None              # set while awaiting a transfer's `loaded` ack
    # Which transfer each of those futures belongs to (PROTOCOL §7.2). Without this,
    # correlation is by device alone: a `loaded` delayed past its own transfer's
    # timeout satisfies the NEXT transfer to the same device, and the hub calls a
    # load good on the strength of a reply to a question it stopped asking.
    release_tid: Optional[str] = None
    load_tid: Optional[str] = None
    # REACHABILITY: the bridge's own verdict on the physical device behind it.
    # None = unknown / not applicable (every ordinary client, which IS its own
    # hardware). False = the bridge is connected but the speaker did not answer.
    reachable: Optional[bool] = None
    reachable_at: int = 0                # when `reachable` was last asserted
    app_running: Optional[bool] = None   # receiver app up (cast bridges only)
    bridged_by: Optional[str] = None     # device id of the client holding this bridge
    # The command echo (see ECHO_CAP). All of it is per connection: cleared where
    # `welcome` is written, which is also where the receiver zeroes `applied`.
    # `echo_need`: seq of the last stamped directive sent since `welcome`; a report
    # with a smaller `applied` was read before that directive took effect.
    echo_need: int = 0
    # (seq, monotonic time) of each stamped directive not yet confirmed, oldest
    # first. Only the head matters: it is what the backstop clock runs from.
    echo_sent: Any = field(default_factory=deque)
    echo_backstop: bool = False          # ECHO_BACKSTOP expired; old rule until caught up
    echo_timer: Any = None               # the pending backstop callback, if any
    # `rev` of the newest session snapshot written to this socket (`welcome` or
    # `session`). It is what an `ack` reports, so that "the ack names a session
    # you already hold" is true by construction and not by timing.
    session_rev_sent: int = 0
    # The load future a `loaded{ok:false}` resolved. Set from that frame until the
    # transfer's waiter has rolled the slot back: reports in between describe a
    # player that failed to start and must not be mirrored into the session.
    load_refused: Any = None

    def effective_reachable(self, now_ms: Optional[int] = None) -> Optional[bool]:
        """`reachable`, demoted to unknown once the asserting bridge goes quiet.

        A verdict is only as good as the bridge that keeps renewing it: a bridge
        that stops sending `deviceState` (crashed, wedged, mid-teardown) must not
        leave a speaker permanently marked up — or permanently marked down.
        """
        if self.reachable is None:
            return None
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if now - self.reachable_at > REACHABLE_TTL_MS:
            return None
        return self.reachable

    def transferable(self, now_ms: Optional[int] = None) -> bool:
        """Can this device be handed the session right now?

        Unknown reachability is permissive — that is every ordinary client, and a
        bridge that hasn't reported yet deserves the benefit of the doubt.
        """
        return self.online and self.effective_reachable(now_ms) is not False

    def info(self, active_id: Optional[str], now_ms: Optional[int] = None) -> dict:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "kind": self.kind,
            "caps": self.caps,
            "online": self.online,
            "isActive": self.id == active_id,
            "lastSeen": self.last_seen,
            "volume": self.volume,
            # PROTOCOL §12.2: presence and reachability are different questions.
            "reachable": self.effective_reachable(now),
            "appRunning": self.app_running,
            "bridgedBy": self.bridged_by,
        }


@dataclass
class SleepTimer:
    minutes: int                # as asked for; relayed so a client can show "30 min"
    set_by: Optional[str]       # device id of whoever set it
    # The end twice over. `ends_at` is wall-clock ms and is what the state file
    # holds, because only a wall clock means anything to the next process.
    # `deadline` is monotonic and is what the countdown runs on, so a clock step
    # on the host neither fires the timer early nor stretches it.
    ends_at: int
    deadline: float

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))


# --------------------------------------------------------------------------- #
# Navidrome savePlayQueue mirror (best-effort, optional)
# --------------------------------------------------------------------------- #
def _nd_auth_params() -> dict:
    salt = hashlib.md5(os.urandom(16)).hexdigest()[:12]
    token = hashlib.md5((ND_PASS + salt).encode()).hexdigest()
    return {"u": ND_USER, "t": token, "s": salt, "v": "1.16.1", "c": "navi-connect", "f": "json"}


def _nd_save_play_queue_blocking(ids: list[str], current: Optional[str], position_ms: int) -> None:
    if not (MIRROR_PLAYQUEUE and NAVIDROME_URL and ND_USER and ND_PASS and ids):
        return
    params = _nd_auth_params()
    # Subsonic savePlayQueue takes repeated id params + current + position
    query = [(k, v) for k, v in params.items()]
    query += [("id", i) for i in ids]
    if current:
        query.append(("current", current))
    query.append(("position", str(int(position_ms))))
    url = f"{NAVIDROME_URL}/rest/savePlayQueue.view?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            r.read()
    except Exception as e:  # noqa: BLE001 — mirror is best-effort
        log("savePlayQueue mirror failed:", e)


# --------------------------------------------------------------------------- #
# Upstream proxies  (designs: ../DESIGN-hub-audiomuse-proxy.md,
#                             ../DESIGN-lbbot-client-integration.md)
#
# Plain HTTP served on the WebSocket port, so the clients reach these services
# through the one component that is already authenticated and remotely reachable.
# They stop holding upstream base URLs, upstream tokens and (on the fingerprint
# call) the Navidrome password.
#
# These are WHITELISTS, not forwarders: HUB_TOKEN is on every device, so a
# pass-through proxy would hand it AudioMuse's analysis/clustering/embedding
# admin endpoints — or, worse, every write endpoint of lb-bot's unauthenticated
# API. Unknown path or method → 404 with no upstream call; unknown parameters are
# dropped rather than relayed.
# --------------------------------------------------------------------------- #
#   (method, hub path) -> upstream method/path + what may be forwarded
#     params:    allowed query parameters (GET)
#     body:      allowed top-level JSON body keys (POST)
#     path_args: keys lifted out of params/body and interpolated into `path`
#                (for upstream routes that put an id in the URL)
#     nd:        inject HUB_ND_USER/HUB_ND_PASS (only the fingerprint route needs them)
#     cache:     cacheable; `ttl` overrides PROXY_CACHE_TTL
#     timeout:   overrides PROXY_TIMEOUT for a known-slow upstream
#     pool:      "default" (absent), "fast" or "sync" — see PROXY_*_INFLIGHT
#     pool_when: {param name: pool}; a request carrying that query param takes
#                that pool instead of `pool` (a route cheap for one query only)
#     invalidates:  routes whose cached answers a 2xx answer of this route drops
#     invalidates_on_answer: the same, but on ANY answer below 500 (a 404 counts;
#                a 5xx and the hub's own errors do not) — for polled job routes
#     coalesce:  False opts a GET out of sharing an identical in-flight call;
#                absent means it shares one (see HttpProxy.call)
#     fallback:  an upstream path retried once when `path` answers 404 — an
#                upstream older than the route it is asked for
SONIC_ROUTES: dict[tuple[str, str], dict] = {
    ("GET", "/sonic/fingerprint"): {
        "method": "GET", "path": "/api/sonic_fingerprint/generate",
        "params": ("n",), "nd": True, "cache": True,
    },
    ("POST", "/sonic/alchemy"): {
        "method": "POST", "path": "/api/alchemy",
        "body": ("items", "n", "temperature", "subtract_distance"),
        # NOT cached: alchemy is stochastic (temperature) and Mood Flow re-asks with
        # the same add/subtract sets as a session goes on — serving a cached mix would
        # top the queue up with the exact tracks it just added.
        "cache": False,
    },
    ("POST", "/sonic/clap/search"): {
        "method": "POST", "path": "/api/clap/search",
        "body": ("query", "limit"), "cache": True,
    },
    ("GET", "/sonic/clap/stats"): {
        "method": "GET", "path": "/api/clap/stats",
        "params": (), "cache": True, "probe": True,
    },
    ("GET", "/sonic/score"): {
        # B-137: current AudioMuse serves the score at /external/get_score and
        # answers /get_score 404 (measured 2026-10-16); the old path stays as the
        # fallback for an AudioMuse older than the move.
        "method": "GET", "path": "/external/get_score", "fallback": "/get_score",
        "params": ("id",), "cache": True,
    },
}

# The station engine's own way to AudioMuse (PROTOCOL §18). NOT routes: no client
# can name one, because `HttpProxy.handle` looks a path up in SONIC_ROUTES and
# nowhere else, and their route keys are not paths. They are called through
# `SONIC.call`, so a station shares §14's four-slot pool, its cache and its
# coalescing with every client request instead of opening a second way in.
#
# `similar_tracks` with `radius_similarity=false&eliminate_duplicates=false` is a
# nearest-first list with distances, and the same list every time for a seed
# (AudioMuse caches per seed), so it is cached long and walked by cursor. Alchemy
# is stochastic and never cached. `score` is where a seed's own title, artist and
# features come from; `/get_score` is its path on an AudioMuse older than B-137.
SONIC_INTERNAL: dict[str, tuple[tuple[str, str], dict]] = {
    "similar_tracks": (("GET", "station:similar_tracks"), {
        "method": "GET", "path": "/api/similar_tracks",
        "params": ("item_id", "n", "eliminate_duplicates", "radius_similarity",
                   "mood_similarity"),
        "cache": True, "ttl": STATION_LIST_TTL, "timeout": STATION_CALL_TIMEOUT,
    }),
    "alchemy": (("POST", "station:alchemy"), {
        "method": "POST", "path": "/api/alchemy",
        "body": ("items", "n", "temperature", "subtract_distance"),
        "cache": False, "timeout": STATION_CALL_TIMEOUT,
    }),
    "fingerprint": (("GET", "station:fingerprint"), {
        "method": "GET", "path": "/api/sonic_fingerprint/generate",
        "params": ("n",), "nd": True, "cache": True, "timeout": STATION_CALL_TIMEOUT,
    }),
    "similar_artists": (("GET", "station:similar_artists"), {
        "method": "GET", "path": "/api/similar_artists",
        "params": ("artist", "artist_id", "n", "include_component_matches"),
        "cache": True, "ttl": STATION_LIST_TTL, "timeout": STATION_CALL_TIMEOUT,
    }),
    "score": (("GET", "station:score"), {
        "method": "GET", "path": "/external/get_score", "fallback": "/get_score",
        "params": ("id",), "cache": True, "ttl": PROXY_CACHE_TTL_LONG,
        "timeout": STATION_CALL_TIMEOUT,
    }),
}

# lb-bot's library-gap intelligence. Read-mostly; the writes are all things the
# clients offer explicitly — index an artist, download an album, allow mp3 for one
# album, and (below) act on ONE partly-owned album's gap. Everything else lb-bot
# exposes — the placement/match workspace, delete-file, trash, prefs, the beets
# import paths — stays off the wire, which is the entire point of a whitelist in
# front of an API with no auth.
#
# Two notes that decide the TTLs. `artist/discography` GET is an instant SQLite
# read, so a short TTL is right and a stale answer is cheap. `album/releases`,
# `album/tracklist` and `album/similar` sit on rate-limited MusicBrainz /
# ListenBrainz calls and return effectively immutable data — a long TTL and a
# longer timeout. `album/status` is the download-progress poll: never cached.
#
# The `gap/*` group fills the holes in an album the library already partly has.
# Every one of them is scoped to a single review group whose id the client got
# from an `incomplete` discography row — lb-bot's discography scan builds the
# review group as it goes, so that id is a live handle. Two things stay out on
# purpose: `/api/gaps` with no id (the whole-library list, unbounded, and no
# client needs it — the id always arrives via the discography row), and
# `/api/gaps/<id>/duplicate-files`.
# Q-017c/Q-018: the gap view's projection. The GET poll carries it at the top level;
# every POST that answers with lb-bot's gap view nests it under `gap`, so those
# routes share the same rules behind a `gap.` prefix.
GAP_STRIP_RULES: tuple[tuple[str, str], ...] = (
    ("sources", "files"), ("sources", "filesTruncated"),
    ("tracks", "placeFolder"), ("tracks", "placeFile"), ("tracks", "manualPick"),
)
GAP_REPLY_STRIP_RULES: tuple[tuple[str, str], ...] = tuple(
    (f"gap.{k}", f) for k, f in GAP_STRIP_RULES)

LB_ROUTES: dict[tuple[str, str], dict] = {
    ("GET", "/lb/status"): {
        # The liveness probe. `/api/health` is lb-bot's cheap dedicated route;
        # `/api/summary`, which this used to ride on, builds the whole Fill-gaps
        # workspace view to answer "are you there". The fallback is for an lb-bot
        # deployed before `/api/health` existed: it answers that path 404, and
        # without the retry a hub deployed first would call a healthy lb-bot down.
        "method": "GET", "path": "/api/health", "fallback": "/api/summary",
        "params": (), "cache": True, "probe": True,
    },
    ("GET", "/lb/index/changes"): {
        # The index mirror's pull: every artist and tombstone with a seq above
        # `since`, a page at a time (contract: PLAN-lbbot-index-mirror §1).
        #
        # Never cached and never coalesced, for one reason: a client pulls
        # because it was just told the head moved. A cached page, or a read it
        # joined that set off before the write, answers with the OLD head, and
        # the client then sits one push behind until the next trigger — the
        # precise staleness the mirror exists to remove. Each pull is ~100 B
        # when there is nothing new, so sharing buys nothing worth that.
        "method": "GET", "path": "/api/index/changes",
        "params": ("since", "epoch"), "cache": False, "coalesce": False,
    },
    ("GET", "/lb/index/keys"): {
        # Every `{key, seq}` lb-bot holds — the drift check's ground truth, read
        # when a pull's count or seq sum disagrees with the client's mirror.
        # Uncached and uncoalesced for the same reason as the pull.
        "method": "GET", "path": "/api/index/keys",
        "params": (), "cache": False, "coalesce": False,
    },
    ("GET", "/lb/artist/discography"): {
        "method": "GET", "path": "/api/artist/discography",
        "params": ("nd_id", "mbid"), "cache": True,
    },
    ("POST", "/lb/artist/discography"): {
        "method": "POST", "path": "/api/artist/discography",
        "body": ("mbid", "name", "nd_id", "external"), "cache": False,
    },
    ("POST", "/lb/artist/release"): {
        # Add or refresh ONE release-group in an artist's stored index.
        #
        # Without it the smallest unit of refresh is a whole artist, at one
        # MusicBrainz request per second per release-group — which is what a
        # release newer than the stored index otherwise costs to make visible.
        # A stored discography is served immediately even when stale, by design,
        # so a brand-new album is simply absent from it, and every Fresh row
        # leads to an artist page that does not list the album it came from.
        #
        # Not cached: it is a write, and its answer is the row it just stored.
        #
        # `title`/`artist`/`type`/`year` are the caller's own metadata, used
        # upstream ONLY when MusicBrainz doesn't answer. lb-bot caches a
        # transient failure for five minutes per exact query string and returns
        # {} inside that window without asking again, so one 503 was a hard 502
        # on that album for everyone who asked next — with nothing the user
        # could do. Every caller reaches here from a row that already names the
        # release, which is the same escape hatch album/sources and
        # album/download carry. Dropping them here would silently disarm it.
        "method": "POST", "path": "/api/artist/release",
        "body": ("rgid", "mbid", "nd_id", "name", "external",
                 "title", "artist", "type", "year"), "cache": False,
    },
    ("GET", "/lb/overrides"): {
        # I-019: the user's artist-MBID overrides, one row per Navidrome artist id,
        # plus `toCheck`, the "matches to check" list. That list is not a plain
        # SQLite read: the handler also reads Navidrome's artist list and groups
        # the index, and it depends on scan results. Cached at the default TTL and
        # flushed by the POSTs that change it (the user's own set/remove never reads
        # back a stale map), by /lb/notify (LB_LIBRARY_ROUTES), by the two polled
        # job routes (album/override, artist/merge) and by an index push
        # (LB_INDEX_INVALIDATES), so a scan
        # that just landed does not leave an artist listed as unmatched. A second
        # client sees another's change within the TTL or on its next refresh.
        "method": "GET", "path": "/api/overrides",
        "params": (), "cache": True,
    },
    ("POST", "/lb/artist/override"): {
        # Set (or replace) the override. PROXY_SLOW_TIMEOUT because lb-bot
        # validates the MBID against MusicBrainz behind its one-request-a-second
        # lock, so a set can queue behind a running discography scan; the default
        # 20 s would turn a healthy wait into a hub 504 `busy` (a hub timeout is a
        # 504, distinct from lb-bot's own 502 with `retryable`, passed through).
        # Never cached. Error statuses (400/404/409/502) pass through untouched.
        "method": "POST", "path": "/api/artist/override",
        "body": ("nd_id", "mbid", "name"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/artist/override/remove"): {
        # Same slow-timeout reasoning: lb-bot runs a strict MusicBrainz name search
        # before deleting (the rescan it kicks off is a background task, not part of
        # the reply), and it can be refused 409 while a fill is in flight.
        "method": "POST", "path": "/api/artist/override/remove",
        "body": ("nd_id",), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    # ---- I-022 / I-023: album release override, cover, artist name merge ----
    ("GET", "/lb/album/override"): {
        # The album as tagged now + its override row. A client POLLS this after an
        # apply or a remove (by `id`, since the album id it started from is dead;
        # a 404 `no_override` is how a remove is seen to finish), so it is never
        # cached. The `?id=` poll rides the fast pool (`pool_when`, below) so a
        # status poll does not queue behind a slow MusicBrainz route; `?album_id=`
        # does not. Local reads on lb-bot: default timeout.
        "method": "GET", "path": "/api/album/override",
        "params": ("album_id", "id"), "cache": False,
        # Only the `?id=` poll is a local read. `?album_id=` is an album read from
        # Navidrome that can cost a MusicBrainz request, so it takes the default
        # pool like every other such read. Every answer it passes through (200 and
        # 404 alike: a 404 `no_override` is a finished remove) flushes
        # /lb/overrides, which no notice announces after a failed or removed job.
        "pool_when": {"id": "fast"}, "invalidates_on_answer": {("GET", "/lb/overrides")},
    },
    ("GET", "/lb/album/covers"): {
        # Cover Art Archive candidates for a release / release group. Cached long
        # like `album/releases`: the Archive changes rarely and a picker reopens it.
        # PROXY_SLOW_TIMEOUT because a cold call is a MusicBrainz lookup (1 req/s
        # lock) plus an Archive fetch; the 502 `retryable` it can answer is not cached.
        "method": "GET", "path": "/api/album/covers",
        "params": ("rgid", "release_mbid"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/override/preview"): {
        # Writes nothing, but matches every file against the release's tracklist,
        # which is a MusicBrainz fetch behind its lock: PROXY_SLOW_TIMEOUT. Never
        # cached (the answer follows the files on disk).
        "method": "POST", "path": "/api/album/override/preview",
        "body": ("album_id", "release_mbid"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/override"): {
        # Apply. Answers at once (the retag runs in the background) but validates
        # the release against MusicBrainz first, hence PROXY_SLOW_TIMEOUT. `force`
        # is a JSON boolean; the body filter passes values through untouched.
        # 409 `poor_match` carries `matched`/`total`, passed through with the status.
        "method": "POST", "path": "/api/album/override",
        "body": ("album_id", "release_mbid", "cover_url", "force"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/cover"): {
        # Cover alone; synchronous on lb-bot, which fetches the image from the
        # Archive before it answers, so PROXY_SLOW_TIMEOUT.
        "method": "POST", "path": "/api/album/cover",
        "body": ("album_id", "cover_url"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/override/remove"): {
        # Undo. The restore runs in the background, but lb-bot first stats every
        # file on record (its reach check) on disks that may be spun down, so the
        # default 20 s could 504 an undo that is already running and that no
        # client then follows: PROXY_SLOW_TIMEOUT.
        "method": "POST", "path": "/api/album/override/remove",
        "body": ("id",), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/artist/merge/preview"): {
        # Counts the variant spelling's albums and files; resolves both artists'
        # effective MBID, which can be a MusicBrainz lookup: PROXY_SLOW_TIMEOUT.
        "method": "GET", "path": "/api/artist/merge/preview",
        "params": ("nd_id", "from_nd_id"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/artist/merge"): {
        # The retag runs in the background, but lb-bot PLANS the whole merge
        # before it answers (one getAlbum per album, a tag read per file), so the
        # default timeout could 504 a merge that is already running and that no
        # client then follows: PROXY_SLOW_TIMEOUT.
        "method": "POST", "path": "/api/artist/merge",
        "body": ("nd_id", "from_nd_id"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/artist/merge"): {
        # Merge row, POLLED until `merged`/`failed` (and until 404 `no_merge`
        # after an undo): never cached, fast pool (assigned below).
        "method": "GET", "path": "/api/artist/merge",
        "params": ("id",), "cache": False,
        # Same flush as album/override: 200 and 404 (`no_merge` = undo finished).
        "invalidates_on_answer": {("GET", "/lb/overrides")},
    },
    ("POST", "/lb/artist/merge/undo"): {
        # Same as album/override/remove: lb-bot stats every file before it answers.
        "method": "POST", "path": "/api/artist/merge/undo",
        "body": ("id",), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/listening/status"): {
        # I-006: the ListenBrainz mirror's state (off / importing / ready / error)
        # and import progress. It is polled while the first backfill runs, so it
        # is never cached (a cached "importing 40 of 900" would stall the bar) and
        # rides the fast pool with the other polls (assigned below), where a
        # saturated default pool cannot starve the progress readout.
        "method": "GET", "path": "/api/listening/status",
        "params": (), "cache": False,
    },
    ("GET", "/lb/listening/summary"): {
        # The My Listening screen in one body. Local SQLite on lb-bot's side, well
        # inside PROXY_TIMEOUT, so no slow timeout. NOT cached, and the reason is
        # that the hub cannot see the thing the answer depends on: lb-bot fills its
        # listens mirror in the background (a first import of ~180 pages, then a
        # pull every 10 minutes), and a client re-reading the summary the moment
        # the import finishes would be handed the body cached MID-import and show
        # partial totals as if final. lb-bot memoizes the aggregation itself, keyed
        # so it is always consistent with the mirror, and re-joins ownership per
        # answer, so a hub copy buys nothing worth that risk. (Not in
        # LB_LIBRARY_ROUTES for the same reason: an uncached route has nothing to
        # flush.) `coalesce: False` closes the other half of the same window: a read
        # that arrives while an identical one is upstream would JOIN it and get a
        # body computed before the import ended; the client re-reads on a timer
        # while importing and again the moment it ends. `tz` is a client-sent IANA
        # name (lb-bot's container runs in UTC) and is forwarded as sent.
        "method": "GET", "path": "/api/listening/summary",
        "params": ("period", "tz"), "cache": False, "coalesce": False,
    },
    ("GET", "/lb/listening/unowned"): {
        # "Most played but not in your library". NOT cached, for the summary's
        # reason: the rows are computed from a mirror that fills in the background
        # and from library ownership, neither of which the hub can observe, and
        # lb-bot already memoizes the aggregation consistently on its own side.
        # `coalesce: False` for the summary's reason: joining an in-flight read is
        # the same stale-by-minutes answer, just shared.
        "method": "GET", "path": "/api/listening/unowned",
        "params": ("period", "kind", "limit"), "cache": False, "coalesce": False,
    },
    ("GET", "/lb/listening/recent"): {
        # The recent-plays list, paged by `before`. Never cached: the first page
        # is the one a user reloads to see their latest listen, and every page key
        # carries a different `before` so a cache would only hold dead pages.
        "method": "GET", "path": "/api/listening/recent",
        "params": ("before", "limit"), "cache": False,
    },
    ("POST", "/lb/listening/sync"): {
        # "Sync now". lb-bot only asks its worker to pull and answers at once, so
        # the default timeout is plenty. No body keys: it takes no input.
        "method": "POST", "path": "/api/listening/sync",
        "body": (), "cache": False,
    },
    ("GET", "/lb/fresh-releases"): {
        # `limit` is not a nicety. Unbounded, this route answers with the entire
        # site-wide ListenBrainz window, which runs past PROXY_MAX_RESPONSE — and
        # a truncated body used to be served as a 200, so both clients showed an
        # empty tab with nothing anywhere saying why.
        #
        # PROXY_SLOW_TIMEOUT for the same reason its rate-limited siblings have
        # it: a cold call here is a ListenBrainz fetch (3 attempts, 25 s read
        # timeout each) plus a full Navidrome artist listing plus a SQLite pass
        # under lb-bot's index lock. One slow ListenBrainz attempt alone outlasts
        # the default 20 s, and the 502 that produced was indistinguishable from
        # lb-bot being down.
        "method": "GET", "path": "/api/fresh-releases",
        "params": ("days", "limit"), "cache": True,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/album/releases"): {
        "method": "GET", "path": "/api/album/releases",
        "params": ("rgid",), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/album/tracklist"): {
        "method": "GET", "path": "/api/album/tracklist",
        "params": ("release_mbid", "album_ids", "group_id"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/album/similar"): {
        "method": "GET", "path": "/api/album/similar",
        "params": ("artist_mbid", "artist_name", "rgid", "limit"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/meta/artist"): {
        # Editorial "About": full Wikipedia text with its CC-BY-SA attribution,
        # the Wikidata one-liner, band members / side projects, external links.
        #
        # PROXY_SLOW_TIMEOUT because a cold call is a MusicBrainz request
        # (behind lb-bot's global 1 req/sec lock, so it can queue behind a
        # running discography scan) plus two Wikimedia requests. A warm one is
        # a SQLite read.
        #
        # PROXY_CACHE_TTL_LONG: encyclopaedia text is the definition of an
        # effectively immutable upstream answer, and lb-bot caches it for 30
        # days behind this anyway.
        "method": "GET", "path": "/api/meta/artist",
        "params": ("mbid", "name", "refresh"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/meta/album"): {
        # Same chain on a release-group, plus release credits (producer,
        # engineer, writer). `release_mbid` is optional and only saves the
        # canonical-release resolution.
        #
        # lb-bot caps paragraphs and credits server-side so this cannot reach
        # PROXY_MAX_RESPONSE — a long article otherwise would, and an oversize
        # body is answered 502 tooLarge, which is correct but user-visible.
        "method": "GET", "path": "/api/meta/album",
        "params": ("rgid", "release_mbid", "refresh"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/album/lookup"): {
        # MusicBrainz album search, the other half of the "reach outside the
        # library" pair. Unlike its artist sibling this answer carries ownership
        # per candidate (`releaseOwned` + `releaseAlbumId`), which is why it is
        # also in LB_LIBRARY_ROUTES below: a fill changes who owns what, and a
        # frozen badge is a row that offers to fetch a record already on disk.
        "method": "GET", "path": "/api/album/lookup",
        "params": ("q",), "cache": True, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/artist/lookup"): {
        # MusicBrainz artist search, so a client's own search can offer a "Not
        # in your library" section that routes to the existing `mb:<mbid>`
        # external artist page. Short TTL, not PROXY_CACHE_TTL_LONG: the answer
        # is a search ranking, not an entity.
        "method": "GET", "path": "/api/artist/lookup",
        "params": ("q",), "cache": True, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/artist/similar"): {
        # "Fans also like", including the artists you do NOT own — the Discover
        # row's whole point. lb-bot marks ownership on each row rather than
        # filtering on it, unlike /lb/album/similar, which is deliberately a
        # shelf of what you already have.
        #
        # Short TTL, not PROXY_CACHE_TTL_LONG, and the reason is the opposite of
        # the usual one. The expensive half — the ListenBrainz Labs + Last.fm
        # merge — is already cached upstream for 24 hours, so a long TTL here
        # would buy almost nothing; what it WOULD do is freeze the `owned` and
        # `indexed` badges, which are recomputed per call and are the only part
        # of the answer that moves. The hub's job on this route is just to
        # collapse two clients asking at once.
        "method": "GET", "path": "/api/artist/similar",
        "params": ("mbid", "name", "limit"), "cache": True,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/artist/related"): {
        # Deezer as a THIRD similarity source, beside ListenBrainz Labs and
        # Last.fm. Deliberately its own route rather than more rows on
        # `/lb/artist/similar`: the merge there is a ranking the other two agree
        # on, and folding a third provider into it would change every existing
        # row's position for the sake of adding some. A client that wants both
        # asks for both and says which is which.
        #
        # Same short TTL as its sibling and for the same reason: the expensive
        # half is cached upstream, and the `owned`/`indexed` marking — the only
        # part that moves — is recomputed per call. Hence also in
        # LB_LIBRARY_ROUTES.
        "method": "GET", "path": "/api/artist/related",
        "params": ("mbid", "name", "limit"), "cache": True,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/deezer/chart"): {
        # Deezer's global chart, ownership-marked. Free and unauthenticated
        # upstream, so the only cost is the name→MBID resolution lb-bot does to
        # mark it — which is why the TTL is six hours and not sixty seconds.
        #
        # In LB_LIBRARY_ROUTES anyway: a six-hour cache of a row that says "you
        # don't own this" outlives the fill that makes it false, and a stale
        # badge on a Discover row is a tile that offers to fetch a record
        # already on disk. The chart itself barely moves; the badges do.
        #
        # `genre` narrows it to one of Deezer's own genre ids ("0" = global). It
        # MUST be on this list: a parameter the whitelist does not name is dropped
        # silently, so every genre would answer with the global chart and nothing
        # would say why. The cache key is built from the forwarded query, so the
        # genres do not collide.
        "method": "GET", "path": "/api/deezer/chart",
        "params": ("limit", "genre"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/deezer/editorial"): {
        # Deezer's own editorial selections — the same shape and the same rules
        # as the chart above.
        "method": "GET", "path": "/api/deezer/editorial",
        "params": ("limit", "genre"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/deezer/genres"): {
        # The genre ids the two rows above accept. Deliberately NOT in
        # LB_LIBRARY_ROUTES, unlike its siblings: it carries no ownership badge, so
        # a landed fill cannot falsify it and there is nothing for a fill to
        # invalidate. Long cache — Deezer's genre list barely moves.
        "method": "GET", "path": "/api/deezer/genres",
        "params": (), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/resolve-link"): {
        # A pasted streaming URL (Spotify/Deezer/Apple/YT-Music/Tidal/Qobuz/
        # MusicBrainz) → the MBIDs behind it, so "share this to the app" lands on
        # a real album page. Not cached: it is a one-shot the user initiated, and
        # the answer carries a `confidence` that a client renders — caching would
        # only help somebody pasting the same link twice.
        #
        # Slow because several providers resolve by SEARCHING MusicBrainz for
        # artist+title scraped from the URL or its page title, which sits on
        # lb-bot's global 1 req/sec lock.
        "method": "POST", "path": "/api/resolve-link",
        "body": ("url",), "cache": False, "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/wishlist"): {
        # "I still want this" — the persisted home for a `no_source` failure,
        # which deliberately never auto-retries. In LB_LIBRARY_ROUTES because a
        # landed album is the one event that removes a row from it.
        "method": "GET", "path": "/api/wishlist",
        "params": (), "cache": True,
    },
    ("POST", "/lb/wishlist"): {
        "method": "POST", "path": "/api/wishlist",
        "body": ("rgid", "artist", "title"), "cache": False,
    },
    ("POST", "/lb/wishlist/remove"): {
        "method": "POST", "path": "/api/wishlist/remove",
        "body": ("rgid",), "cache": False,
    },
    ("GET", "/lb/album/sources"): {
        # Ranked slskd folders for a release-group, so a client can show what it is
        # about to download instead of taking lb-bot's top pick on faith. Coverage
        # here is matched against the canonical MusicBrainz tracklist, not a file
        # count, which is what makes "a folder with enough files in it" stop
        # reading as a complete match for a completely different album.
        #
        # Deliberately NOT stripped, unlike `/lb/gap`. That one is polled every 5s,
        # so the peer file listings are pure weight; this is a one-shot read the
        # user asked for, and the per-file `matchedTo` rows are the actual evidence
        # they are judging. Different cost profile, different rule — don't
        # "unify" the two.
        #
        # A live slskd search fanning out to peers: slow, and worth a short cache
        # so re-opening the sheet doesn't start another one.
        #
        # `release_mbid`/`artist`/`album`/`total` are the caller's own resolution
        # of the release-group, forwarded so lb-bot can skip re-asking
        # MusicBrainz. Its resolver caches a transient failure for five minutes
        # and answers {} without retrying inside that window, which made one 503
        # a hard 400 on that album for everyone who asked next. The client got
        # these values from /lb/album/releases, so they cost nothing and no new
        # route is involved.
        "method": "GET", "path": "/api/album/sources",
        "params": ("rgid", "release_mbid", "artist", "album", "total"), "cache": True,
        # A live slskd fan-out, not a MusicBrainz hop — see PROXY_SEARCH_TIMEOUT.
        "timeout": PROXY_SEARCH_TIMEOUT,
    },
    ("POST", "/lb/album/download"): {
        # `release_mbid` + its metadata: the edition the user actually picked,
        # which also spares lb-bot a resolve that may be inside its MusicBrainz
        # failure cooldown. See /lb/album/sources above.
        "method": "POST", "path": "/api/album/download",
        # `excludeUsers`: "try another source" — the peers that already failed or
        # crawled for this album, so re-ranking cannot hand the fill straight back.
        # `allowMp3`: the whole-album counterpart of a gap group's opt-in. A
        # `format_rejected` fill had `mp3WouldHelp` on the wire and no route
        # that could act on it, because `/lb/album/allow-mp3` needs a group.
        # `allFiles` (B-084): "Download all files in this folder" on a picked
        # source — a JSON boolean the body filter passes through untouched. Without
        # it here the hub drops the key silently and lb-bot downloads the album's
        # tracks only; the client learns that from the `allFiles` echo in the answer.
        "body": ("rgid", "release_mbid", "artist", "title", "total_tracks",
                 "sourceUsername", "sourceFolder", "quality", "excludeUsers",
                 "allowMp3", "allFiles"),
        "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/cancel"): {
        # Stop one album fill wherever it has got to (searching, queued or
        # transferring) and record it as a retryable `cancelled` failure. Before
        # this the only cancel was slskd's own UI, which lb-bot misread as a
        # transfer failure and re-queued from another peer.
        "method": "POST", "path": "/api/album/cancel",
        "body": ("release_mbid",), "cache": False,
    },
    ("GET", "/lb/album/status"): {
        "method": "GET", "path": "/api/album/status",
        "params": ("release_mbid", "rgid"), "cache": False,
    },
    ("GET", "/lb/fills"): {
        # Every fill a client watches, in one read: `albums` keyed by release
        # mbid and `gaps` keyed by review-group id, plus `serverTime`. A Download
        # Center with eight rows used to poll eight times per tick; now one, on
        # the fast pool, so it never queues behind a source search.
        "method": "GET", "path": "/api/fills",
        "params": ("release_mbids", "group_ids"), "cache": False,
    },
    ("POST", "/lb/album/allow-mp3"): {
        # lb-bot puts the group id in the URL; the clients send it in the body so
        # the hub route table can stay a table of exact paths.
        "method": "POST", "path": "/api/gaps/{group_id}/allow-mp3",
        "path_args": ("group_id",), "body": ("group_id", "allow"), "cache": False,
        "strip": GAP_REPLY_STRIP_RULES,
    },
    ("GET", "/lb/gap"): {
        # The gap progress poll — never cached, same reasoning as album/status.
        #
        # `strip` is why this route is affordable: lb-bot embeds each source's
        # entire peer file listing, ten sources to a page, which is hundreds of KB
        # on a path a phone polls every 5s into a cache bounded by entry count
        # rather than bytes. The clients' picker shows per-source summaries
        # (peer, format, bitrate, coverage, flags, score), never the filenames, so
        # dropping them here also keeps other people's file paths off the device.
        "method": "GET", "path": "/api/gaps/{group_id}",
        "path_args": ("group_id",), "params": ("group_id", "sourcePage"),
        "cache": False, "timeout": PROXY_SLOW_TIMEOUT,
        # `tracks[].placeFolder`/`placeFile` are lb-bot HOST paths (an absolute
        # `/downloads/<folder>` path and a peer's raw filename) — its own SPA
        # reads them directly, not through the hub, and no client code needs
        # them (Q-017c). `tracks[].manualPick` is lb-bot's record of the file
        # picked by hand for a track, and its `filename` is a peer's raw slskd
        # path; no client reads any key of it, so the whole object goes (Q-043).
        "strip": GAP_STRIP_RULES,
    },
    ("GET", "/lb/gap/source-files"): {
        # One source's real folder listing, each file tagged with the track it
        # would fill. This is the counterpart to `strip` on `/lb/gap`: the poll
        # can't afford these, but the user still has to be able to answer "does
        # this peer actually have my 17 missing tracks, or 12 from a different
        # pressing" — so they are fetched once, on demand, when a source is
        # opened. Upstream expands the peer's directory for real, so it is slow.
        "method": "GET", "path": "/api/groups/{group_id}/sources/{source_index}/files",
        "path_args": ("group_id", "source_index"),
        "params": ("group_id", "source_index"), "cache": True,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/gap/search"): {
        # Find sources WITHOUT enqueuing any — the review step. `auto` does this
        # and then commits to the top pick; splitting them is what lets a client
        # show the candidates first, which matters more here than for a whole
        # album: these tracks land inside a record the user already owns, so a
        # different pressing contaminates rather than merely disappoints.
        # Answers with a task id the clients ignore, same as `auto`.
        #
        # The long timeout is not about the search — that is a background task and
        # the POST returns at once. It is about `_review_lock`: a search already in
        # flight holds it in bursts, so a second press blocks in the handler, and on
        # the default 20s that surfaced as a bare 502 for what is really "busy".
        "method": "POST", "path": "/api/groups/{group_id}/sources",
        "path_args": ("group_id",), "body": ("group_id", "force"), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/gap/auto"): {
        # Search, rank and enqueue the best source in one shot. Answers with a task
        # id the clients discard: the GET above carries `sourceTask` precisely so
        # nobody needs /api/tasks, which is not whitelisted and must stay that way.
        "method": "POST", "path": "/api/gaps/{group_id}/auto",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
        "strip": GAP_REPLY_STRIP_RULES,
    },
    ("POST", "/lb/gap/fetch"): {
        "method": "POST", "path": "/api/gaps/{group_id}/fetch",
        "path_args": ("group_id",), "body": ("group_id", "sourceId"), "cache": False,
        "strip": GAP_REPLY_STRIP_RULES,
    },
    ("POST", "/lb/gap/cancel"): {
        "method": "POST", "path": "/api/gaps/{group_id}/cancel",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
        "strip": GAP_REPLY_STRIP_RULES,
    },
    ("POST", "/lb/gap/rescan"): {
        # Re-reads the album from Navidrome and walks its folder for files
        # Navidrome hasn't indexed — slow enough to need the long timeout.
        "method": "POST", "path": "/api/gaps/{group_id}/rescan",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
        "strip": GAP_REPLY_STRIP_RULES,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
}

# The preview sidecar's CONTROL plane, and only that. There is no `/preview/stream`
# entry here and there must never be one — see PreviewProxy for the measurements.
PREVIEW_ROUTES: dict[tuple[str, str], dict] = {
    ("GET", "/preview/resolve"): {
        # artist+title (+album, +durationMs to reject a bad match) → one
        # queue-track-shaped object, or `{}` for "no preview found". `{}` is a
        # legitimate 200, never an error: same rule as lb-bot's strict=False
        # metadata chain, and a client renders it as "no preview", not a failure.
        #
        # `sign` is the one place a proxy rewrites a body instead of passing it
        # through. See PreviewProxy.call.
        #
        # Slow: a cold resolve is a search plus an extractor run on a third-party
        # site. Cached long — an `ext:` id is stable for a given track, and the
        # signature is minted AFTER the cache, so a cached hit still carries a
        # fresh `exp`.
        "method": "GET", "path": "/resolve",
        "params": ("artist", "title", "album", "durationMs"), "cache": True,
        "ttl": PROXY_CACHE_TTL_LONG, "timeout": PROXY_SLOW_TIMEOUT,
        "sign": True,
    },
    ("GET", "/preview/status"): {
        # The liveness probe, mirroring /lb/status. Carries `previewCastable`,
        # which is not a health fact but a configuration one: without
        # PREVIEW_PUBLIC_URL a Chromecast cannot fetch the audio, and a client
        # has to know that BEFORE it offers the transfer rather than discovering
        # it as silence at the speaker.
        "method": "GET", "path": "/status",
        "params": (), "cache": True, "probe": True,
    },
}

# Covers are deliberately absent. lb-bot's /api/cover/<id> serves *Navidrome*
# art keyed on a Navidrome album id — no use for a release the library does not
# have — and the art for unowned releases is a public Cover Art Archive URL the
# clients fetch directly. Proxying images would also put multi-megabyte bodies
# into a cache bounded by entry count, not bytes.

# The polled and the cancelling routes take the small fast pool (see
# PROXY_FAST_INFLIGHT); everything else shares the default one.
for _fast in (("GET", "/lb/album/status"), ("GET", "/lb/fills"), ("GET", "/lb/gap"),
              ("GET", "/lb/listening/status"), ("GET", "/lb/artist/merge"),
              ("POST", "/lb/album/cancel"), ("POST", "/lb/gap/cancel")):
    LB_ROUTES[_fast]["pool"] = "fast"
# The index mirror's pulls take their own one-slot pool (PROXY_SYNC_INFLIGHT).
for _sync in (("GET", "/lb/index/changes"), ("GET", "/lb/index/keys")):
    LB_ROUTES[_sync]["pool"] = "sync"
# Writes that make their own cached read stale. `/lb/notify` covers a landing;
# these cover the user's own action, which no landing announces.
LB_ROUTES[("POST", "/lb/wishlist")]["invalidates"] = {("GET", "/lb/wishlist")}
LB_ROUTES[("POST", "/lb/wishlist/remove")]["invalidates"] = {("GET", "/lb/wishlist")}
LB_ROUTES[("POST", "/lb/artist/release")]["invalidates"] = {("GET", "/lb/artist/discography")}
# I-019: an override changes which MBID the discography is read under, and the
# set starts a rescan; both the map and the cached discography are stale at once.
for _ovr in (("POST", "/lb/artist/override"), ("POST", "/lb/artist/override/remove")):
    LB_ROUTES[_ovr]["invalidates"] = {("GET", "/lb/overrides"),
                                      ("GET", "/lb/artist/discography")}
# I-022 / I-023: an album retag, a cover change and an artist merge change the
# override/merge lists and what the discography reads as owned. `preview` writes
# nothing and flushes nothing.
for _alb in (("POST", "/lb/album/override"), ("POST", "/lb/album/cover"),
             ("POST", "/lb/album/override/remove"), ("POST", "/lb/artist/merge"),
             ("POST", "/lb/artist/merge/undo")):
    LB_ROUTES[_alb]["invalidates"] = {("GET", "/lb/overrides"),
                                      ("GET", "/lb/artist/discography")}

_PATH_ARG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _url_for_log(url: str) -> str:
    """An upstream URL as a log line may show it: scheme, host and path, never the
    query (B-136). The query is where the credentials go — `navidrome_password` on
    the fingerprint route, Subsonic's `t`/`s` token pair on every Navidrome call —
    and a timeout line used to print all of it."""
    return url.split("?", 1)[0]


def _proxy_upstream_blocking(method: str, url: str, body: Optional[bytes],
                             token: str, timeout: float,
                             label: str) -> tuple[int, bytes, str]:
    """One upstream call, off the event loop (same shape as the savePlayQueue mirror).

    Upstream status codes are passed through (503 cold index, 404 unanalyzed track,
    400 feature disabled) so the clients' existing fail-soft branches keep working.
    """
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # One byte past the cap, so "exactly at the limit" and "over it" are
            # distinguishable. `read(PROXY_MAX_RESPONSE)` alone TRUNCATES SILENTLY:
            # the status stays 200 and the body is a half-finished JSON document,
            # which every client then fails to parse and reports as "couldn't
            # reach the service" — a corrupt success is worse than a clean error,
            # and this was invisible on every route until lb-bot's fresh-releases
            # feed grew past 4 MB and took both clients' tabs down with it.
            raw = r.read(PROXY_MAX_RESPONSE + 1)
            if len(raw) > PROXY_MAX_RESPONSE:
                log(f"{label} proxy: upstream body exceeded {PROXY_MAX_RESPONSE} bytes")
                return (502, json.dumps({
                    "error": f"{label} response exceeded the {PROXY_MAX_RESPONSE}-byte "
                             "proxy limit", "tooLarge": True}).encode(),
                        "application/json")
            return (r.status, raw,
                    r.headers.get("Content-Type") or "application/json")
    except urllib.error.HTTPError as e:
        try:
            payload = e.read(PROXY_MAX_RESPONSE)
        except Exception:  # noqa: BLE001
            payload = b""
        return e.code, payload, e.headers.get("Content-Type") or "application/json"
    except Exception as e:  # noqa: BLE001 — never raise into the WS loop
        # "Slow" and "down" used to be the same 502, so a client could only say
        # "lb-bot is busy or unreachable" — a message that sends the user to
        # look at a service that is healthy and still searching.
        reason = getattr(e, "reason", e)
        if (isinstance(e, TimeoutError) or isinstance(reason, TimeoutError)
                or "timed out" in str(e).lower()):
            log(f"{label} proxy: upstream timed out after {timeout}s: {_url_for_log(url)}")
            return (504, json.dumps({"error": f"{label} timed out", "busy": True}).encode(),
                    "application/json")
        log(f"{label} proxy: upstream failed:", e)
        return (502, json.dumps({"error": f"{label} unreachable", "down": True}).encode(),
                "application/json")


class HttpProxy:
    """Route table + auth + concurrency cap + shared result cache.

    One instance per upstream. Subclasses supply the URL prefix they answer for,
    the upstream base URL, and (optionally) an upstream bearer token and a probe
    response — everything else is identical between them.
    """

    prefix = ""                                  # e.g. "/sonic"
    label = "proxy"
    routes: dict[tuple[str, str], dict] = {}

    def __init__(self) -> None:
        self._pools = {"default": asyncio.Semaphore(PROXY_MAX_INFLIGHT),
                       "fast": asyncio.Semaphore(PROXY_FAST_INFLIGHT),
                       "sync": asyncio.Semaphore(PROXY_SYNC_INFLIGHT)}
        self._cache: dict[str, tuple[float, int, bytes, str]] = {}
        # One invalidation generation PER ROUTE, not one for the proxy. A read
        # caches its answer only if its own route's generation did not move
        # while it was upstream. A single proxy-wide counter meant any
        # invalidation voided every read in flight: during a bulk index build
        # lb-bot pushes `/lb/index` every 2 s, and that push (which drops only
        # the discography cache) stopped every cacheable answer slower than
        # 2 s from ever being cached — `/lb/album/sources` (45-150 s) and the
        # six-hour MusicBrainz routes above all.
        self._cache_gen: dict[tuple[str, str], int] = {}
        # Identical GETs in flight at once share one upstream call. Two devices
        # opening the same source sheet used to start two slskd searches; the
        # cache only ever helped the second caller once the first had returned.
        self._inflight: dict[str, asyncio.Future] = {}

    # ----- per-upstream ---------------------------------------------------- #
    @property
    def upstream(self) -> str:
        raise NotImplementedError

    @property
    def upstream_token(self) -> str:
        return ""

    @property
    def enabled(self) -> bool:
        # An empty HUB_TOKEN already means "accept any client"; refusing to run the
        # proxy in that state is what stops it becoming an open relay upstream.
        return bool(self.upstream and TOKEN)

    @property
    def disabled_reason(self) -> str:
        return ("upstream URL is unset" if not self.upstream
                else "HUB_TOKEN is empty (refusing to run as an open relay)")

    def disabled_probe(self, route: tuple[str, str]) -> Optional[tuple]:
        """Answer for the liveness probe while the proxy is off, so clients learn
        the feature is unconfigured instead of reading an error."""
        spec = self.routes.get(route)
        if spec and spec.get("probe"):
            return _http_json(200, {"configured": False, "upstreamReachable": False})
        return None

    def augment(self, spec: dict, status: int, data: bytes) -> tuple[int, bytes, str]:
        return status, data, "application/json"

    def _note_upstream_result(self, upstream_status: int, data: bytes) -> None:
        """Hook for a subclass to react to a proxied call's raw outcome, on EVERY
        route — not just the probe's own. No-op here; `LbProxy` overrides it for
        B-005 (a failed call on any lb-bot route should be reflected by the next
        `/lb/status`, not just the probe route's own next call)."""
        return

    @staticmethod
    def _strip(spec: dict, status: int, data: bytes) -> bytes:
        """Drop fields the clients never render from a successful response.

        Not a security control — the whitelist is that. It is a projection:
        bandwidth (`sources.files`) and path hygiene (`placeFolder`/`placeFile`,
        `manualPick`), for an upstream view shaped for a local web UI that carries a
        payload nobody on a phone wants. Each rule is (list_key, field): the field
        is removed from every object in that list. `list_key` is a top-level key or
        a dotted path (`"gap.tracks"`) for views nested in a POST reply; a missing
        or non-dict intermediate leaves the rule a no-op. Anything unexpected
        (a non-200, a non-JSON body, a shape that doesn't match) passes through
        untouched, because a projection failing must never fail the request.
        """
        rules = spec.get("strip") or ()
        if not rules or status != 200:
            return data
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                return data
            for list_key, field in rules:
                node: object = payload
                for part in list_key.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                for row in node if isinstance(node, list) else []:
                    if isinstance(row, dict):
                        row.pop(field, None)
            return json.dumps(payload).encode()
        except Exception:  # noqa: BLE001 — never fail a request over a projection
            return data

    # ----- cache ----------------------------------------------------------- #
    def _cache_get(self, key: str) -> Optional[tuple[int, bytes, str]]:
        hit = self._cache.get(key)
        if hit is None:
            return None
        expiry, status, body, ctype = hit
        if expiry < time.monotonic():
            del self._cache[key]
            return None
        return status, body, ctype

    def _cache_put(self, key: str, status: int, body: bytes, ctype: str,
                   ttl: float = PROXY_CACHE_TTL) -> None:
        # Only cache successes — a cold-index 503 must not stick for a minute.
        if status != 200:
            return
        if len(self._cache) >= PROXY_CACHE_MAX:
            for old in sorted(self._cache, key=lambda k: self._cache[k][0])[:PROXY_CACHE_MAX // 4]:
                del self._cache[old]
        self._cache[key] = (time.monotonic() + ttl, status, body, ctype)

    def invalidate(self, routes: set[tuple[str, str]]) -> int:
        """Drop every cached answer for these routes, and void the ones in flight.

        The generation bump is the half that matters under load: a read that went
        upstream a moment BEFORE the invalidation returns the old answer a moment
        AFTER it, and caching that would re-arm the very staleness this clears.
        Bumped for exactly these routes: a read of any other route in flight
        across this call has nothing stale about it and still caches.
        """
        for r in routes:
            r = tuple(r)
            self._cache_gen[r] = self._cache_gen.get(r, 0) + 1
        wanted = [list(r) for r in routes]
        dropped = 0
        for key in list(self._cache):
            try:
                route = json.loads(key)[0]
            except Exception:  # noqa: BLE001
                continue
            if route in wanted:
                del self._cache[key]
                dropped += 1
        return dropped

    # ----- request handling ------------------------------------------------ #
    @staticmethod
    def _filtered_params(spec: dict, query: str) -> list[tuple[str, str]]:
        allowed = spec.get("params") or ()
        out = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=False)
               if k in allowed]
        if spec.get("nd") and ND_USER and ND_PASS:
            # The core reads Navidrome play history for the fingerprint. The hub holds
            # these already (savePlayQueue mirror) — clients never send them.
            out += [("navidrome_user", ND_USER), ("navidrome_password", ND_PASS)]
        return out

    @staticmethod
    def _upstream_path(spec: dict, params: list[tuple[str, str]],
                       body: Optional[dict]) -> Optional[str]:
        """Fill `{name}` placeholders in the upstream path from params/body.

        The value goes into a URL path, so it is validated rather than escaped:
        an id that isn't a plain token is a client bug or an attempt to walk out
        of the whitelisted route, and both deserve a 400, not a best-effort quote.
        Consumed keys are removed so they aren't also forwarded as data.
        """
        path = spec["path"]
        for name in spec.get("path_args") or ():
            value = ""
            if body is not None and name in body:
                value = str(body.pop(name) or "")
            else:
                hit = next((v for k, v in params if k == name), "")
                value = str(hit or "")
                params[:] = [(k, v) for k, v in params if k != name]
            if not _PATH_ARG_RE.match(value):
                return None
            path = path.replace("{" + name + "}", value)
        return path

    @staticmethod
    def _filtered_body(spec: dict, raw: bytes) -> Optional[dict]:
        """Whitelist the client's JSON body. None = malformed (→ 400)."""
        allowed = spec.get("body") or ()
        try:
            data = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        out = {k: v for k, v in data.items() if k in allowed}
        items = out.get("items")
        if isinstance(items, list):
            # Alchemy items are the only nested structure we forward; keep it to the
            # three fields the API defines rather than relaying arbitrary objects.
            out["items"] = [{k: v for k, v in it.items() if k in ("op", "id", "type")}
                            for it in items if isinstance(it, dict)]
        return out

    async def call(self, route: tuple[str, str], spec: dict,
                   params: list[tuple[str, str]],
                   body: Optional[dict]) -> tuple[int, bytes, str]:
        upstream_path = self._upstream_path(spec, params, body)
        if upstream_path is None:
            return 400, b'{"error":"invalid path parameter"}', "application/json"

        # The cache key deliberately IGNORES PROXY_NO_CACHE_PARAMS. `refresh=1` is
        # not a different resource; it is the same one with an instruction about
        # how to fetch it. Keying on it would hand a refresh its own cache slot,
        # so the stale copy every ordinary caller reads would survive the refresh
        # completely untouched — on the meta routes, for the six hours of
        # PROXY_CACHE_TTL_LONG. Sharing the key instead means the fresh answer is
        # stored where the next ordinary call will find it, which is what a user
        # pressing refresh actually means.
        cache_params = sorted((k, v) for k, v in params
                              if k not in PROXY_NO_CACHE_PARAMS)
        key = json.dumps([route, upstream_path, cache_params, body],
                         sort_keys=True, default=str)
        refresh = any(k in PROXY_NO_CACHE_PARAMS and v not in ("", "0", "false")
                      for k, v in params)
        if spec.get("cache") and not refresh:
            hit = self._cache_get(key)
            if hit is not None:
                return hit

        # `coalesce: False` is the index mirror's sync routes and the listening
        # summary / unowned reads: a caller that joins a read which set off before
        # a write (or before the listens import ended) gets the earlier answer.
        coalesce = (spec["method"] == "GET" and not refresh
                    and spec.get("coalesce", True))
        leader = self._inflight.get(key) if coalesce else None
        if leader is not None:
            # Shielded: a follower being cancelled must not cancel the shared call.
            return await asyncio.shield(leader)
        fut: Optional[asyncio.Future] = None
        if coalesce:
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
        try:
            result = await self._call_upstream(route, spec, upstream_path, params,
                                               body, key)
            if fut is not None:
                fut.set_result(result)
            return result
        except BaseException as e:
            if fut is not None and not fut.done():
                fut.set_exception(e)
            raise
        finally:
            if fut is not None:
                self._inflight.pop(key, None)

    async def _call_upstream(self, route: tuple[str, str], spec: dict,
                             upstream_path: str, params: list[tuple[str, str]],
                             body: Optional[dict], key: str) -> tuple[int, bytes, str]:
        gen = self._cache_gen.get(route, 0)
        url = self.upstream + upstream_path
        # nd creds are injected here, not logged: keep them out of the cache key too.
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode() if body is not None else None

        pool = spec.get("pool", "default")
        # A route whose cost depends on which query it was given (see
        # `pool_when`: param name -> pool).
        for name, named_pool in (spec.get("pool_when") or {}).items():
            if any(k == name for k, _v in params):
                pool = named_pool
        sem = self._pools[pool]
        queue_timeout = (PROXY_QUEUE_TIMEOUT_FAST if pool in ("fast", "sync")
                         else spec.get("timeout", PROXY_TIMEOUT))
        try:
            await asyncio.wait_for(sem.acquire(), timeout=queue_timeout)
        except asyncio.TimeoutError:
            log(f"{self.label} proxy: no {pool} slot within {queue_timeout}s for "
                f"{_url_for_log(url)}")
            return (503, json.dumps({"error": f"{self.label} proxy busy",
                                     "busy": True}).encode(), "application/json")
        try:
            status, data, ctype = await self._fetch(spec, upstream_path, params, payload)
        finally:
            sem.release()

        # Captured BEFORE augment: the probe route rewrites any upstream status
        # into a 200 verdict, and caching that verdict froze "unreachable" for a
        # minute after lb-bot came back (and "reachable" for a minute after it
        # went away). Only an upstream success is worth remembering.
        upstream_status = status
        self._note_upstream_result(upstream_status, data)
        if spec.get("probe"):
            status, data, ctype = self.augment(spec, status, data)
        data = self._strip(spec, status, data)
        if (spec.get("cache") and gen == self._cache_gen.get(route, 0)
                and upstream_status == 200):
            self._cache_put(key, status, data, ctype,
                            spec.get("ttl", PROXY_CACHE_TTL))
        # A write that succeeded makes its own read stale: adding to the wishlist
        # answered the list without the row for up to a minute.
        if 200 <= status < 300 and spec.get("invalidates"):
            self.invalidate(set(spec["invalidates"]))
        # A polled job route: any answer the lb-bot gave (not its 5xx, not the
        # hub's own error) means the job's list may have moved.
        if status < 500 and spec.get("invalidates_on_answer"):
            self.invalidate(set(spec["invalidates_on_answer"]))
        return status, data, ctype

    async def _fetch(self, spec: dict, upstream_path: str,
                     params: list[tuple[str, str]],
                     payload: Optional[bytes],
                     timeout: Optional[float] = None) -> tuple[int, bytes, str]:
        """The upstream call itself, off the event loop, plus the route's 404
        fallback. Takes no slot: the caller holds one, or — the hub's own
        liveness probe — deliberately does not need one. `timeout` overrides the
        route's for the first call only; the fallback keeps the route's own."""
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        timeout_route = spec.get("timeout", PROXY_TIMEOUT)
        result = await asyncio.to_thread(
            _proxy_upstream_blocking, spec["method"], self.upstream + upstream_path + query,
            payload, self.upstream_token,
            timeout if timeout is not None else timeout_route, self.label)
        if result[0] == 404 and spec.get("fallback"):
            # The upstream is older than the route: ask the one it has. Once,
            # and only on a 404 — a 5xx or a timeout is an answer about the
            # service, not about the route, and retrying it would only double
            # the wait before the client hears it.
            result = await asyncio.to_thread(
                _proxy_upstream_blocking, spec["method"],
                self.upstream + spec["fallback"] + query,
                payload, self.upstream_token, timeout_route, self.label)
        return result

    async def handle(self, protocol: Any, raw_path: str,
                     headers: Any, method: str) -> Optional[tuple]:
        """Return an (status, headers, body) triple to answer as plain HTTP, or None
        to let the request continue into the WebSocket handshake."""
        path, _, query = raw_path.partition("?")
        path = path.rstrip("/") or "/"
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            return None  # not ours — the next proxy, or the WS handshake, proceeds

        if not self.enabled:
            # The probe still answers, so clients learn the feature is off instead
            # of erroring. Anything else is a clean 503 they can fail soft on.
            probe = self.disabled_probe((method, path))
            if probe is not None:
                return probe
            return _http_json(503, {"error": f"{self.label} proxy disabled: "
                                             f"{self.disabled_reason}"})

        auth = (headers.get("Authorization") or "") if headers is not None else ""
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
            return _http_json(401, {"error": "unauthorized"})

        spec = self.routes.get((method, path))
        if spec is None:
            return _http_json(404, {"error": "unknown route"})

        body: Optional[dict] = None
        if spec["method"] == "POST":
            raw = await _read_body(protocol, headers)
            if raw is None:
                return _http_json(400, {"error": "request body too large or unreadable"})
            body = self._filtered_body(spec, raw)
            if body is None:
                return _http_json(400, {"error": "malformed JSON body"})

        params = self._filtered_params(spec, query)
        try:
            status, data, ctype = await self.call((method, path), spec, params, body)
        except Exception as e:  # noqa: BLE001 — a proxy fault must not kill the socket
            log(f"{self.label} proxy failed:", e)
            return _http_json(502, {"error": "proxy failure"})
        dlog(f"{self.label.upper()} {method} {path} -> {status} ({len(data)}B)")
        return _http_response(status, data, ctype)


class SonicProxy(HttpProxy):
    """AudioMuse-AI Tier 2 (design: ../DESIGN-hub-audiomuse-proxy.md)."""

    prefix = "/sonic"
    label = "audiomuse"
    routes = SONIC_ROUTES

    @property
    def upstream(self) -> str:
        return AUDIOMUSE_URL

    @property
    def upstream_token(self) -> str:
        return AUDIOMUSE_TOKEN

    @property
    def disabled_reason(self) -> str:
        return ("AUDIOMUSE_URL is unset" if not AUDIOMUSE_URL
                else "HUB_TOKEN is empty (refusing to run as an open relay)")

    def augment(self, spec: dict, status: int, data: bytes) -> tuple[int, bytes, str]:
        """`/sonic/clap/stats` doubles as the Tier-2 liveness probe: pass the upstream
        stats through, plus the hub's own view, so both clients agree on Tier-2 state
        from one source. Always 200 — 'unreachable' is an answer, not an error."""
        stats: dict = {}
        if status == 200:
            try:
                parsed = json.loads(data or b"{}")
                if isinstance(parsed, dict):
                    stats = parsed
            except Exception:  # noqa: BLE001
                stats = {}
        stats["configured"] = True
        stats["upstreamReachable"] = status == 200
        return 200, json.dumps(stats).encode(), "application/json"


class LbProxy(HttpProxy):
    """lb-bot library-gap intelligence (design: ../DESIGN-lbbot-client-integration.md)."""

    prefix = "/lb"
    label = "lbbot"
    routes = LB_ROUTES

    def __init__(self) -> None:
        super().__init__()
        # The hub's own verdict on lb-bot, for `welcome.lb` and the `lb` frame.
        # Moved by the 60 s probe, by every `/lb/status` answer that reached
        # lb-bot, and to True by any authenticated inbound push — lb-bot talking
        # to us is proof enough. Only ever through `_set_available`.
        self.available = False
        self._probed = asyncio.Event()
        self._announcing: set[asyncio.Task] = set()

    @property
    def upstream(self) -> str:
        return LBBOT_URL

    @property
    def disabled_reason(self) -> str:
        return ("LBBOT_URL is unset" if not LBBOT_URL
                else "HUB_TOKEN is empty (refusing to run as an open relay)")

    def advertised_routes(self) -> list[str]:
        """What this hub can actually proxy — one list, served by `/lb/status` and
        by `welcome.lb`, so the two can never disagree about a route.

        A client ships independently of the hub and the hub is a long-running
        process, so "my client is newer than the hub it is talking to" is a
        permanent condition, not an edge case — and without this it surfaces as a
        button that does nothing, because a 404 from an unknown route is a
        perfectly ordinary HTTP response. Advertising the list lets a client say
        which feature its hub is too old for.

        Empty while the proxy is off: it can proxy nothing then, and that is also
        what `/lb/status` says in `disabled_probe`, so `welcome.lb` and the probe
        agree in that state too.
        """
        if not self.enabled:
            return []
        return sorted(f"{method} {path}" for method, path in self.routes)

    def disabled_probe(self, route: tuple[str, str]) -> Optional[tuple]:
        # The base class's answer plus `routes`, so a client reads the same
        # object from `/lb/status` and `welcome.lb` whether lb-bot is on or off.
        spec = self.routes.get(route)
        if spec and spec.get("probe"):
            return _http_json(200, {"configured": False, "upstreamReachable": False,
                                    "routes": self.advertised_routes(),
                                    "webUrl": self.web_url()})
        return None

    def welcome_info(self) -> dict:
        """`welcome.lb` and the `lb` frame: the probe's answer, without a client
        having to ask for it."""
        return {"available": bool(self.enabled and self.available),
                "routes": self.advertised_routes(),
                "webUrl": self.web_url()}

    def web_url(self) -> str:
        """lb-bot's web UI for a person to open, or "" while the proxy is off.

        LBBOT_WEB_URL when set, else the upstream the hub itself reaches — the
        same address on a LAN install. Only ever an http(s) URL: a client opens
        it in a browser, so anything else is not passed on."""
        if not self.enabled:
            return ""
        url = LBBOT_WEB_URL or self.upstream
        return url if url.startswith(("http://", "https://")) else ""

    def _set_available(self, value: bool) -> None:
        """The one way the verdict moves. A CHANGE is broadcast as `t: "lb"`
        (ruling R18): `welcome.lb` is a connect-time snapshot, and a client that
        trusts it and stops probing would otherwise never hear lb-bot come up or
        go away while it stays connected. An unchanged verdict sends nothing —
        the probe runs every 60 s and most of its answers are "same as before".

        Scheduled rather than awaited because `augment` and the inbound handlers
        call this synchronously; the frame is built NOW, so two quick flips
        still reach clients in the order they happened.
        """
        value = bool(value)
        if value == self.available:
            return
        self.available = value
        log(f"lbbot {'available' if value else 'UNAVAILABLE'} (hub verdict changed)")
        hub = HUB_INSTANCE
        if hub is None:
            return
        frame = {"t": "lb", **self.welcome_info()}
        try:
            task = asyncio.get_running_loop().create_task(hub._broadcast(frame))  # noqa: SLF001
        except RuntimeError:  # no loop (an import-time caller): nobody to tell
            return
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    def mark_alive(self) -> None:
        """An authenticated inbound push from lb-bot: it is up, whatever the last
        probe said. Only ever moves the verdict to True — the probe is what
        notices lb-bot going away."""
        if self.enabled:
            self._set_available(True)

    def _note_upstream_result(self, upstream_status: int, data: bytes) -> None:
        """B-005: a proxied call that reached the hub but not lb-bot says
        something about `/lb/status`'s cached answer. Without this, `/lb/status`
        kept answering `upstreamReachable: true` for up to PROXY_CACHE_TTL (60 s)
        after lb-bot died: its own cached answer from before the crash was still
        fresh, and nothing invalidated it just because some OTHER route failed
        meanwhile.

        Only `_proxy_upstream_blocking`'s 502 `down` (connection refused /
        unreachable) flips the hub's cross-cutting verdict at once, broadcasting
        `t:"lb"` to every connected client, same as a probe tick. A 504 `busy`
        (a socket op timed out) is a healthy lb-bot still working — a
        rate-limited MusicBrainz hop, a 150 s source search — and flipping the
        verdict on it blanked every client's lb-bot layer until the next probe
        tick (ruling R18; the busy-vs-down split of `_proxy_upstream_blocking`
        and `probe()`). Both drop `/lb/status`'s own cache entry, so its very
        next call is forced upstream and answers from lb-bot, not from memory.

        Checks the `down`/`busy` flag, not just the status code: a 502 also covers
        `tooLarge` (lb-bot answered fine, its body was just oversize) — not a
        reachability signal, and must not flip the verdict or drop the cache.
        """
        if upstream_status not in (502, 504):
            return
        try:
            payload = json.loads(data or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if not isinstance(payload, dict):
            return
        if payload.get("down"):
            self._set_available(False)
        elif not payload.get("busy"):
            return
        self.invalidate({("GET", "/lb/status")})

    def augment(self, spec: dict, status: int, data: bytes) -> tuple[int, bytes, str]:
        """`/lb/status` is the liveness probe, and only the reachability verdict is
        passed on — neither `/api/health`'s body nor (on an older lb-bot) the
        summary's, a large object about the Fill-gaps workspace no client renders.
        `routes` is `advertised_routes()`.
        """
        self._set_available(status == 200)
        return 200, json.dumps({
            "configured": True,
            "upstreamReachable": status == 200,
            "routes": self.advertised_routes(),
            "webUrl": self.web_url(),
        }).encode(), "application/json"

    async def probe(self) -> bool:
        """The hub's own liveness check: the `/lb/status` upstream and its 404
        fallback, but no cache and NO pool slot. Through the default pool it would
        queue behind four source searches and time out into a 503 — and a probe
        that cannot tell "busy" from "down" would mark a healthy lb-bot
        unavailable to every client that connects meanwhile."""
        if not self.enabled:
            self._set_available(False)
            return False
        spec = self.routes[("GET", "/lb/status")]
        try:
            status, _data, _ctype = await self._fetch(spec, spec["path"], [], None,
                                                      timeout=LB_PROBE_TIMEOUT)
            self._set_available(status == 200)
        except Exception as e:  # noqa: BLE001 — a probe must never kill its loop
            log("lbbot probe failed:", e)
            self._set_available(False)
        finally:
            self._probed.set()
        return self.available

    async def probe_loop(self) -> None:
        while True:
            await self.probe()
            await asyncio.sleep(LB_PROBE_INTERVAL)

    async def wait_first_probe(self, timeout: float) -> None:
        """Return once the first probe has a verdict, or after `timeout`.

        A wait that expires ENDS the waiting for everyone: `_probed` is set, so
        the next welcome goes straight out. Otherwise every handshake during a
        hanging first probe would pay the full `timeout` again. Whatever the
        late probe decides then reaches those clients as an `lb` frame.
        """
        if not self.enabled or self._probed.is_set():
            return
        try:
            await asyncio.wait_for(self._probed.wait(), timeout)
        except asyncio.TimeoutError:
            self._probed.set()
            log(f"lbbot probe: no verdict within {timeout}s; welcomes say "
                "unavailable until it answers")


def preview_sign(track_id: str, exp: int) -> str:
    """The stream-URL capability signature: HMAC-SHA256(PREVIEW_SECRET, id\\nexp).

    Newline-separated rather than concatenated so the two fields cannot be slid
    past each other — `ext:yt:a` + `1` and `ext:yt:a1` + `` would otherwise sign
    the same bytes.
    """
    return hmac.new(PREVIEW_SECRET.encode(),
                    f"{track_id}\n{exp}".encode(), hashlib.sha256).hexdigest()


def preview_stream_url(track_id: str) -> str:
    """A capability URL for one `ext:` id, good for PREVIEW_TTL seconds.

    Falls back to PREVIEW_URL when PREVIEW_PUBLIC_URL is unset. That URL is
    typically an internal Docker hostname, so it works only for a client on the
    same network and never for a Chromecast — which is exactly the state
    `previewCastable: false` announces.
    """
    exp = int(time.time()) + PREVIEW_TTL
    base = PREVIEW_PUBLIC_URL or PREVIEW_URL
    query = urllib.parse.urlencode(
        {"id": track_id, "exp": exp, "sig": preview_sign(track_id, exp)})
    return f"{base}/stream?{query}"


class PreviewProxy(HttpProxy):
    """The preview sidecar's control plane (PROTOCOL §16).

    **The hub does not stream, and that is a decision, not an omission.** The
    obvious design — a third route here that relays the audio — is not
    implementable on this transport, and the reasons are structural:

      * `HttpProxy.handle` ends in `_http_response`, whose body is a single
        `bytes`. `process_request`'s return is coerced through `AbortHandshake`
        into a buffered body with a library-computed `Content-Length`
        (`websockets/legacy/server.py:233`) — there is no chunk-by-chunk path.
      * `PROXY_MAX_RESPONSE` is 4 MB. A three-minute preview is larger.
      * `PROXY_MAX_INFLIGHT` is 4 per proxy, so one playing stream would hold a
        quarter of the preview proxy's slots for its whole duration.
      * Serving media here at all would mean writing to `self.transport` by hand
        and raising `BrokenPipeError` to suppress the library's own write — and
        even then `open_timeout` (the handshake deadline, ~165 s) truncates the
        body mid-stream. That is the exact failure this stack was just bitten by
        on the slow lb-bot routes.

    So the sidecar is the media origin and the hub keeps the control plane,
    which is ordinary buffered JSON and fits this class perfectly. The sidecar
    had to be a separate process anyway — a broken extractor must not be able to
    take the session relay down with it — so serving its own bytes costs nothing.
    """

    prefix = "/preview"
    label = "preview"
    routes = PREVIEW_ROUTES

    @property
    def upstream(self) -> str:
        return PREVIEW_URL

    @property
    def enabled(self) -> bool:
        # PREVIEW_SECRET is part of "enabled", unlike the other two proxies'
        # tokens: without it every streamUrl the hub mints signs with an empty
        # key, the sidecar refuses all of them, and the feature fails at the
        # point of playback rather than at the point of configuration.
        return bool(PREVIEW_URL and TOKEN and PREVIEW_SECRET)

    @property
    def disabled_reason(self) -> str:
        if not PREVIEW_URL:
            return "PREVIEW_URL is unset"
        if not TOKEN:
            return "HUB_TOKEN is empty (refusing to run as an open relay)"
        return "PREVIEW_SECRET is unset (stream URLs would be unsignable)"

    def disabled_probe(self, route: tuple[str, str]) -> Optional[tuple]:
        # Same contract as the base class, plus the castability flag — a client
        # reads one object whether the feature is on or off.
        spec = self.routes.get(route)
        if spec and spec.get("probe"):
            return _http_json(200, {"configured": False, "upstreamReachable": False,
                                    "previewCastable": False})
        return None

    def augment(self, spec: dict, status: int, data: bytes) -> tuple[int, bytes, str]:
        """`/preview/status` is the liveness probe, mirroring `LbProxy.augment`.

        `previewCastable` is the hub's own answer, not the sidecar's: only this
        process knows whether it was given a publicly reachable address to sign
        stream URLs against.

        `extractorBlocked` is the sidecar's, passed through. It exists because
        "the extractor is being refused" and "nothing matched" are otherwise the
        same thing to a client — both are an empty resolve against a process that
        reports itself healthy — so the whole feature can be dead while every
        probe says fine. It is additive: a client that does not read it behaves
        exactly as before.
        """
        out = {
            "configured": True,
            "upstreamReachable": status == 200,
            "previewCastable": bool(PREVIEW_PUBLIC_URL),
        }
        if status == 200:
            try:
                parsed = json.loads(data or b"{}")
                if isinstance(parsed, dict):
                    for key in ("extractorBlocked", "cookies"):
                        if key in parsed:
                            out[key] = bool(parsed[key])
            except Exception:  # noqa: BLE001 — a probe must not fail on a body
                pass
        return 200, json.dumps(out).encode(), "application/json"

    @staticmethod
    def _sign_body(data: bytes) -> bytes:
        """Rewrite a resolve answer's `streamUrl` into a signed capability URL.

        The sidecar returns a bare `ext:` id and no URL at all, so PREVIEW_SECRET
        never has to be handed to a client and the capability cannot be minted by
        anything but these two processes.

        Anything unexpected passes through untouched, on the same rule as
        `_strip`: a transform failing must never fail the request. `{}` — "no
        preview found" — has no id and is returned unchanged.
        """
        try:
            payload = json.loads(data or b"{}")
        except Exception:  # noqa: BLE001
            return data
        if not isinstance(payload, dict):
            return data
        track_id = payload.get("id")
        if not isinstance(track_id, str) or not track_id.startswith("ext:"):
            return data
        payload["streamUrl"] = preview_stream_url(track_id)
        return json.dumps(payload).encode()

    async def call(self, route: tuple[str, str], spec: dict,
                   params: list[tuple[str, str]],
                   body: Optional[dict]) -> tuple[int, bytes, str]:
        """Pass through, then sign.

        This is the one place a proxy in this file transforms a body rather than
        relaying it, which is against the house rule, so: it happens here, AFTER
        `super().call`, precisely because that is after the cache. A resolution is
        cached for six hours (an `ext:` id is stable) but a signature is only good
        for PREVIEW_TTL — signing before the cache would store the *signature*
        too, and every cache hit past the TTL would serve a URL the sidecar
        rejects. Cache the identity, mint the capability per answer.
        """
        status, data, ctype = await super().call(route, spec, params, body)
        if spec.get("sign") and status == 200:
            data = self._sign_body(data)
        return status, data, ctype


def _http_response(status: int, body: bytes, ctype: str) -> tuple:
    try:
        code = http.HTTPStatus(status)
    except ValueError:
        code = http.HTTPStatus.BAD_GATEWAY
    return code, [("Content-Type", ctype), ("Connection", "close")], body


def _http_json(status: int, payload: dict) -> tuple:
    return _http_response(status, json.dumps(payload).encode(), "application/json")


async def _read_body(protocol: Any, headers: Any) -> Optional[bytes]:
    """Read a Content-Length body off the connection.

    `websockets` reads only the request line + headers before handing control to
    process_request (a WS handshake has no body), so the POST body is still sitting
    in the protocol's StreamReader.
    """
    try:
        length = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return b""
    if length <= 0:
        return b""
    if length > PROXY_MAX_BODY:
        return None
    try:
        raw = await asyncio.wait_for(protocol.reader.readexactly(length), timeout=10)
    except Exception:  # noqa: BLE001
        # Forwarding `{}` here sent a cancel or a download to lb-bot with an
        # empty body, which it answered with a 400 the client could not explain.
        protocol.body_consumed = True
        return None
    protocol.body_consumed = True
    return raw


async def _drain_body(protocol: Any, headers: Any) -> None:
    """Swallow an unread request body before answering.

    Answering a POST without reading its body (401/404/405) closes the socket while
    the client is still writing, which surfaces on the client as a connection reset
    instead of the status we meant to send.
    """
    if getattr(protocol, "body_consumed", False):
        return
    try:
        length = min(int(headers.get("Content-Length") or 0), PROXY_MAX_BODY)
    except (TypeError, ValueError):
        return
    if length <= 0:
        return
    try:
        await asyncio.wait_for(protocol.reader.readexactly(length), timeout=5)
    except Exception:  # noqa: BLE001
        pass
    protocol.body_consumed = True


# --------------------------------------------------------------------------- #
# Stations (PROTOCOL §18): radio and autoplay, generated by the hub
#
# A station is a seed plus a character. The hub keeps what it served, what was
# skipped and liked, and a play history across sessions, and answers "the next N
# tracks" from AudioMuse (Tier 2) or Navidrome's similar-songs agent (Tier 1). It
# holds no queue: the clients put what it answers into theirs.
#
# The gate is AudioMuse's own neighbourhood. A candidate must sit within a rank
# ceiling of an anchor's nearest list (an anchor is a seed track, or a liked one).
# Nothing else admits a track — not alchemy, not a similar artist, not a feature
# distance — so every source only chooses among what the gate already holds, and a
# station never walks from what it last played (the walk that took Lana Del Rey to
# OsamaSon). Explore alone may take a second hop: a track within 200 of the list of
# a track it served AND that was then played through.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _StationTuning:
    """One character's numbers (PROTOCOL §18.3's table)."""
    ceiling: int          # gate: rank in an anchor's nearest list
    ceiling_max: int      # where the widening ladder stops raising it
    alchemy_every: int    # 0 = never, 1 = every batch, 2 = every other batch
    temperature: float    # alchemy's
    alchemy_recent: int   # played-through tracks (Explore: or liked) added to alchemy
    artist_cap: int       # at most this many tracks by one artist in any 10
    seed_artist_cap: int  # the same for the seed's own artist
    artist_gap: int       # at least this many other tracks between two by one artist
    like_anchors: int     # likes that become anchors, at most
    skips_to_cool: int    # skips of one artist that cool it for STATION_COOL_MS
    drift: bool           # played-through tracks' neighbours are a source
    artists: bool         # similar artists' tracks are a source


STATION_TUNING: dict[str, _StationTuning] = {
    "close": _StationTuning(ceiling=150, ceiling_max=1000, alchemy_every=0,
                            temperature=0.0, alchemy_recent=0, artist_cap=2,
                            seed_artist_cap=3, artist_gap=3, like_anchors=2,
                            skips_to_cool=2, drift=False, artists=False),
    "balanced": _StationTuning(ceiling=400, ceiling_max=1000, alchemy_every=2,
                               temperature=0.3, alchemy_recent=3, artist_cap=2,
                               seed_artist_cap=2, artist_gap=4, like_anchors=4,
                               skips_to_cool=2, drift=False, artists=False),
    "explore": _StationTuning(ceiling=1000, ceiling_max=2000, alchemy_every=1,
                              temperature=0.8, alchemy_recent=5, artist_cap=1,
                              seed_artist_cap=1, artist_gap=8, like_anchors=6,
                              skips_to_cool=1, drift=True, artists=True),
}

STATION_ROUTES: dict[tuple[str, str], dict] = {
    # The probe and the shared catalogue: always 200, so both clients render the
    # same words whether or not this hub can compute a station.
    ("GET", "/sonic/station/characters"): {"method": "GET", "params": (), "probe": True},
    # Every body key a route reads is listed: HttpProxy drops the rest silently.
    ("POST", "/sonic/station/start"): {
        "method": "POST",
        "body": ("seed", "character", "count", "exclude", "bindSession", "deviceId"),
    },
    ("POST", "/sonic/station/next"): {
        "method": "POST",
        "body": ("stationId", "after", "count", "exclude", "deviceId"),
    },
    ("POST", "/sonic/station/signal"): {
        "method": "POST", "body": ("stationId", "trackId", "kind", "value"),
    },
}

_STATION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_STATION_SIGNALS = ("like", "unlike", "dislike", "skip", "played", "character")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _station_kv(raw: Any) -> list[tuple[str, float]]:
    """AudioMuse's `mood_vector` / `other_features`, in their order.

    A "k:v,k:v" STRING on the live server (measured 2026-10-16), an object on
    some builds. Anything unreadable is skipped rather than failing the row.
    """
    out: list[tuple[str, float]] = []
    if isinstance(raw, dict):
        items: Any = raw.items()
    elif isinstance(raw, str):
        items = (part.rsplit(":", 1) for part in raw.split(",") if ":" in part)
    else:
        return out
    for k, v in items:
        try:
            out.append((str(k).strip(), float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _station_features(raw: Any) -> Optional[tuple[float, ...]]:
    kv = dict(_station_kv(raw))
    if not all(f in kv for f in STATION_FEATURES):
        return None
    return tuple(kv[f] for f in STATION_FEATURES)


# An artist credit split into acts, the rule both clients already use for "same
# artist": `&`, `+`, `,`, `feat.`, `with`, `vs`, `x` — never `/` (AC/DC). The key
# is the first act, lowercased, punctuation dropped.
_ARTIST_SEP_RE = re.compile(
    r"\s*[,&+;]\s*|\s+(?:feat\.?|ft\.?|featuring|with|vs\.?|x)\s+", re.I)


def _station_artist_key(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        return ""
    first = _ARTIST_SEP_RE.split(name.strip(), maxsplit=1)[0]
    key = re.sub(r"[^\w]+", " ", first.lower()).strip()
    return key or name.strip().lower()


# A version of a song is the same song for "played in the last 14 days": the
# remaster, the live take, the one with a guest. Brackets go whole; a dash tail
# goes when it says remaster or live; a feat. tail goes; punctuation goes. A
# leading "Live" ("Live Forever") is the title and stays.
_TITLE_BRACKET_RE = re.compile(r"[(\[{（【][^)\]}）】]*[)\]}）】]")
_TITLE_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_TITLE_TAIL_RE = re.compile(r"\s+[-–—/|]\s+.*\b(?:remaster(?:ed)?|live)\b.*$", re.I)


def _station_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    t = title.lower()
    t = _TITLE_BRACKET_RE.sub(" ", t)
    t = _TITLE_FEAT_RE.sub("", t)
    t = _TITLE_TAIL_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or title.strip().lower()


def _station_title_key(title: Any, artist: Any) -> str:
    """Title + primary artist, hashed: what the history and a station's served set
    compare, and all they store of a title — a log or a copied state file then
    holds no song names."""
    raw = f"{_station_title(title)}\x1f{_station_artist_key(artist)}"
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=8).hexdigest()


def _station_row(raw: Any) -> Optional[dict]:
    """An AudioMuse row (`item_id`, `author`) or a Navidrome song (`id`, `artist`),
    reduced to what a station decides with."""
    if not isinstance(raw, dict):
        return None
    tid = raw.get("item_id") or raw.get("id")
    if not isinstance(tid, str) or not _STATION_ID_RE.match(tid):
        return None
    title = _as_str(raw.get("title")) or ""
    artist = _as_str(raw.get("author") or raw.get("artist")) or ""
    tags = _station_kv(raw.get("mood_vector"))
    try:
        dist: Optional[float] = float(raw["distance"])
    except (KeyError, TypeError, ValueError):
        dist = None
    return {
        "id": tid, "title": title, "artist": artist,
        "album": _as_str(raw.get("album")) or "",
        "feats": _station_features(raw.get("other_features")),
        "tag": (tags[0][0] if tags else
                (_as_str(raw.get("top_genre")) or _as_str(raw.get("genre")) or "")),
        "ak": _station_artist_key(artist), "tk": _station_title_key(title, artist),
        "dist": dist,
    }


def _station_change(a: Optional[Any], b: Optional[Any]) -> float:
    """Mean change over the six features; 1.0 (the most there is) when either
    side has none, so a row without features is placed last, never first."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _station_depth(ceiling: int) -> int:
    for d in STATION_LIST_DEPTHS:
        if ceiling <= d:
            return d
    return STATION_LIST_DEPTHS[-1]


def _write_text_atomic(path: str, payload: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


@dataclass
class Station:
    """One station. Everything here but the runtime sets is persisted in
    state.json["stations"] (PROTOCOL §18.4), so a restart keeps it."""
    id: str
    seed: dict                       # as answered: {kind, ids?, name?}
    name: str
    character: str
    created_at: int
    used_at: int
    engine: str = "tier2"
    seq: int = 0                     # tracks served, ever
    rev: int = 0                     # moved by every signal and character change
    exhausted: bool = False
    seed_ids: list = field(default_factory=list)    # never served
    # The anchors' own rows: a seed's (from AudioMuse's score or Navidrome's
    # getSong), or for Your mix the fingerprint's top rows, which ARE servable.
    # {id, title, artist, album, ak, tk, feats, mix}
    anchor_meta: list = field(default_factory=list)
    likes: list = field(default_factory=list)       # liked ids, oldest first
    served: list = field(default_factory=list)      # [id, tk, ak, hop], in order
    trail: list = field(default_factory=list)       # artist keys, oldest first
    last_feats: Optional[list] = None               # the last track's six features
    played: list = field(default_factory=list)      # played-through ids, newest last
    skips: list = field(default_factory=list)       # [id, at]: alchemy's subtract set
    artist_skips: dict = field(default_factory=dict)  # artist key -> [at, ...]
    cooldown: dict = field(default_factory=dict)    # artist key -> until (ms)
    cursors: dict = field(default_factory=dict)     # anchor id -> rows walked past
    batches: int = 0
    last_batch: Optional[dict] = None               # {device, after, tracks}
    # runtime only
    served_ids: set = field(default_factory=set, repr=False)
    served_keys: set = field(default_factory=set, repr=False)
    hops: dict = field(default_factory=dict, repr=False)
    announced: Optional[tuple] = field(default=None, repr=False)

    def index(self) -> None:
        self.served_ids = {r[0] for r in self.served}
        self.served_keys = {r[1] for r in self.served if r[1]}
        self.hops = {r[0]: r[3] for r in self.served}

    def base_anchors(self) -> list[str]:
        return [m["id"] for m in self.anchor_meta]

    def anchors(self) -> list[str]:
        """The seed's (or Your mix's) anchors, then the likes the character allows."""
        tune = STATION_TUNING[self.character]
        base = self.base_anchors()
        extra = [i for i in self.likes if i not in base][:tune.like_anchors]
        return base + extra

    def knows(self, track_id: str) -> bool:
        return (track_id in self.served_ids or track_id in self.seed_ids
                or any(m["id"] == track_id for m in self.anchor_meta))

    def track_key(self, track_id: str) -> str:
        for r in self.served:
            if r[0] == track_id:
                return r[1]
        for m in self.anchor_meta:
            if m["id"] == track_id:
                return m.get("tk") or ""
        return ""

    def artist_of(self, track_id: str) -> str:
        for r in reversed(self.served):
            if r[0] == track_id:
                return r[2]
        for m in self.anchor_meta:
            if m["id"] == track_id:
                return m.get("ak") or ""
        return ""

    def frame(self) -> dict:
        return {"t": "station", "stationId": self.id, "seq": self.seq, "rev": self.rev,
                "character": self.character, "exhausted": self.exhausted}

    def to_state(self) -> dict:
        return {
            "id": self.id, "seed": self.seed, "name": self.name,
            "character": self.character, "createdAt": self.created_at,
            "usedAt": self.used_at, "engine": self.engine, "seq": self.seq,
            "rev": self.rev, "exhausted": self.exhausted, "seedIds": self.seed_ids,
            "anchorMeta": self.anchor_meta, "likes": self.likes, "served": self.served,
            "trail": self.trail, "lastFeats": self.last_feats, "played": self.played,
            "skips": self.skips, "artistSkips": self.artist_skips,
            "cooldown": self.cooldown, "cursors": self.cursors, "batches": self.batches,
            "lastBatch": self.last_batch,
        }

    @classmethod
    def from_state(cls, d: Any) -> Optional["Station"]:
        """A persisted station, or None if it cannot be trusted. Every field is
        coerced: this is read back from a file a person may have edited."""
        if not isinstance(d, dict):
            return None
        sid = _as_str(d.get("id"), 64)
        character = d.get("character")
        seed = d.get("seed")
        if not sid or character not in STATION_CHARACTERS or not isinstance(seed, dict):
            return None

        def strs(v: Any, cap: int) -> list:
            return [x for x in (v if isinstance(v, list) else [])
                    if isinstance(x, str) and _STATION_ID_RE.match(x)][-cap:]

        meta = []
        for m in d.get("anchorMeta") or []:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                feats = m.get("feats")
                meta.append({
                    "id": m["id"], "title": _as_str(m.get("title")) or "",
                    "artist": _as_str(m.get("artist")) or "",
                    "album": _as_str(m.get("album")) or "",
                    "ak": _as_str(m.get("ak")) or "", "tk": _as_str(m.get("tk"), 32) or "",
                    "feats": list(feats) if isinstance(feats, list)
                    and len(feats) == len(STATION_FEATURES) else None,
                    "mix": bool(m.get("mix")),
                })
        served = []
        for r in d.get("served") or []:
            if (isinstance(r, list) and len(r) == 4 and isinstance(r[0], str)
                    and isinstance(r[1], str) and isinstance(r[2], str)):
                served.append([r[0], r[1], r[2], 2 if r[3] == 2 else 1])
        last = d.get("lastBatch")
        if not (isinstance(last, dict) and isinstance(last.get("tracks"), list)):
            last = None
        feats = d.get("lastFeats")
        st = cls(
            id=sid, seed=seed, name=_as_str(d.get("name")) or "Radio",
            character=character, created_at=_as_int(d.get("createdAt"), _now_ms(), 0),
            used_at=_as_int(d.get("usedAt"), _now_ms(), 0),
            engine="tier1" if d.get("engine") == "tier1" else "tier2",
            seq=_as_int(d.get("seq"), 0, 0), rev=_as_int(d.get("rev"), 0, 0),
            exhausted=bool(d.get("exhausted")),
            seed_ids=strs(d.get("seedIds"), STATION_SEEDS_MAX), anchor_meta=meta,
            likes=strs(d.get("likes"), 50), served=served[-STATION_SERVED_MAX:],
            trail=[a for a in (d.get("trail") or []) if isinstance(a, str)][-STATION_TRAIL_MAX:],
            last_feats=list(feats) if isinstance(feats, list)
            and len(feats) == len(STATION_FEATURES) else None,
            played=strs(d.get("played"), 20),
            skips=[s for s in (d.get("skips") or []) if isinstance(s, list) and len(s) == 2
                   and isinstance(s[0], str)][-40:],
            artist_skips={k: [int(t) for t in v if isinstance(t, (int, float))]
                          for k, v in (d.get("artistSkips") or {}).items()
                          if isinstance(k, str) and isinstance(v, list)},
            cooldown={k: int(v) for k, v in (d.get("cooldown") or {}).items()
                      if isinstance(k, str) and isinstance(v, (int, float))},
            cursors={k: _as_int(v, 0, 0) for k, v in (d.get("cursors") or {}).items()
                     if isinstance(k, str)},
            batches=_as_int(d.get("batches"), 0, 0), last_batch=last,
        )
        st.index()
        return st


class _StationBatch:
    """One request's working state: its deadline, its share of the upstream calls,
    what failed, and the lists it has fetched (so the ladder asks for each once)."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.sem = asyncio.Semaphore(STATION_CALLS_PER_REQUEST)
        self.failed = False        # a source failed: the answer is partial
        self.timed_out = False     # the budget ran out: the answer is partial
        self.lists: dict[tuple[str, int], Optional[list]] = {}
        self.t1_lists: dict[tuple[str, int], Optional[list]] = {}
        self.unknown: set[str] = set()   # ids a tier said it does not hold
        self.found: set[str] = set()     # ids a tier answered rows for
        self.alchemy: Optional[list] = None
        self.alchemy_done = False
        self.artist_ids: Optional[list] = None
        self.tier2_failed = False        # every anchor list failed at AudioMuse
        self.tier1_failed = False
        self.engine = "tier2"
        self.rungs: list[str] = []
        # What `_compute` chose, for `_commit` to write into the station.
        self.picked_meta: list[tuple[dict, int]] = []
        self.trail: Optional[list] = None
        self.prev: Optional[list] = None
        self.levels_done = False   # every rung of the ladder ran

    def left(self) -> float:
        return self.deadline - time.monotonic()


class StationEngine:
    """The stations, their play history, and the two tiers they are computed from.

    Owned by the Hub (it reads the session and broadcasts through it); reached by
    clients only through StationProxy.
    """

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.stations: dict[str, Station] = {}
        # The play history: [id, title key, at ms], oldest first. Ids and hashed
        # keys only, never in health, welcome or a log line (§18.4).
        self.history: list[list] = []
        self._history_path = STATE_PATH + STATION_HISTORY_SUFFIX
        self._history_dirty = False
        self._history_task: Optional[asyncio.Task] = None
        self._history_written_at = -STATION_HISTORY_FLUSH_S
        self._locks: dict[str, asyncio.Lock] = {}
        self._computing = asyncio.Semaphore(STATION_COMPUTING_MAX)
        self._nd_sem = asyncio.Semaphore(STATION_CALLS_PER_REQUEST)
        # The tiers' last verdicts (None = never asked) and whether Navidrome
        # offers AudioMuse's sonic similarity (`getSonicSimilarTracks`).
        self.tier2_ok: Optional[bool] = None
        self.tier1_ok: Optional[bool] = None
        self.sonic: Optional[bool] = None
        self._probed = asyncio.Event()
        self._probe_at = 0.0
        self._probe_soon: Optional[asyncio.TimerHandle] = None
        self._probe_lock = asyncio.Lock()
        self._t1_cache: dict[tuple[str, str, int], tuple[float, list]] = {}
        self._bg: set = set()
        self._load_history()

    # ----- configuration ---------------------------------------------------- #
    @staticmethod
    def tier1_configured() -> bool:
        return bool(NAVIDROME_URL and ND_USER and ND_PASS)

    @staticmethod
    def tier2_configured() -> bool:
        return bool(AUDIOMUSE_URL)

    def engine_name(self) -> Optional[str]:
        if self.tier2_configured() and self.tier2_ok:
            return "tier2"
        if self.tier1_configured() and self.tier1_ok:
            return "tier1"
        return None

    # ----- persistence ------------------------------------------------------ #
    def load_state(self, rows: Any) -> None:
        """Boot: the persisted stations, minus the expired, the newest STATION_MAX.
        Runs before the session is validated against them, so it drops nothing
        through `_drop` (which would announce an unbinding nobody can hear)."""
        now = _now_ms()
        loaded = []
        for raw in rows if isinstance(rows, list) else []:
            st = Station.from_state(raw)
            if st is not None and now - st.used_at < STATION_IDLE_MS:
                loaded.append(st)
        loaded.sort(key=lambda st: st.used_at, reverse=True)
        self.stations = {st.id: st for st in loaded[:STATION_MAX]}

    def to_state(self) -> list[dict]:
        return [st.to_state() for st in self.stations.values()]

    def _load_history(self) -> None:
        if not STATION_HISTORY_ON:
            return
        status, data = _read_state_file(self._history_path)
        if status != "ok":
            if status == "corrupt":
                _set_aside_state_file(self._history_path)
            return
        plays = data.get("plays")
        for p in plays if isinstance(plays, list) else []:
            if (isinstance(p, list) and len(p) == 3 and isinstance(p[0], str)
                    and isinstance(p[1], str) and isinstance(p[2], (int, float))):
                self.history.append([p[0], p[1], int(p[2])])
        self._prune_history()
        log(f"station history: {len(self.history)} plays loaded")

    def _prune_history(self) -> None:
        cutoff = _now_ms() - STATION_HISTORY_DAYS * 86_400_000
        self.history = [p for p in self.history if p[2] >= cutoff][-STATION_HISTORY_MAX:]

    def _history_add(self, track_id: str, key: str) -> None:
        now = _now_ms()
        for p in reversed(self.history[-50:]):
            if p[0] == track_id and now - p[2] < STATION_HISTORY_REPEAT_MS:
                p[2] = now     # the same play, heard again or re-judged
                break
        else:
            self.history.append([track_id, key, now])
        self._prune_history()
        self._history_touch()

    def _history_sets(self, days: int, now: int) -> tuple[set, set]:
        cutoff = now - days * 86_400_000
        ids, keys = set(), set()
        for tid, key, at in self.history:
            if at >= cutoff:
                ids.add(tid)
                if key:
                    keys.add(key)
        return ids, keys

    def _history_touch(self) -> None:
        if not STATION_HISTORY_ON:
            return
        self._history_dirty = True
        if self._history_task is not None and not self._history_task.done():
            return
        try:
            self._history_task = asyncio.get_running_loop().create_task(
                self._history_writer())
        except RuntimeError:
            self._write_history_now()  # no loop: a tool or a test driving the engine

    async def _history_writer(self) -> None:
        try:
            while self._history_dirty:
                wait = self._history_written_at + STATION_HISTORY_FLUSH_S - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._history_dirty = False
                payload = json.dumps({"v": 1, "plays": self.history})
                try:
                    await asyncio.to_thread(_write_text_atomic, self._history_path, payload)
                except Exception as e:  # noqa: BLE001 — a lost write is retried on the next play
                    log("station history: write failed:", e)
                self._history_written_at = time.monotonic()
        finally:
            self._history_task = None

    def _write_history_now(self) -> None:
        if not STATION_HISTORY_ON:
            return
        self._history_dirty = False
        try:
            _write_text_atomic(self._history_path, json.dumps({"v": 1, "plays": self.history}))
        except Exception as e:  # noqa: BLE001
            log("station history: write failed:", e)

    def close(self) -> None:
        """Graceful stop: the history is written now, not up to 30 s from now."""
        if self._history_task is not None and not self._history_task.done():
            self._history_task.cancel()
            self._history_dirty = True
        if self._history_dirty:
            self._write_history_now()
        if self._probe_soon is not None:
            self._probe_soon.cancel()

    # ----- the station set ------------------------------------------------- #
    def _mint_id(self) -> str:
        return f"st_{_now_ms()}_{os.urandom(2).hex()}"

    def _lock(self, sid: str) -> asyncio.Lock:
        lock = self._locks.get(sid)
        if lock is None:
            lock = self._locks[sid] = asyncio.Lock()
        return lock

    def get(self, sid: Any) -> Optional[Station]:
        self._sweep()
        return self.stations.get(sid) if isinstance(sid, str) else None

    def bound(self) -> Optional[Station]:
        sid = self.hub.session.station_id
        return self.stations.get(sid) if sid else None

    def _drop(self, sid: str, why: str) -> None:
        self.stations.pop(sid, None)
        lock = self._locks.get(sid)
        if lock is not None and not lock.locked():
            self._locks.pop(sid, None)
        dlog(f"station {sid} dropped ({why})")
        if self.hub.session.station_id == sid:
            # The queue it belonged to no longer continues as anything.
            self.hub.session.station_id = None
            self.hub.session.bump()
            self.hub._spawn(self.hub._broadcast_session())  # noqa: SLF001 — same module

    def _sweep(self) -> None:
        now = _now_ms()
        for sid, st in list(self.stations.items()):
            if now - st.used_at >= STATION_IDLE_MS and not self._lock(sid).locked():
                self._drop(sid, "idle")

    def _evict(self) -> None:
        if len(self.stations) <= STATION_MAX:
            return
        bound = self.hub.session.station_id
        victims = sorted((st for st in self.stations.values() if st.id != bound),
                         key=lambda st: st.used_at)
        for st in victims[:len(self.stations) - STATION_MAX]:
            self._drop(st.id, "evicted")

    def welcome_station(self) -> Optional[dict]:
        st = self.bound()
        if st is None:
            return None
        frame = st.frame()
        frame.pop("t")
        frame.update(name=st.name, seed=st.seed)
        return frame

    async def _announce(self, st: Station) -> None:
        sig = (st.seq, st.rev, st.character, st.exhausted)
        if st.announced == sig or st.id not in self.stations:
            return
        st.announced = sig
        await self.hub._broadcast(st.frame())  # noqa: SLF001 — same module

    @staticmethod
    async def _acquire(lock: Any, deadline: float) -> bool:
        """Wait for a lock or a slot, but not past the request's budget, which runs
        from the request's arrival: two stations computing must not make a third
        request outlast the clients' own timeout (Feishin 15 s) while it queues."""
        try:
            await asyncio.wait_for(lock.acquire(), max(0.0, deadline - time.monotonic()))
            return True
        except asyncio.TimeoutError:
            return False

    def _bump(self, st: Station) -> None:
        st.rev += 1
        st.exhausted = False   # a signal or a new character: the answer may differ
        st.used_at = _now_ms()

    # ----- the two tiers --------------------------------------------------- #
    def _note_tier2(self, ok: bool) -> None:
        self.tier2_ok = ok
        if not ok:
            self._schedule_probe()

    def _note_tier1(self, ok: bool) -> None:
        self.tier1_ok = ok
        if not ok:
            self._schedule_probe()

    def _schedule_probe(self) -> None:
        if self._probe_soon is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def fire() -> None:
            self._probe_soon = None
            self.hub._spawn(self.probe())  # noqa: SLF001 — same module
        self._probe_soon = loop.call_later(STATION_PROBE_RETRY, fire)

    async def _await_bounded(self, coro: Any, b: Optional[_StationBatch]) -> Optional[Any]:
        """Run one upstream call inside the request's budget. A call still running
        when the budget is spent is NOT cancelled: it finishes in the background
        and lands in SONIC's cache, where the next batch finds it, and a cancel
        would also fail every other request coalesced onto it."""
        task = asyncio.ensure_future(coro)
        if b is None:
            return await task
        left = b.left()
        if left <= 0:
            task.cancel()
            b.timed_out = True
            return None
        done, _pending = await asyncio.wait({task}, timeout=left)
        if not done:
            b.timed_out = True
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            return None
        return task.result()

    async def _am(self, name: str, params: Optional[dict], body: Optional[dict],
                  b: Optional[_StationBatch]) -> tuple[int, Any]:
        """One AudioMuse call through SONIC. Returns (status, parsed JSON or None);
        status 0 = the budget ran out first."""
        route, spec = SONIC_INTERNAL[name]
        query = urllib.parse.urlencode(params or {})
        plist = HttpProxy._filtered_params(spec, query)  # adds the nd creds where the spec says
        async with (b.sem if b is not None else _NullAsync()):
            result = await self._await_bounded(SONIC.call(route, spec, plist, body), b)
        if result is None:
            return 0, None
        status, raw, _ctype = result
        if status >= 500:
            # A failed source either way. Only the hub's own "could not reach it"
            # (502 down, 503/504 busy) says AudioMuse is unavailable; a 500 from
            # AudioMuse itself is AudioMuse answering, about one route.
            if b is not None:
                b.failed = True
            try:
                own = json.loads(raw or b"{}")
            except Exception:  # noqa: BLE001
                own = {}
            self._note_tier2(not (isinstance(own, dict)
                                  and (own.get("down") or own.get("busy"))))
            return status, None
        self._note_tier2(True)
        if status != 200:
            return status, None
        try:
            return status, json.loads(raw or b"null")
        except Exception:  # noqa: BLE001 — a body that is not JSON is a failed source
            if b is not None:
                b.failed = True
            return 502, None

    async def _nd(self, method: str, params: dict,
                  b: Optional[_StationBatch]) -> tuple[str, Optional[dict]]:
        """One Navidrome call: ("ok", payload) | ("missing", None) | ("failed", None).
        Never `getRandomSongs`: no path in the engine fills with random songs."""
        if not self.tier1_configured():
            return "failed", None
        q = {**_nd_auth_params(), **params}
        url = f"{NAVIDROME_URL}/rest/{method}?" + urllib.parse.urlencode(q)
        async with (b.sem if b is not None else _NullAsync()):
            async with self._nd_sem:
                result = await self._await_bounded(asyncio.to_thread(
                    _proxy_upstream_blocking, "GET", url, None, "",
                    STATION_CALL_TIMEOUT, "navidrome"), b)
        if result is None:
            return "failed", None
        status, raw, _ctype = result
        resp = None
        if status == 200:
            try:
                resp = (json.loads(raw) or {}).get("subsonic-response")
            except Exception:  # noqa: BLE001
                resp = None
        if not isinstance(resp, dict):
            self._note_tier1(False)
            if b is not None:
                b.failed = True
            return "failed", None
        self._note_tier1(True)
        if resp.get("status") == "ok":
            return "ok", resp
        err = resp.get("error") or {}
        if isinstance(err, dict) and err.get("code") == 70:   # "data not found"
            return "missing", None
        if b is not None:
            b.failed = True
        return "failed", None

    async def probe(self) -> None:
        """Ask each configured tier whether it answers. Any answer below 500 is
        one: a probe id AudioMuse does not hold is answered 404, which is AudioMuse
        answering."""
        async with self._probe_lock:
            self._probe_at = time.monotonic()
            if self.tier2_configured():
                # An id AudioMuse cannot hold: answered 404 at once, and on a route
                # with no 404 fallback, so one probe is one call.
                status, _ = await self._am("similar_tracks", {
                    "item_id": "station-probe", "n": 1, "radius_similarity": "false",
                    "eliminate_duplicates": "false"}, None, None)
                self.tier2_ok = 0 < status < 500
            if self.tier1_configured():
                outcome, payload = await self._nd("getOpenSubsonicExtensions", {}, None)
                self.tier1_ok = outcome == "ok"
                if outcome == "ok":
                    names = [e.get("name") for e in payload.get("openSubsonicExtensions") or []
                             if isinstance(e, dict)]
                    self.sonic = "sonicSimilarity" in names
            self._probed.set()

    async def probe_loop(self) -> None:
        while True:
            try:
                await self.probe()
            except Exception as e:  # noqa: BLE001 — a probe must never kill its loop
                log("station probe failed:", e)
                self._probed.set()
            await asyncio.sleep(STATION_PROBE_INTERVAL)

    # ----- the lists -------------------------------------------------------- #
    async def _nearest(self, item_id: str, depth: int, b: _StationBatch) -> Optional[list]:
        """An item's nearest list at `depth`: rows nearest first. None = the call
        failed; [] = AudioMuse holds no such item (or has nothing near it)."""
        for (iid, d), rows in b.lists.items():   # a deeper list already in hand
            if iid == item_id and d >= depth and rows is not None:
                return rows[:depth]
        key = (item_id, depth)
        if key in b.lists:
            return b.lists[key]
        status, data = await self._am("similar_tracks", {
            "item_id": item_id, "n": depth, "radius_similarity": "false",
            "eliminate_duplicates": "false"}, None, b)
        rows: Optional[list]
        if status == 200 and isinstance(data, list):
            rows = [r for r in (_station_row(x) for x in data) if r is not None]
            b.found.add(item_id)
        elif status in (400, 404):
            rows = []
            b.unknown.add(item_id)
        else:
            rows = None
        b.lists[key] = rows
        return rows

    async def _tier1_list(self, item_id: str, n: int, b: _StationBatch) -> Optional[list]:
        n = max(1, min(n, STATION_TIER1_MAX))
        key = (item_id, n)
        if key in b.t1_lists:
            return b.t1_lists[key]
        if self.sonic is None:
            outcome, payload = await self._nd("getOpenSubsonicExtensions", {}, b)
            if outcome == "ok":
                self.sonic = "sonicSimilarity" in [
                    e.get("name") for e in payload.get("openSubsonicExtensions") or []
                    if isinstance(e, dict)]
        method = "getSonicSimilarTracks" if self.sonic else "getSimilarSongs2"
        ck = (method, item_id, n)
        hit = self._t1_cache.get(ck)
        if hit is not None and hit[0] > time.monotonic():
            b.t1_lists[key] = hit[1]
            b.found.add(item_id)
            return hit[1]
        outcome, payload = await self._nd(method, {"id": item_id, "count": n}, b)
        rows: Optional[list]
        if outcome == "ok":
            if self.sonic:
                entries = [m.get("entry") for m in payload.get("sonicMatch") or []
                           if isinstance(m, dict)]
            else:
                entries = (payload.get("similarSongs2") or {}).get("song") or []
            rows = [r for r in (_station_row(x) for x in entries) if r is not None]
            b.found.add(item_id)
            if len(self._t1_cache) >= 64:
                for old in sorted(self._t1_cache, key=lambda k: self._t1_cache[k][0])[:16]:
                    del self._t1_cache[old]
            self._t1_cache[ck] = (time.monotonic() + STATION_LIST_TTL, rows)
        elif outcome == "missing":
            rows = []
            b.unknown.add(item_id)
        else:
            rows = None
            b.tier1_failed = True
        b.t1_lists[key] = rows
        return rows

    async def _gather(self, coros: list) -> list:
        """Run calls together; each already takes a slot of the request's two."""
        return list(await asyncio.gather(*coros)) if coros else []

    # ----- candidates ------------------------------------------------------- #
    @staticmethod
    def _round_robin(lists: list[list]) -> list:
        out, i = [], 0
        while True:
            moved = False
            for lst in lists:
                if i < len(lst):
                    out.append(lst[i])
                    moved = True
            if not moved:
                return out
            i += 1

    @staticmethod
    def _interleave(sources: list[list]) -> list:
        """One stream from several sources, one from each in turn, first sighting
        of an id kept."""
        seen, out = set(), []
        for c in StationEngine._round_robin(sources):
            if c[0]["id"] not in seen:
                seen.add(c[0]["id"])
                out.append(c)
        return out

    async def _tier2_stream(self, st: Station, tune: _StationTuning, ceiling: int,
                            b: _StationBatch) -> Optional[list]:
        """Every candidate the gate admits at this ceiling, best first, as
        (row, why, rank, hop). None = every anchor list failed."""
        depth = _station_depth(ceiling)
        anchors = st.anchors()
        lists = await self._gather([self._nearest(a, depth, b) for a in anchors])
        answered = [(a, rows) for a, rows in zip(anchors, lists) if rows is not None]
        if not answered:
            return None
        gate: dict[str, tuple[int, dict]] = {}
        near_lists = []
        for a, rows in answered:
            start = min(st.cursors.get(a, 0), len(rows))
            near = []
            for i, r in enumerate(rows[:ceiling]):
                rank = i + 1
                if r["id"] not in gate or rank < gate[r["id"]][0]:
                    gate[r["id"]] = (rank, r)
                if i >= start:
                    near.append((r, "near", rank, 1))
            near_lists.append(near)
        near_stream = self._round_robin(near_lists)
        # Your mix: the fingerprint's own top rows are tracks to play, first.
        mix = [({**m, "dist": 0.0}, "near", 0, 1) for m in st.anchor_meta if m.get("mix")]
        sources: list[list] = [mix + near_stream]

        drift_gate: dict[str, tuple[int, dict]] = {}
        drift_stream: list = []
        if tune.drift:
            src = [p for p in reversed(st.played) if st.hops.get(p) == 1]
            src = src[:STATION_DRIFT_SOURCES]
            dl = await self._gather([self._nearest(p, STATION_DRIFT_CEILING, b) for p in src])
            per = []
            for rows in dl:
                one = []
                for i, r in enumerate((rows or [])[:STATION_DRIFT_CEILING]):
                    if r["id"] not in drift_gate or i + 1 < drift_gate[r["id"]][0]:
                        drift_gate[r["id"]] = (i + 1, r)
                    one.append((r, "drift", i + 1, 2))
                per.append(one)
            drift_stream = self._round_robin(per)

        def admit(tid: str, why: str) -> Optional[tuple]:
            if tid in gate:
                rank, row = gate[tid]
                return (row, why, rank, 1)
            if tid in drift_gate:
                rank, row = drift_gate[tid]
                return (row, why, rank, 2)
            return None

        use_alchemy = tune.alchemy_every == 1 or (
            tune.alchemy_every == 2 and st.batches % 2 == 1)
        if use_alchemy and not b.alchemy_done:
            b.alchemy_done = True
            b.alchemy = await self._alchemy(st, tune, b)
        blend = []
        for r in b.alchemy or []:
            c = admit(r["id"], "blend")
            if c is not None:
                blend.append(c)
        if blend:
            sources.append(blend)
        if drift_stream:
            sources.append(drift_stream)
        if tune.artists:
            if b.artist_ids is None:
                b.artist_ids = await self._similar_artist_ids(st, b)
            arts = [c for c in (admit(t, "artist") for t in b.artist_ids) if c is not None]
            if arts:
                sources.append(arts)
        return self._interleave(sources)

    async def _alchemy(self, st: Station, tune: _StationTuning,
                       b: _StationBatch) -> list:
        recent = [p for p in reversed(st.played)]
        if st.character == "explore":
            recent = [i for i in reversed(st.likes)] + recent
        adds, seen = [], set()
        for i in st.anchors() + recent[:tune.alchemy_recent]:
            if i not in seen:
                seen.add(i)
                adds.append({"op": "add", "id": i, "type": "song"})
        now = _now_ms()
        subs = [{"op": "subtract", "id": s[0], "type": "song"} for s in st.skips
                if now - _as_int(s[1]) < STATION_SKIP_TTL_MS and s[0] not in seen][-10:]
        status, data = await self._am("alchemy", None, {
            "items": adds + subs, "n": STATION_ALCHEMY_N,
            "temperature": tune.temperature}, b)
        rows = (data or {}).get("results") if isinstance(data, dict) else None
        return [r for r in (_station_row(x) for x in rows or []) if r is not None]

    async def _similar_artist_ids(self, st: Station, b: _StationBatch) -> list:
        names = []
        for m in st.anchor_meta:
            name = m.get("artist") or ""
            if name and name not in names:
                names.append(name)
        out: list[str] = []
        for name in names[:STATION_SEED_ARTISTS]:
            status, data = await self._am("similar_artists", {
                "artist": name, "n": STATION_SIMILAR_ARTISTS_N,
                "include_component_matches": "true"}, None, b)
            for a in data if isinstance(data, list) else []:
                for cm in (a.get("component_matches") or []) if isinstance(a, dict) else []:
                    for song in (cm.get("artist2_representative_songs") or []) \
                            if isinstance(cm, dict) else []:
                        tid = song.get("item_id") if isinstance(song, dict) else None
                        if isinstance(tid, str) and tid not in out:
                            out.append(tid)
        return out

    async def _tier1_stream(self, st: Station, ceiling: int,
                            b: _StationBatch) -> Optional[list]:
        """Navidrome's similar songs per anchor, gated by rank alone (no features
        to compare): None = every anchor failed."""
        n = min(ceiling, STATION_TIER1_MAX)
        anchors = st.anchors()
        lists = await self._gather([self._tier1_list(a, n, b) for a in anchors])
        answered = [rows for rows in lists if rows is not None]
        if not answered:
            return None
        per = [[(r, "tier1", i + 1, 1) for i, r in enumerate(rows[:n])] for rows in answered]
        mix = [({**m, "dist": 0.0}, "tier1", 0, 1) for m in st.anchor_meta if m.get("mix")]
        return self._interleave([mix + self._round_robin(per)])

    # ----- one batch --------------------------------------------------------- #
    def _levels(self, st: Station, tier1_primary: bool) -> list[tuple[str, int, int]]:
        """The widening ladder (§18.3) as (rung, ceiling, history days). The first
        is the lists walked to the character's ceiling; then the ceiling ×1.5 up to
        its maximum; then the history window down to 3 days; then Tier 1. The
        played-through-neighbours and similar-artists rungs are Explore's sources
        from the first rung on; on Close and Balanced they could only offer what
        the anchor gate already holds, so they add no rung of their own."""
        tune = STATION_TUNING[st.character]
        top = tune.ceiling_max if not tier1_primary else min(tune.ceiling_max,
                                                             STATION_TIER1_MAX)
        ceiling = min(tune.ceiling, top)
        out = [("lists", ceiling, STATION_HISTORY_DAYS)]
        while ceiling < top:
            ceiling = min(top, int(math.ceil(ceiling * STATION_WIDEN)))
            out.append((f"ceiling {ceiling}", ceiling, STATION_HISTORY_DAYS))
        out.append(("history 3d", ceiling, STATION_HISTORY_RELAXED_DAYS))
        if not tier1_primary and self.tier1_configured():
            out.append(("tier1", ceiling, STATION_HISTORY_RELAXED_DAYS))
        return out

    async def _compute(self, st: Station, count: int, exclude: set,
                       b: _StationBatch, debug: bool = False) -> list[dict]:
        """Choose up to `count` tracks. Writes nothing to the station: `_commit`
        does, once the request is sure to be answered."""
        tune = STATION_TUNING[st.character]
        now = _now_ms()
        hard_ids = set(exclude) | st.served_ids | set(st.seed_ids) | self._bound_queue_ids(st)
        seed_keys = {m["tk"] for m in st.anchor_meta if not m.get("mix") and m.get("tk")}
        hard_keys = st.served_keys | seed_keys
        cooled = {ak for ak, until in st.cooldown.items() if until > now}
        seed_aks = {m["ak"] for m in st.anchor_meta if m.get("ak") and not m.get("mix")}
        trail = list(st.trail)
        prev = st.last_feats
        picked: list[tuple] = []
        picked_ids: set = set()
        picked_keys: set = set()

        def artist_ok(ak: str) -> bool:
            if not ak:
                return True
            if ak in trail[-tune.artist_gap:]:
                return False
            cap = tune.seed_artist_cap if ak in seed_aks else tune.artist_cap
            return trail[-9:].count(ak) < cap

        # AudioMuse unset, or known unreachable since its last call: Navidrome
        # answers this batch. The latter is a failed source, so `partial`; the
        # re-probe that follows a failure brings Tier 2 back.
        tier1_primary = not self.tier2_configured() or (
            self.tier2_ok is False and self.tier1_configured())
        if self.tier2_configured() and tier1_primary:
            b.failed = True
            b.tier2_failed = True
        b.engine = "tier1" if tier1_primary else "tier2"
        levels = self._levels(st, tier1_primary)
        li = 0
        while li < len(levels) and len(picked) < count and b.left() > 0:
            rung, ceiling, days = levels[li]
            li += 1
            tier1 = rung == "tier1" or tier1_primary
            if not tier1 and li > 1 and all(a in b.unknown for a in st.anchors()):
                continue  # AudioMuse holds none of the anchors: deeper asks the same
            b.rungs.append(rung)
            if tier1:
                stream = await self._tier1_stream(st, ceiling, b)
            else:
                stream = await self._tier2_stream(st, tune, ceiling, b)
            if stream is None:
                b.failed = True
                if tier1:
                    b.tier1_failed = True
                    if tier1_primary:
                        break   # nothing is left to ask
                    continue
                if li == 1:
                    # Every anchor list failed at AudioMuse: this batch comes from
                    # Navidrome, and the answer says so (engine tier1, partial).
                    b.tier2_failed = True
                    if not self.tier1_configured():
                        break
                    tier1_primary = True
                    b.engine = "tier1"
                    levels, li = self._levels(st, True), 0
                continue
            hist_ids, hist_keys = self._history_sets(days, now)

            def passes(c: tuple) -> bool:
                r = c[0]
                return (r["id"] not in hard_ids and r["id"] not in picked_ids
                        and r["id"] not in hist_ids
                        and not (r["tk"] and (r["tk"] in hard_keys or r["tk"] in picked_keys
                                              or r["tk"] in hist_keys))
                        and r["ak"] not in cooled)

            def take(c: tuple) -> None:
                nonlocal prev
                r = c[0]
                picked.append(c + (rung,))
                picked_ids.add(r["id"])
                if r["tk"]:
                    picked_keys.add(r["tk"])
                trail.append(r["ak"])
                if r["feats"]:
                    prev = list(r["feats"])

            eligible = (c for c in stream if passes(c))
            if st.character == "close":
                # Nearest first: the stream's own order, a track held back only
                # until its artist is far enough behind.
                waiting: list[tuple] = []
                while len(picked) < count:
                    ready = next((w for w in waiting if artist_ok(w[0]["ak"])), None)
                    if ready is not None:
                        waiting.remove(ready)
                        take(ready)
                        continue
                    c = next(eligible, None)
                    if c is None:
                        break
                    if artist_ok(c[0]["ak"]):
                        take(c)
                    else:
                        waiting.append(c)
            else:
                # Smooth: among the next few the gate admits, the one whose six
                # features change least from the track before it.
                window: list[tuple] = []
                spent = False
                width = max(6, 2 * (count - len(picked)))
                while len(picked) < count:
                    while not spent and len(window) < width:
                        c = next(eligible, None)
                        if c is None:
                            spent = True
                        else:
                            window.append(c)
                    ok = [c for c in window if artist_ok(c[0]["ak"])]
                    if not ok:
                        if spent or width > 400:
                            break
                        width += 8
                        continue
                    # Your mix's own top rows (rank 0) lead, as a radio's seed does.
                    best = next((c for c in ok if c[2] == 0), None) or min(
                        ok, key=lambda c: _station_change(prev, c[0]["feats"]))
                    window.remove(best)
                    take(best)
        out = []
        for row, why, rank, hop, rung in picked:
            t = {"id": row["id"], "title": row["title"], "artist": row["artist"],
                 "album": row["album"], "why": why, "rank": rank}
            if debug:
                t["_debug"] = {"tag": row.get("tag"), "hop": hop, "rung": rung,
                               "dist": row.get("dist"), "ak": row["ak"], "tk": row["tk"],
                               "feats": row.get("feats")}
            out.append(t)
        b.picked_meta = [(c[0], c[3]) for c in picked]
        b.trail = trail
        b.prev = prev
        b.levels_done = li >= len(levels)
        return out

    def _bound_queue_ids(self, st: Station) -> set:
        s = self.hub.session
        if s.station_id != st.id:
            return set()
        return {t.get("id") for t in s.queue if isinstance(t, dict) and t.get("id")}

    def _commit(self, st: Station, tracks: list[dict], b: _StationBatch,
                device: Optional[str], after: int, rev_at_start: int) -> tuple[bool, bool]:
        """Write a computed batch into the station. Returns (exhausted, partial)."""
        for row, hop in b.picked_meta:
            st.served.append([row["id"], row["tk"], row["ak"], hop])
        st.served = st.served[-STATION_SERVED_MAX:]
        st.index()
        if b.trail is not None:
            st.trail = b.trail[-STATION_TRAIL_MAX:]
        if b.prev:
            st.last_feats = list(b.prev)
        st.seq += len(tracks)
        st.batches += 1
        st.engine = b.engine
        st.used_at = _now_ms()
        partial = b.failed or b.timed_out
        # `exhausted` is an answer, not a failure: every rung answered and none had
        # anything left that fits. A rung that failed, or a budget that ran out, is
        # `partial` and never this.
        exhausted = not tracks and not partial and b.levels_done
        if st.rev == rev_at_start:
            st.exhausted = exhausted
        st.last_batch = {"device": device, "after": after, "tracks": tracks}
        # The cursors: past every row at the front of an anchor's list that can
        # never be served again (served, or a seed), so the next walk starts there.
        done = st.served_ids | set(st.seed_ids)
        for (iid, _depth), rows in b.lists.items():
            if rows is None or iid not in st.anchors():
                continue
            i = st.cursors.get(iid, 0)
            while i < len(rows) and rows[i]["id"] in done:
                i += 1
            st.cursors[iid] = i
        return exhausted, partial

    # ----- routes ------------------------------------------------------------ #
    async def route(self, path: str, body: dict) -> tuple[int, dict]:
        if path == "/sonic/station/characters":
            return 200, await self.characters()
        if path == "/sonic/station/start":
            return await self.start(body)
        if path == "/sonic/station/next":
            return await self.next(body)
        if path == "/sonic/station/signal":
            return await self.signal(body)
        return 404, {"error": "unknown route"}

    async def characters(self) -> dict:
        if not self._probed.is_set():
            try:
                await asyncio.wait_for(self._probed.wait(), STATION_PROBE_STARTUP_WAIT)
            except asyncio.TimeoutError:
                pass
        engine = self.engine_name()
        return {"configured": True, "available": engine is not None, "engine": engine,
                "default": STATION_DEFAULT_CHARACTER,
                "characters": [dict(c) for c in STATION_CATALOGUE]}

    @staticmethod
    def _parse_seed(raw: Any) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        if kind == "yourMix":
            return {"kind": "yourMix"}
        if kind not in ("track", "tracks"):
            return None
        ids = raw.get("ids")
        if not isinstance(ids, list):
            return None
        clean = []
        for i in ids:
            if not isinstance(i, str) or not _STATION_ID_RE.match(i):
                return None
            if i not in clean:
                clean.append(i)
        if not clean or len(clean) > (1 if kind == "track" else STATION_SEEDS_MAX):
            return None
        seed = {"kind": kind, "ids": clean}
        name = _as_str(raw.get("name"), 200)
        if name and name.strip():
            seed["name"] = name.strip()
        return seed

    @staticmethod
    def _parse_exclude(raw: Any) -> set:
        if not isinstance(raw, list):
            return set()
        return {i for i in raw[:STATION_EXCLUDE_MAX] if isinstance(i, str)}

    async def _seed_meta(self, ids: list, b: _StationBatch) -> list[dict]:
        """Each seed's own row, for its title key, its artist and its features."""
        async def one(i: str) -> dict:
            row = None
            if self.tier2_configured() and self.tier2_ok is not False:
                status, data = await self._am("score", {"id": i}, None, b)
                row = _station_row(data) if status == 200 else None
            if row is None and self.tier1_configured() and (
                    not self.tier2_configured() or self.tier2_ok is False):
                outcome, payload = await self._nd("getSong", {"id": i}, b)
                if outcome == "ok":
                    row = _station_row(payload.get("song"))
                elif outcome == "missing":
                    b.unknown.add(i)
            row = row or {"id": i, "title": "", "artist": "", "album": "", "ak": "",
                          "tk": "", "feats": None}
            return {"id": i, "title": row["title"], "artist": row["artist"],
                    "album": row["album"], "ak": row["ak"],
                    "tk": row["tk"] if row["title"] else "",
                    "feats": list(row["feats"]) if row.get("feats") else None,
                    "mix": False}
        return list(await asyncio.gather(*(one(i) for i in ids)))

    async def _mix_anchors(self, b: _StationBatch) -> tuple[str, list]:
        """Your mix: the Sonic Fingerprint of HUB_ND_USER. Its first rows, at
        distance 0, are that user's own most-played tracks."""
        if not self.tier2_configured():
            return "unavailable", []
        status, data = await self._am("fingerprint", {"n": STATION_FINGERPRINT_N}, None, b)
        if status != 200 or not isinstance(data, list):
            return ("unavailable" if status == 0 or status >= 500 else "empty"), []
        rows = [r for r in (_station_row(x) for x in data) if r is not None]
        top = [r for r in rows if r["dist"] is not None and r["dist"] <= 1e-9]
        top = (top or rows[:3])[:STATION_MIX_ANCHORS]
        return "ok", [{"id": r["id"], "title": r["title"], "artist": r["artist"],
                       "album": r["album"], "ak": r["ak"], "tk": r["tk"],
                       "feats": list(r["feats"]) if r["feats"] else None, "mix": True}
                      for r in top]

    def _answer(self, st: Station, tracks: list, exhausted: bool, partial: bool) -> dict:
        return {"stationId": st.id, "seq": st.seq, "rev": st.rev,
                "character": st.character, "engine": st.engine, "tracks": tracks,
                "exhausted": exhausted, "partial": partial}

    async def start(self, body: dict, debug: bool = False) -> tuple[int, dict]:
        seed = self._parse_seed(body.get("seed"))
        if seed is None:
            return 400, {"code": "bad_seed", "error": "seed must be track, tracks or yourMix"}
        character = body.get("character")
        if character is None:
            character = STATION_DEFAULT_CHARACTER
        if character not in STATION_CHARACTERS:
            return 400, {"code": "bad_character",
                         "error": f"character must be one of {list(STATION_CHARACTERS)}"}
        count = _as_int(body.get("count"), 20, 1, 50)
        exclude = self._parse_exclude(body.get("exclude"))
        device = _as_str(body.get("deviceId"), 128)
        bind = _as_str(body.get("bindSession"), 128)
        self._sweep()

        def bound_conflict() -> Optional[tuple[int, dict]]:
            s = self.hub.session
            if bind and bind == s.saved_queue_id and s.station_id in self.stations:
                return 409, {"code": "session_bound", "stationId": s.station_id}
            return None

        conflict = bound_conflict()
        if conflict is not None:
            return conflict
        if bind and bind == self.hub.session.saved_queue_id:
            # The queue this station is about to continue: nothing in it is new.
            exclude |= {t.get("id") for t in self.hub.session.queue
                        if isinstance(t, dict) and isinstance(t.get("id"), str)}
        now = _now_ms()
        st = Station(id=self._mint_id(), seed=seed, name="", character=character,
                     created_at=now, used_at=now)
        deadline = time.monotonic() + STATION_BUDGET_S
        if not await self._acquire(self._computing, deadline):
            return 503, {"fallback": True, "busy": True,
                         "error": "the hub is computing two stations already"}
        try:
            b = _StationBatch(deadline)
            if seed["kind"] == "yourMix":
                outcome, anchors = await self._mix_anchors(b)
                if outcome == "unavailable":
                    return 503, {"fallback": True, "error": "no tier can compute Your mix"}
                if not anchors:
                    return 404, {"code": "seed_unknown",
                                 "error": "AudioMuse has no fingerprint for this user"}
                st.anchor_meta = anchors
            else:
                st.seed_ids = list(seed["ids"])
                st.anchor_meta = await self._seed_meta(st.seed_ids, b)
                st.trail = [m["ak"] for m in st.anchor_meta if m["ak"]][-STATION_TRAIL_MAX:]
                feats = [m["feats"] for m in st.anchor_meta if m["feats"]]
                st.last_feats = feats[-1] if feats else None
            st.index()
            tracks = await self._compute(st, count, exclude, b, debug)
            if seed["kind"] != "yourMix":
                if all(i in b.unknown and i not in b.found for i in st.seed_ids):
                    return 404, {"code": "seed_unknown",
                                 "error": "no tier holds the seed track"}
            no_tier2 = not self.tier2_configured() or b.tier2_failed
            no_tier1 = not self.tier1_configured() or b.tier1_failed
            if not tracks and no_tier2 and no_tier1:
                return 503, {"fallback": True, "error": "no tier answered"}
            st.name = self._name(st)
            exhausted, partial = self._commit(st, tracks, b, device, 0, st.rev)
        finally:
            self._computing.release()
        conflict = bound_conflict()
        if conflict is not None:
            # Another device's start bound the session while this one computed:
            # both continue that station, so this one is never stored.
            return conflict
        self.stations[st.id] = st
        self._evict()
        s = self.hub.session
        if bind and bind == s.saved_queue_id and st.id in self.stations:
            s.station_id = st.id
            s.bump()
            await self.hub._broadcast_session()  # noqa: SLF001 — same module
        else:
            self.hub._save()  # noqa: SLF001 — same module
        await self._announce(st)
        dlog(f"station {st.id} started: {seed['kind']}, {character}, {len(tracks)} tracks, "
             f"engine {st.engine}, rungs {b.rungs}")
        answer = {**self._answer(st, tracks, exhausted, partial), "name": st.name,
                  "seed": st.seed}
        if debug:
            answer["_debug"] = {"rungs": b.rungs, "failed": b.failed,
                                "timedOut": b.timed_out}
        return 200, answer

    def _name(self, st: Station) -> str:
        if st.seed.get("kind") == "yourMix":
            return "Your mix"
        name = st.seed.get("name") or next(
            (m["title"] for m in st.anchor_meta if m.get("title")), "")
        return f"{name} radio" if name else "Radio"

    async def next(self, body: dict, debug: bool = False) -> tuple[int, dict]:
        st = self.get(body.get("stationId"))
        if st is None:
            return 404, {"code": "station_unknown", "error": "no such station"}
        after = body.get("after")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            return 400, {"code": "bad_after", "error": "after must be the seq last applied"}
        count = _as_int(body.get("count"), 5, 1, 25)
        exclude = self._parse_exclude(body.get("exclude"))
        device = _as_str(body.get("deviceId"), 128)
        deadline = time.monotonic() + STATION_BUDGET_S
        lock = self._lock(st.id)
        if not await self._acquire(lock, deadline):
            # Another request's batch for this station is still being computed.
            return 200, {**self._answer(st, [], st.exhausted, True),
                         "servedElsewhere": False}
        try:
            if st.id not in self.stations:
                return 404, {"code": "station_unknown", "error": "no such station"}
            st.used_at = _now_ms()
            last = st.last_batch or {}
            if after != st.seq and after == last.get("after") and device == last.get("device"):
                # A retry of the batch this device was last given.
                return 200, {**self._answer(st, list(last.get("tracks") or []),
                                            st.exhausted, False),
                             "servedElsewhere": False}
            if after < st.seq:
                # Someone already topped this station up past `after`.
                return 200, {**self._answer(st, [], st.exhausted, False),
                             "servedElsewhere": True}
            if after > st.seq:
                log(f"station {st.id}: after {after} is past seq {st.seq}; "
                    "answered as a new batch")
            rev = st.rev
            if not await self._acquire(self._computing, deadline):
                return 200, {**self._answer(st, [], st.exhausted, True),
                             "servedElsewhere": False}
            try:
                b = _StationBatch(deadline)
                tracks = await self._compute(st, count, exclude, b, debug)
                exhausted, partial = self._commit(st, tracks, b, device, st.seq, rev)
            finally:
                self._computing.release()
        finally:
            lock.release()
        self.hub._save()  # noqa: SLF001 — same module
        await self._announce(st)
        answer = {**self._answer(st, tracks, exhausted, partial), "servedElsewhere": False}
        if debug:
            answer["_debug"] = {"rungs": b.rungs, "failed": b.failed,
                                "timedOut": b.timed_out}
        return 200, answer

    async def signal(self, body: dict) -> tuple[int, dict]:
        st = self.get(body.get("stationId"))
        if st is None:
            return 404, {"code": "station_unknown", "error": "no such station"}
        kind = body.get("kind")
        if kind not in _STATION_SIGNALS:
            return 400, {"code": "bad_signal",
                         "error": f"kind must be one of {list(_STATION_SIGNALS)}"}
        if kind == "character":
            value = body.get("value")
            if value not in STATION_CHARACTERS:
                return 400, {"code": "bad_character",
                             "error": f"value must be one of {list(STATION_CHARACTERS)}"}
            if value != st.character:
                st.character = value
                self._bump(st)
                self._changed(st)
            return 200, {"rev": st.rev, "applied": True}
        tid = body.get("trackId")
        if kind in ("skip", "played") and self.hub.session.station_id == st.id:
            # The hub hears this station's queue itself (§18.3).
            return 200, {"rev": st.rev, "applied": False, "ignored": "session"}
        if not isinstance(tid, str) or not st.knows(tid):
            return 200, {"rev": st.rev, "applied": False, "ignored": "unknown_track"}
        changed = False
        if kind == "like":
            if tid not in st.likes:
                st.likes.append(tid)
                changed = True
        elif kind == "unlike":
            if tid in st.likes:
                st.likes.remove(tid)
                changed = True
        elif kind == "dislike":
            if tid in st.likes:
                st.likes.remove(tid)
            self._skip(st, tid, weight=STATION_TUNING[st.character].skips_to_cool)
            changed = True
        elif kind == "skip":
            self._skip(st, tid)
            changed = True
        elif kind == "played":
            self._played(st, tid)
            key = st.track_key(tid)
            self._history_add(tid, key)
            changed = True
        if changed:
            self._bump(st)
            self._changed(st)
        return 200, {"rev": st.rev, "applied": True}

    def _changed(self, st: Station) -> None:
        self.hub._save()  # noqa: SLF001 — same module
        self.hub._spawn(self._announce(st))  # noqa: SLF001 — same module

    def _skip(self, st: Station, tid: str, weight: int = 1) -> None:
        now = _now_ms()
        st.skips = [s for s in st.skips if now - _as_int(s[1]) < STATION_SKIP_TTL_MS]
        st.skips.append([tid, now])
        ak = st.artist_of(tid)
        if not ak:
            return
        times = [t for t in st.artist_skips.get(ak, []) if now - t < STATION_COOL_MS]
        times += [now] * weight
        st.artist_skips[ak] = times[-10:]
        if len(times) >= STATION_TUNING[st.character].skips_to_cool:
            st.cooldown[ak] = now + STATION_COOL_MS
        st.cooldown = {k: v for k, v in st.cooldown.items() if v > now}

    @staticmethod
    def _played(st: Station, tid: str) -> None:
        if tid in st.played:
            st.played.remove(tid)
        st.played.append(tid)
        st.played = st.played[-20:]

    # ----- the session's half (§18.3) ---------------------------------------- #
    def departure(self, track: Any, verdict: Optional[str], is_play: bool,
                  steer: bool) -> None:
        """The hub judged the track the session just left. Every play feeds the
        history; a skip or a play-through steers the bound station, when it is a
        track that station served or seeded."""
        if not isinstance(track, dict):
            return
        tid = track.get("id")
        if not isinstance(tid, str) or not tid:
            return
        if is_play:
            self._history_add(tid, _station_title_key(track.get("title"),
                                                      track.get("artist")))
        if not steer or verdict not in ("played", "skip"):
            return
        st = self.bound()
        if st is None or not st.knows(tid):
            return
        if verdict == "played":
            self._played(st, tid)
        else:
            self._skip(st, tid)
        self._bump(st)
        self._changed(st)


class _NullAsync:
    """`async with` that holds nothing."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class StationProxy(HttpProxy):
    """`/sonic/station/*` (PROTOCOL §18.1). Uses the base class for auth, the body
    read and the body-key whitelist; answers from the hub's StationEngine instead
    of forwarding. Must sit BEFORE SonicProxy in PROXIES: SonicProxy claims every
    `/sonic/*` path and answers 404 for the ones it does not know."""

    prefix = "/sonic/station"
    label = "station"
    routes = STATION_ROUTES

    @property
    def upstream(self) -> str:
        return AUDIOMUSE_URL or NAVIDROME_URL

    @property
    def enabled(self) -> bool:
        return bool(TOKEN and (AUDIOMUSE_URL or StationEngine.tier1_configured()))

    @property
    def disabled_reason(self) -> str:
        if not TOKEN:
            return "HUB_TOKEN is empty (refusing to run as an open relay)"
        return "neither AUDIOMUSE_URL nor NAVIDROME_URL + HUB_ND_USER + HUB_ND_PASS is set"

    def disabled_probe(self, route: tuple[str, str]) -> Optional[tuple]:
        spec = self.routes.get(route)
        if spec is None:
            return None
        if spec.get("probe"):
            return _http_json(200, {"configured": False, "available": False,
                                    "engine": None, "default": STATION_DEFAULT_CHARACTER,
                                    "characters": [dict(c) for c in STATION_CATALOGUE]})
        # The client's own fallback runs; nothing here is an error to show.
        return _http_json(503, {"fallback": True,
                                "error": f"stations disabled: {self.disabled_reason}"})

    async def call(self, route: tuple[str, str], spec: dict,
                   params: list[tuple[str, str]],
                   body: Optional[dict]) -> tuple[int, bytes, str]:
        hub = HUB_INSTANCE
        if hub is None:
            return 503, json.dumps({"fallback": True, "error": "hub starting"}).encode(), \
                "application/json"
        status, payload = await hub.stations.route(route[1], body or {})
        return status, json.dumps(payload).encode(), "application/json"


SONIC = SonicProxy()
LB = LbProxy()
PREVIEW = PreviewProxy()
STATION = StationProxy()
# Each returns None for paths outside its own prefix, and the first one that
# claims a path answers it. So STATION goes before SONIC: SonicProxy claims every
# `/sonic/*` path, `/sonic/station/*` included, and 404s the ones it does not list.
PROXIES: tuple[HttpProxy, ...] = (STATION, SONIC, LB, PREVIEW)

# Set once main() builds the hub, so the inbound notify handler below can reach
# the connected devices. The proxy protocol class is constructed by `websockets`
# per connection and has no other handle on it.
HUB_INSTANCE: Optional["Hub"] = None

# The one inbound path: lb-bot telling the hub an album just landed. Not a proxy
# route — nothing is forwarded — so it lives outside LB_ROUTES, and it answers
# whether or not LBBOT_URL is configured, because this direction doesn't need it.
LB_NOTIFY_PATH = "/lb/notify"
# The second inbound path: a fill moved. Its own frame (`t: "fill"`) because a
# `library` event makes every client refetch its discographies and flushes the
# hub's library caches — the right answer to a landing and the wrong one to
# "3 of 12 downloaded" or "cancelled", where nothing in the library changed.
LB_FILL_PATH = "/lb/fill"
_LB_FILL_KINDS = ("album", "gap", "wishlist")
_LB_FILL_STR = ("kind", "key", "releaseMbid", "rgid", "groupId", "state", "status",
                "artist", "album", "failureKind", "reason", "source", "taskStatus",
                "quality")
_LB_FILL_INT = ("done", "failed", "total", "percent", "bytesDone", "bytesTotal",
                "speedBps", "activeFiles", "attempts", "leftOut")
_LB_FILL_FLOAT = ("updatedAt", "serverTime", "retryAt")
_LB_FILL_BOOL = ("retryable", "mp3WouldHelp", "allowMp3", "cancellable", "verifyGaveUp",
                  "allFiles")
# A fill that ended makes the ranked source list it was picked from stale: a
# "try another source" re-read within the 60 s TTL got the same list back,
# with the peer that had just failed still at the top.
_LB_FILL_TERMINAL = ("failed", "cancelled")
# The third inbound path: lb-bot's library index moved (the mirror's live
# trigger). `t: "index"` rather than `library` for the same reason as `fill`, and
# more so — during a bulk build it arrives every 2 s, and a `library` frame at
# that rate would have every client refetch its whole library continuously.
LB_INDEX_PATH = "/lb/index"
# The caches an index push drops: the discography read, the clients' fallback
# when they have no mirror, and the override map, whose `toCheck` list depends on
# scan results (a list still showing an artist as unmatched after its scan landed
# is worse than one extra upstream read per throttled push). Named so the
# proxy-cache test invalidates exactly what the handler does.
LB_INDEX_INVALIDATES = frozenset({("GET", "/lb/artist/discography"),
                                  ("GET", "/lb/overrides")})
_LB_INDEX_EPOCH_MAX = 64
_LB_NOTIFY_STR_MAX = 200
_LB_NOTIFY_IDS_MAX = 16
_LB_NOTIFY_RENAME = {"release_mbid": "releaseMbid", "nd_artist_id": "ndArtistId"}

# Cached lb-bot answers a landed album makes wrong. Deliberately NOT the long-TTL
# MusicBrainz routes (editions, tracklists): a fill changes nothing about those.
# `/lb/overrides` (I-022/I-023) is there for a client that only watches: the
# album and merge lists change with a landing's retag and nothing else tells it.
# `/lb/album/lookup` is a MusicBrainz search and belongs here anyway — the
# ranking does not move, but the `releaseOwned` badge it now carries does, and
# a stale badge is a row offering to fetch a record already on disk.
#
# The Deezer rows and `/lb/artist/related` join it for that same reason and no
# other — their *content* is a chart or a similarity list that a fill does not
# move — and `/lb/wishlist` for the opposite one: a landing is precisely what
# takes a row OFF it, so the answer the fill falsifies is the whole list.
LB_LIBRARY_ROUTES = {
    ("GET", "/lb/artist/discography"),
    ("GET", "/lb/album/lookup"),
    ("GET", "/lb/artist/similar"),
    ("GET", "/lb/artist/related"),
    ("GET", "/lb/deezer/chart"),
    ("GET", "/lb/deezer/editorial"),
    ("GET", "/lb/wishlist"),
    ("GET", "/lb/fresh-releases"),
    ("GET", "/lb/status"),
    ("GET", "/lb/overrides"),
}

# One discography release row, in the exact shape GET /lb/artist/discography
# returns it, so a client can patch its copy without re-reading the page.
_LB_ROW_STR = ("rgid", "title", "year", "primary_type", "effective_type",
               "status", "match_method", "group_id")
_LB_ROW_INT = ("present", "total")


def _lb_notify_row(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    row: dict = {}
    for key in _LB_ROW_STR:
        value = _as_str(raw.get(key), _LB_NOTIFY_STR_MAX)
        if value is not None:
            row[key] = value
    for key in _LB_ROW_INT:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            row[key] = value
    score = raw.get("match_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        row["match_score"] = float(score)
    for key, cap in (("secondary_types", 8), ("navidrome_album_ids", _LB_NOTIFY_IDS_MAX)):
        values = raw.get(key)
        if isinstance(values, list):
            row[key] = [v for v in (_as_str(x, 128) for x in values[:cap]) if v]
    # A row with no rgid can't be matched to anything a client holds.
    return row if row.get("rgid") else None


def _lb_fill_frame(payload: dict) -> Optional[dict]:
    """Rebuild a fill push into the `fill` frame, field by field and bounded."""
    kind = _as_str(payload.get("kind"), 16)
    if kind not in _LB_FILL_KINDS:
        return None
    frame: dict = {"t": "fill"}
    for key in _LB_FILL_STR:
        value = _as_str(payload.get(key), _LB_NOTIFY_STR_MAX)
        if value:
            frame[key] = value
    for key in _LB_FILL_INT:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            frame[key] = int(value)
    for key in _LB_FILL_FLOAT:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            frame[key] = float(value)
    for key in _LB_FILL_BOOL:
        if key in payload:
            frame[key] = bool(payload.get(key))
    ids = payload.get("ndAlbumIds")
    if isinstance(ids, list):
        clean = [v for v in (_as_str(i, 128) for i in ids[:_LB_NOTIFY_IDS_MAX]) if v]
        if clean:
            frame["ndAlbumIds"] = clean
    if not frame.get("key"):
        return None
    return frame


def _lb_index_hint(raw: Any) -> Optional[dict]:
    """`{seq, epoch}` or None. Used on the inbound body AND on state.json's copy,
    so a hand-edited or older file cannot put a malformed hint into `welcome`."""
    if not isinstance(raw, dict):
        return None
    seq, epoch = raw.get("seq"), raw.get("epoch")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None
    if not isinstance(epoch, str) or not epoch or len(epoch) > _LB_INDEX_EPOCH_MAX:
        return None
    return {"seq": seq, "epoch": epoch}


async def _handle_lb_index(protocol: Any, raw_path: str, headers: Any,
                           method: str) -> Optional[tuple]:
    """lb-bot's index head moved: remember it and tell every client to pull.

    The frame carries no rows — a client pulls `/lb/index/changes` from its own
    cursor, so a push it missed costs nothing but latency. Deliberately does NOT
    touch `LB_LIBRARY_ROUTES`: the one cache it drops is the discography read,
    which is the clients' fallback path when they have no mirror.
    """
    path, _, _query = raw_path.partition("?")
    if (path.rstrip("/") or "/") != LB_INDEX_PATH:
        return None
    if method != "POST":
        return _http_json(405, {"error": "method not allowed"})

    auth = (headers.get("Authorization") or "") if headers is not None else ""
    supplied = auth[7:] if auth.startswith("Bearer ") else ""
    if not TOKEN or not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
        return _http_json(401, {"error": "unauthorized"})
    LB.mark_alive()

    raw = await _read_body(protocol, headers)
    if raw is None:
        return _http_json(400, {"error": "request body too large or unreadable"})
    try:
        payload = json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        return _http_json(400, {"error": "malformed JSON body"})
    hint = _lb_index_hint(payload)
    if hint is None:
        return _http_json(400, {"error": "index push needs an integer seq >= 0 and a "
                                         f"non-empty epoch of at most {_LB_INDEX_EPOCH_MAX} chars"})

    LB.invalidate(LB_INDEX_INVALIDATES)
    if HUB_INSTANCE is not None:
        HUB_INSTANCE.set_lb_index(hint)
        await HUB_INSTANCE._broadcast({"t": "index", **hint})  # noqa: SLF001 — same module
    dlog("LB index ->", hint)
    return _http_json(200, {"ok": True})


async def _handle_lb_fill(protocol: Any, raw_path: str, headers: Any,
                          method: str) -> Optional[tuple]:
    """Fan a fill's progress or state change out to every connected client.

    Deliberately does NOT touch `LB_LIBRARY_ROUTES`: nothing in the library
    moved. The one cache it invalidates is the ranked source list, and only
    when the fill ended.
    """
    path, _, _query = raw_path.partition("?")
    if (path.rstrip("/") or "/") != LB_FILL_PATH:
        return None
    if method != "POST":
        return _http_json(405, {"error": "method not allowed"})

    auth = (headers.get("Authorization") or "") if headers is not None else ""
    supplied = auth[7:] if auth.startswith("Bearer ") else ""
    if not TOKEN or not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
        return _http_json(401, {"error": "unauthorized"})
    LB.mark_alive()

    raw = await _read_body(protocol, headers)
    if raw is None:
        return _http_json(400, {"error": "request body too large or unreadable"})
    try:
        payload = json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        return _http_json(400, {"error": "malformed JSON body"})
    if not isinstance(payload, dict):
        return _http_json(400, {"error": "malformed JSON body"})
    frame = _lb_fill_frame(payload)
    if frame is None:
        return _http_json(400, {"error": "fill needs a known kind and a key"})

    if frame.get("kind") == "album" and frame.get("state") in _LB_FILL_TERMINAL:
        LB.invalidate({("GET", "/lb/album/sources")})
    if HUB_INSTANCE is not None:
        await HUB_INSTANCE._broadcast(frame)  # noqa: SLF001 — same module
    dlog("LB fill ->", frame)
    return _http_json(200, {"ok": True})


async def _handle_lb_notify(protocol: Any, raw_path: str, headers: Any,
                            method: str) -> Optional[tuple]:
    """Fan an lb-bot library change out to every connected client.

    Same bearer as everything else, so a device token is enough to send one. That
    is deliberate and harmless: the frame carries no authority — it only tells
    clients to re-read data they can already read — and the alternative is a
    second shared secret for a notification.
    """
    path, _, _query = raw_path.partition("?")
    if (path.rstrip("/") or "/") != LB_NOTIFY_PATH:
        return None
    if method != "POST":
        return _http_json(405, {"error": "method not allowed"})

    auth = (headers.get("Authorization") or "") if headers is not None else ""
    supplied = auth[7:] if auth.startswith("Bearer ") else ""
    if not TOKEN or not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
        return _http_json(401, {"error": "unauthorized"})
    LB.mark_alive()

    raw = await _read_body(protocol, headers)
    if raw is None:
        return _http_json(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        return _http_json(400, {"error": "malformed JSON body"})
    if not isinstance(payload, dict):
        return _http_json(400, {"error": "malformed JSON body"})

    # Rebuilt field by field rather than relayed: this goes straight out to every
    # client, so it carries only what the wire format promises, bounded in size.
    frame = {"t": "library", "event": "albumPlaced"}
    for key in ("event", "release_mbid", "rgid", "artist", "album", "nd_artist_id"):
        value = _as_str(payload.get(key), _LB_NOTIFY_STR_MAX)
        if value:
            frame[_LB_NOTIFY_RENAME.get(key, key)] = value
    ids = payload.get("nd_album_ids")
    if isinstance(ids, list):
        clean = [v for v in (_as_str(i, 128) for i in ids[:_LB_NOTIFY_IDS_MAX]) if v]
        if clean:
            frame["ndAlbumIds"] = clean
    row = _lb_notify_row(payload.get("row"))
    if row:
        frame["row"] = row

    # BEFORE the broadcast. Every client answers this frame by re-reading, and a
    # re-read served from a cache filled a minute ago is the old "missing" answer —
    # which is exactly what made a landed album take a minute longer to show than
    # the frame announcing it.
    LB.invalidate(LB_LIBRARY_ROUTES)

    if HUB_INSTANCE is not None:
        await HUB_INSTANCE._broadcast(frame)  # noqa: SLF001 — same module
    dlog("LB notify ->", frame)
    return _http_json(200, {"ok": True})


class Act:
    """One `act` frame on its way through the hub: who sent it, on which socket,
    and what its `ack` will say (PROTOCOL §5.4).

    The socket is kept because a `cid` belongs to a connection. An act that waited
    behind a transfer can outlive the connection it came in on, and by then its
    sender may be counting `cid`s from the start again on a new one.
    """
    __slots__ = ("dev", "ws", "msg", "cid", "ok", "code", "message", "acked", "committed")

    def __init__(self, dev: "Device", msg: dict) -> None:
        self.dev = dev
        self.ws = dev.ws
        self.msg = msg
        cid = msg.get("cid")
        self.cid: Optional[str] = cid[:CID_MAX] if isinstance(cid, str) and cid else None
        self.ok = True
        self.code: Optional[str] = None
        self.message: Optional[str] = None
        self.acked = False
        self.committed = False      # a transfer: the handoff was made

    @property
    def action(self) -> Optional[str]:
        # None for anything that is not a string: it is looked up in sets, and an
        # unhashable `action` must end as an acked `bad_action`, not as a raise in
        # the read loop that leaves a `cid` unanswered.
        a = self.msg.get("action")
        return a if isinstance(a, str) else None

    @property
    def stale(self) -> bool:
        """Its sender's connection is gone (dropped, or replaced by a newer one)."""
        return self.dev.ws is not self.ws

    def fail(self, code: str, message: str, ok: bool = False) -> None:
        self.ok, self.code, self.message = ok, code, message


# --------------------------------------------------------------------------- #
# Hub
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self) -> None:
        self.session = Session()
        self.devices: dict[str, Device] = {}
        # Rolling saved-queue history (Continue Listening), authoritative + shared.
        # Keyed by record id; capped at SAVED_QUEUES_MAX, oldest by updatedAt evicted.
        self.saved_queues: dict[str, dict] = {}
        # Deletions remembered as {id: deletedAt} so a client that still holds the row
        # locally can't resurrect it via syncSavedQueues. Capped + persisted.
        self.deleted_saved_queues: dict[str, int] = {}
        # "Mixed for You" — recipes, not results. Keyed by id, capped at MIXES_MAX.
        #
        # NO TOMBSTONES, deliberately, and the asymmetry with saved_queues above is
        # worth stating rather than leaving to be discovered. A saved queue is
        # published concurrently by several devices — every receiver stamps the one
        # it is playing — so a delete races a re-publish and needs a tombstone to
        # win. A mix is only ever written by the user, on one device, through an
        # explicit act; nothing republishes it, so there is no race to arbitrate and
        # union-merge has nothing to merge.
        self.mixes: dict[str, dict] = {}
        # lb-bot's index head as last pushed (`{seq, epoch}`), relayed in `welcome`.
        # A HINT only: a client pulls on every welcome whatever it says, because
        # a push missed while the hub was down would leave this stale.
        self.lb_index: Optional[dict] = None
        self._last_lb_index_save_at = 0.0
        self._last_sq_progress_at = 0.0  # throttle the current record's cursor writes
        self._last_position_save_at = 0.0  # throttle position-only state persistence
        self._last_progress_sent = 0.0
        self._play_intent_at = 0.0  # monotonic time of the last user play/pause intent
        self._position_intent_at = 0.0  # monotonic time of the last seek/jump/skip intent
        self._pre_intent_position = 0  # position_ms just BEFORE that seek/jump/skip
        self._index_intent_at = 0.0  # monotonic time of the last skip/jump to ANOTHER index
        self._pre_intent_index = 0  # the index that skip/jump left
        # The command echo: one counter for the whole process, so a number is never
        # reused for another device or after a reconnect (PROTOCOL §5.2).
        self._do_seq = 0
        self._stale_reports = 0  # /health: reports dropped for `applied` < need
        self._echo_backstops = 0  # /health: times ECHO_BACKSTOP expired on a device
        # Tasks nobody awaits (closing a superseded socket). asyncio keeps only weak
        # references to tasks, so an unreferenced one can be collected mid-flight.
        self._bg_tasks: set = set()
        self._mirror_task: Optional[asyncio.Task] = None  # single mirror worker
        self._mirror_pending = False  # a newer snapshot is waiting to be written
        self._mirror_latest: tuple = ([], None, 0)  # (ids, current, position_ms)
        # The fsynced backup of state.json (STATE_BACKUP_SUFFIX). Same shape as the
        # mirror above: one worker, latest payload wins, never two writers.
        self._backup_task: Optional[asyncio.Task] = None
        self._backup_pending = False
        self._backup_latest: Optional[tuple[str, str]] = None  # (signature, payload)
        # Signature of the newest backup written or queued; None = unknown, so the
        # next save writes one. Set by _load() before anything can save.
        self._backup_sig: Optional[str] = None
        self._started_at = time.monotonic()
        # Transfers, and every act held behind one, in arrival order. One task
        # drains it (`_drain_held`), so transfers cannot interleave; it exists
        # exactly while the hub is holding acts, and is created by the transfer
        # that starts the hold. See `_on_act`.
        self._held: Any = deque()
        self._drain_task: Optional[asyncio.Task] = None
        # Monotonic id for every handoff, carried on do:release/released and
        # do:load/loaded so a reply can be matched to the attempt that asked for it.
        self._transfer_seq = 0
        # The sleep timer, if one is set, and the pending callback for its end.
        self._sleep: Optional[SleepTimer] = None
        self._sleep_handle: Any = None
        # Who its pause comes from: an `act` needs a sender, and this one has no
        # socket, so it is never stale, never acked and never sent an `error`.
        self._sleep_dev = Device(id="sleep-timer", name="sleep timer")
        # The stations and their play history (PROTOCOL §18). Built before _load,
        # which hands it the persisted stations.
        self.stations = StationEngine(self)
        # The queue position an `ended` report was last judged at, so a receiver
        # repeating the flag does not count one play-through twice.
        self._ended_judged: Optional[tuple] = None
        self._load()
        # A timer the state file brought back counts down from here.
        self._sleep_arm()

    def _next_transfer_id(self) -> str:
        self._transfer_seq += 1
        return f"t{self._transfer_seq}"

    def _mark_play_intent(self) -> None:
        self._play_intent_at = time.monotonic()

    def _mark_position_intent(self) -> None:
        # A fresh seek/jump/skip makes the receiver's in-flight 1 Hz reports
        # (carrying the OLD position) untrustworthy for INTENT_GRACE seconds.
        # Capture the position we're leaving so a stale report — one still near
        # that old spot — can be told apart from real forward progress toward
        # the new target. MUST be called BEFORE overwriting session.position_ms.
        self._position_intent_at = time.monotonic()
        self._pre_intent_position = self.session.position_ms

    def _mark_index_intent(self, new_index: int) -> None:
        # The same guard for `index`, which had none: a 1 Hz tick sampled before a
        # skip names the index just left and used to move the session back to it
        # (three `session` frames for one `next`). Armed only when the index really
        # changes: a jump to the current index leaves nothing to tell apart, and
        # arming it would ignore a true report. MUST be called BEFORE overwriting
        # session.index.
        if new_index != self.session.index:
            self._index_intent_at = time.monotonic()
            self._pre_intent_index = self.session.index
            # A skip, a play-through, or neither, for the track being left (§18.3).
            self._judge_departure(self.session.index, new_index, self.session.position_ms)

    def _next_in_order(self, index: int) -> Optional[int]:
        """The index play order takes after `index` (None: the queue ends there)."""
        order = self._play_order()
        try:
            pos = order.index(index)
        except ValueError:
            return None
        if pos + 1 < len(order):
            return order[pos + 1]
        return order[0] if self.session.repeat == "all" and order else None

    def _judge_departure(self, left: int, new: Optional[int], pos_ms: int, *,
                         ended: bool = False, steer: bool = True) -> None:
        """Judge the track at `left`, which playback is leaving (PROTOCOL §18.3).

        `pos_ms` is where it was left: for a report, the position of the report
        BEFORE the one that moves the index, which already carries the new
        track's position. Played through: `ended`, 85 %, or under 15 s to go
        (that last only past half the track, so a short track skipped at once is
        not "played"). Skipped: a step forward in play order before min(30 s,
        40 %). Anything else is neither: a backward or non-adjacent move at a
        position that is not a play-through. `steer=False` (a setQueue or clear)
        feeds the play history only, so a new queue is never a skip.
        """
        s = self.session
        if not 0 <= left < len(s.queue) or not isinstance(s.queue[left], dict):
            return
        if not ended:
            self._ended_judged = None
        track = s.queue[left]
        dur = _as_int(track.get("durationMs"), 0, 0)
        pos = max(0, _as_int(pos_ms, 0))
        played = ended or (dur > 0 and (
            pos >= STATION_PLAYED_FRACTION * dur
            or (dur - pos < STATION_PLAYED_TAIL_MS and pos >= 0.5 * dur)))
        skip_below = min(STATION_SKIP_MS, STATION_SKIP_FRACTION * dur) if dur else STATION_SKIP_MS
        skipped = (not played and new is not None and new == self._next_in_order(left)
                   and pos < skip_below)
        is_play = played or pos >= STATION_PLAY_MS or (
            dur > 0 and pos >= STATION_PLAY_FRACTION * dur)
        verdict = "played" if played else "skip" if skipped else None
        dlog(f"STATION judged index {left} -> {new}: {verdict or 'neither'} "
             f"at {pos}/{dur} ms{' (ended)' if ended else ''}"
             f"{'' if steer else ' (history only)'}")
        try:
            self.stations.departure(track, verdict, is_play, steer)
        except Exception as e:  # noqa: BLE001 — steering must never break playback
            log("station departure failed:", e)

    def _spawn(self, coro: Any) -> None:
        """Run `coro` in the background, holding a reference until it is done."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ----- the command echo (PROTOCOL §5.2-5.3) ---------------------------- #
    def _echo_expect(self, dev: Optional[Device], seq: Optional[int] = None) -> Optional[int]:
        """Raise `dev`'s need to a stamped directive's number and return that number.

        Called by `_do` as it sends, and ahead of the send where a device becomes
        active for a load that is still to be written (a transfer target): from
        that instant its reports describe a player that has not loaded yet,
        whatever they say. Passing the number back in is a no-op, so the later
        `_do` records nothing twice. None for a device without the cap.
        """
        if dev is None or ECHO_CAP not in (dev.caps or []):
            return None
        if seq is None:
            self._do_seq += 1
            seq = self._do_seq
        if seq > dev.echo_need:
            dev.echo_need = seq
            # Past the backstop the clock is spent: the device stays on the old rule
            # until `applied` reaches `need`, so there is nothing left to time.
            if not dev.echo_backstop:
                dev.echo_sent.append((seq, time.monotonic()))
                if len(dev.echo_sent) == 1:
                    self._echo_arm(dev)
        return seq

    def _echo_confirm(self, dev: Device, applied: int) -> None:
        """A report from `dev` named `applied`: everything up to it is confirmed."""
        self._echo_settle(dev, applied)
        if applied >= dev.echo_need:
            dev.echo_sent.clear()
            self._echo_arm(dev)
            if dev.echo_backstop:
                dev.echo_backstop = False
                log(f"echo: {dev.name} ({dev.id[:8]}) caught up at do seq {applied}")

    def _echo_settle(self, dev: Device, seq: Optional[int]) -> None:
        """Stop the backstop clock for every directive up to `seq`.

        Does not touch `need`: a report still has to name the number to be believed.
        Besides a report, one thing settles a directive: the hub giving up on a
        transfer's load. The receiver sends no echoing report after that one
        (PROTOCOL §5.3, duty 4), so nothing else would ever stop its clock.
        """
        sent = dev.echo_sent
        if seq is None or not sent or sent[0][0] > seq:
            return
        while sent and sent[0][0] <= seq:
            sent.popleft()
        self._echo_arm(dev)

    def _echo_arm(self, dev: Device) -> None:
        """(Re)start the backstop clock from the oldest unconfirmed directive.

        From the OLDEST, so a receiver that never settles cannot keep itself
        distrusted by being sent more directives: three pauses five seconds apart
        still end the wait twelve seconds after the first.
        """
        if dev.echo_timer is not None:
            dev.echo_timer.cancel()
            dev.echo_timer = None
        if dev.echo_backstop or not dev.echo_sent:
            return
        seq, sent_at = dev.echo_sent[0]
        delay = max(0.0, sent_at + ECHO_BACKSTOP - time.monotonic())
        dev.echo_timer = asyncio.get_running_loop().call_later(
            delay, self._echo_backstop_fire, dev, seq)

    def _echo_backstop_fire(self, dev: Device, seq: int) -> None:
        dev.echo_timer = None
        if dev.echo_backstop or not dev.echo_sent or dev.echo_sent[0][0] != seq:
            self._echo_arm(dev)  # that directive was confirmed meanwhile
            return
        dev.echo_sent.clear()
        if dev.id != self.session.active_device_id:
            # It lost the slot with a directive unconfirmed. Its reports are not
            # mirrored, so there is nothing to misjudge and nothing to count. Should
            # it take the slot back without having caught up, the first report
            # dropped for it starts this clock again (see _on_report).
            return
        dev.echo_backstop = True
        self._echo_backstops += 1
        log(f"echo backstop: {dev.name} ({dev.id[:8]}) has not confirmed do seq {seq} "
            f"after {ECHO_BACKSTOP:.0f}s (need {dev.echo_need}); its reports go by the "
            f"{INTENT_GRACE:.0f}s rule until it catches up")

    def _echo_reset(self, dev: Device) -> None:
        """Forget what `dev` was sent. The one reset point is its `welcome`."""
        dev.echo_need = 0
        dev.echo_sent.clear()
        dev.echo_backstop = False
        if dev.echo_timer is not None:
            dev.echo_timer.cancel()
            dev.echo_timer = None

    # ----- the sleep timer (PROTOCOL §5.1) --------------------------------- #
    def _session_snapshot(self) -> dict:
        """The session as a client is sent it: what is stored, plus the sleep
        timer with the time it has left at this instant.

        Computed per frame because nothing is broadcast while the timer counts
        down: a client counts from the arrival of the last frame, on its own
        clock, and a device that connects mid-way gets the right remainder in its
        `welcome`.
        """
        snap = self.session.snapshot()
        t = self._sleep
        snap["sleepTimer"] = None if t is None else {
            "remainingMs": t.remaining_ms(), "minutes": t.minutes, "setBy": t.set_by}
        return snap

    def _sleep_set(self, timer: Optional[SleepTimer]) -> None:
        """Make `timer` the sleep timer (None = none) and aim the clock at it."""
        self._sleep = timer
        self._sleep_arm()

    def _sleep_arm(self) -> None:
        if self._sleep_handle is not None:
            self._sleep_handle.cancel()
            self._sleep_handle = None
        timer = self._sleep
        if timer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop: a Hub built outside one (a test reading a state file)
        self._sleep_handle = loop.call_later(
            max(0.0, timer.deadline - time.monotonic()),
            lambda: self._spawn(self._sleep_expire(timer)))

    async def _sleep_expire(self, timer: SleepTimer) -> None:
        """The timer ran out: what is playing is paused, and the timer is gone.

        The pause IS a `pause` act, sent through `_on_act` like one from a
        controller, so it gets everything a controller's pause gets. Above all
        the hold during a transfer: applied in the middle of one it would go to
        the device being abandoned and be overwritten by the commit, and the new
        device would start playing after the timer had run out. Held, it is
        folded into the load or run right behind it.

        With nothing playing there is nothing to pause, so no directive is sent
        and only the timer's end is announced. Except while a transfer is under
        way: what will be playing after it is not decided yet (a `play` may be
        waiting in the same queue), so the pause is queued behind whatever
        arrived first, and paused is how the session comes out.

        The timer is cleared before the pause, so the `session` frame the pause
        broadcasts (or the commit that folded it) is also the one that says the
        timer is gone: one frame, not a paused session that still shows a timer
        with no time left.
        """
        if self._sleep is not timer:
            return  # replaced or cleared between the clock firing and this task
        self._sleep_handle = None
        self._sleep = None
        s = self.session
        who = self._dev(timer.set_by)
        note = f"sleep timer ({timer.minutes} min, set by {who.name if who else timer.set_by})"
        if s.is_playing or self._drain_task is not None:
            log(f"{note} ran out: pausing")
            await self._on_act(self._sleep_dev, {"t": "act", "action": "pause"})
        else:
            log(f"{note} ran out with nothing playing")
            s.bump()
            await self._broadcast_session()

    # ----- persistence ----------------------------------------------------- #
    def _load(self) -> None:
        """Boot: read state.json, or fall back to its backup, or start fresh.

        Parsing is kept apart from applying. A file that cannot be PARSED is set
        aside under a `.corrupt-<UTC>` name and the backup is loaded instead; it
        used to be logged and then overwritten by the first save, which is how a
        damaged file became a lost one. A file that parses is APPLIED section by
        section (see `_apply_state`).
        """
        bak_path = STATE_PATH + STATE_BACKUP_SUFFIX
        status, data = _read_state_file(STATE_PATH)
        restored = False
        if status != "ok":
            if status == "corrupt":
                aside = _set_aside_state_file(STATE_PATH)
                if aside:
                    log(f"state file unreadable — set aside as {aside}")
            bak_status, data = _read_state_file(bak_path)
            if bak_status == "corrupt":
                aside = _set_aside_state_file(bak_path)
                if aside:
                    log(f"state backup unreadable too — set aside as {aside}")
            if bak_status != "ok":
                log("state file empty, starting fresh" if status == "empty"
                    else "no prior state, starting fresh")
                # Nothing irreplaceable exists yet, so nothing is owed a backup: the
                # empty state counts as already backed up, and the backup file first
                # appears with the first mix or tombstone.
                self._backup_sig = _durable_signature([], {})
                return
            restored = True
            log(f"state file {status} — restoring from {bak_path}")

        self._apply_state(data)
        log(f"loaded state: {len(self.session.queue)} queued, {len(self.devices)} known devices")

        if restored:
            # Put state.json back at once rather than on the first broadcast: until
            # then the backup would be the only copy.
            self._backup_sig = _durable_signature(data.get("mixes"),
                                                  data.get("deletedSavedQueues"))
            self._save(blocking_backup=True)
            return
        # A clean load. Bring the backup in line if it disagrees with what was just
        # loaded — it is stale, unreadable, or (first boot on this build, with a mix
        # or tombstone already in the file) not there yet. state.json wins.
        bak_status, bak = _read_state_file(bak_path)
        if bak_status == "ok":
            self._backup_sig = _durable_signature(bak.get("mixes"),
                                                  bak.get("deletedSavedQueues"))
        elif bak_status == "corrupt":
            self._backup_sig = None
        else:
            self._backup_sig = _durable_signature([], {})
        try:
            payload, sig = self._state_payload()
        except Exception as e:  # noqa: BLE001
            log("failed to build the state backup:", e)
            return
        self._request_backup(sig, payload, blocking=True)

    def _apply_state(self, data: dict) -> None:
        """Apply a parsed state file, each section guarded on its own.

        One `try` around the lot meant `"mixes": null`, or a single device row with
        no `id`, aborted everything after it — and the hub then ran, and saved, with
        only what had been applied so far. That turned one bad value into the loss
        of every section behind it. A section that cannot be read is now logged and
        skipped; the others load.
        """
        def session() -> None:
            s = data.get("session", {})
            queue = s.get("queue", [])
            if not isinstance(queue, list):
                raise TypeError(f"queue is {type(queue).__name__}, not a list")
            self.session = Session(
                rev=s.get("rev", 0),
                active_device_id=None,  # nothing is live yet after a restart
                queue=queue,
                index=s.get("index", 0),
                order=s.get("order"),
                position_ms=s.get("positionMs", 0),
                is_playing=False,
                repeat=s.get("repeat", "none"),
                shuffle=s.get("shuffle", False),
                updated_at=s.get("updatedAt", 0),
                saved_queue_id=s.get("savedQueueId"),
                source_kind=s.get("sourceKind", "manual"),
                source_name=s.get("sourceName"),
                station_id=_as_str(s.get("stationId"), 64),
            )

        def saved_queues() -> None:
            # Saved-queue history (rolling, capped). Keyed by id for cheap upsert.
            # Re-sanitised on the way in so a state.json written by an older build (which
            # stored client values verbatim) can't keep breaking _save() forever.
            for rec in data.get("savedQueues", []):
                clean = self._sanitize_saved_queue(rec, SAVED_QUEUE_SONGS_HARD_MAX)
                if clean is not None:
                    self.saved_queues[clean["id"]] = clean

        def mixes() -> None:
            # Additive in exactly the way savedQueues is: there is no schema version
            # and no migration path in this file, only `data.get(key, default)`. So a
            # state.json from before mixes existed loads with zero of them, and an
            # older hub reading a newer file silently drops the key rather than
            # failing — which is the whole reason the collection can be added at all.
            for raw_mix in data.get("mixes", []):
                clean = self._sanitize_mix(raw_mix)
                if clean is not None:
                    self.mixes[clean["id"]] = clean

        def stations() -> None:
            # Additive like `mixes`: a state.json from before stations loads with
            # none, and an older hub reading this one drops the key.
            self.stations.load_state(data.get("stations", []))

        def lb_index() -> None:
            # Additive like `mixes`: a state.json from before the index mirror
            # simply has no hint, and welcome omits the field until lb-bot pushes.
            self.lb_index = _lb_index_hint(data.get("lbIndex"))

        def tombstones() -> None:
            for rid, at in (data.get("deletedSavedQueues") or {}).items():
                try:
                    self.deleted_saved_queues[rid] = int(at)
                except (TypeError, ValueError):
                    continue

        def devices() -> None:
            for d in data.get("devices", []):
                try:  # per row: one bad device must not cost the whole registry
                    did = d["id"]
                    if not isinstance(did, str) or not did:
                        raise ValueError(f"id is {did!r}")
                    self.devices[did] = Device(
                        id=did, name=d.get("name", "Unknown"),
                        platform=d.get("platform", "unknown"),
                        caps=d.get("caps", ["controller"]),
                        kind=_as_kind(d.get("kind")),
                        # Never anything but an int 0..100, whatever the file holds:
                        # the row is relayed as it is and a client reads null as 100.
                        volume=_as_int(d.get("volume"), 100, 0, 100),
                        # _as_int: _prune_devices compares this on every sweep, long
                        # after this guard is gone.
                        last_seen=_as_int(d.get("lastSeen")),
                    )
                except Exception as e:  # noqa: BLE001
                    log("state: skipped an unreadable device row:", e)

        def sleep_timer() -> None:
            # Only a timer that is still running comes back. One whose end passed
            # while the hub was down is dropped and pauses nothing: firing it late
            # would stop whatever the user has started since.
            ends_at = data.get("sleepTimerEndsAt")
            if ends_at is None:
                return
            left = (int(ends_at) - time.time() * 1000) / 1000
            if left <= 0:
                log("sleep timer ran out while the hub was down: dropped")
                return
            minutes = _as_int(data.get("sleepTimerMinutes"),
                              int(left / SLEEP_MINUTE_S) + 1, 1, SLEEP_MINUTES_MAX)
            # Never longer than it was set for: a wall clock that was wrong when
            # the timer was set, or was stepped back since, must not stretch it.
            left = min(left, minutes * SLEEP_MINUTE_S)
            self._sleep = SleepTimer(
                minutes=minutes, set_by=_as_str(data.get("sleepTimerSetBy"), 128),
                ends_at=int((time.time() + left) * 1000),
                deadline=time.monotonic() + left)
            log(f"sleep timer restored: {left:.0f}s left of {minutes} min")

        for name, section in (("session", session), ("savedQueues", saved_queues),
                              ("mixes", mixes), ("stations", stations),
                              ("lbIndex", lb_index),
                              ("deletedSavedQueues", tombstones), ("devices", devices),
                              ("sleepTimer", sleep_timer)):
            try:
                section()
            except Exception as e:  # noqa: BLE001
                log(f"state: skipped unreadable {name}:", e)
        # A queue bound to a station the file no longer holds continues as nothing.
        if self.session.station_id not in self.stations.stations:
            self.session.station_id = None
        try:
            self._prune_devices()
        except Exception as e:  # noqa: BLE001
            log("state: device pruning failed:", e)

    def _prune_devices(self) -> None:
        """Drop known devices not seen within DEVICE_TTL_DAYS (0 = keep forever).

        Runs on load and on a periodic sweep: the registry accretes a row per device
        that ever connected, so without this a long-lived hub grows a stale picker
        list — most visibly a `cast-<id>` for every speaker the bridging client has
        ever been on the same network as. Never drops an online or active device.
        """
        if DEVICE_TTL_DAYS <= 0:
            return 0
        cutoff = int(time.time() * 1000) - int(DEVICE_TTL_DAYS * 86_400_000)
        stale = [d.id for d in self.devices.values()
                 if d.id != self.session.active_device_id
                 and not d.online and d.last_seen < cutoff]
        for did in stale:
            del self.devices[did]
        if stale:
            log(f"pruned {len(stale)} device(s) not seen in {DEVICE_TTL_DAYS}d")
        return len(stale)

    async def _device_sweep_loop(self) -> None:
        """Expire stale picker rows while the hub runs, not just at startup."""
        while True:
            await asyncio.sleep(DEVICE_SWEEP_INTERVAL)
            try:
                if self._prune_devices():
                    self._save()
                    await self._broadcast_devices()
                self.stations._sweep()  # noqa: SLF001 — same module
            except Exception as e:  # noqa: BLE001 — a sweep must never kill the hub
                log("device sweep error:", e)

    def health(self) -> dict:
        """Liveness snapshot for the HTTP health endpoint.

        Deliberately carries NO session intent (no active device, queue contents or
        play-state): the port is a plain unauthenticated HTTP probe.
        """
        return {
            "status": "ok",
            "uptimeSeconds": int(time.monotonic() - self._started_at),
            "devices": len(self.devices),
            "online": sum(1 for d in self.devices.values() if d.online),
            # The command echo at work (PROTOCOL §5.3). `staleReports` counts
            # reports dropped because they were read before a directive took
            # effect: above zero means the rule is doing its job. `echoBackstops`
            # counts receivers that left a directive unconfirmed for
            # ECHO_BACKSTOP: above zero means one is breaking its contract.
            "staleReports": self._stale_reports,
            "echoBackstops": self._echo_backstops,
        }

    def _state_payload(self) -> tuple[str, Optional[str]]:
        """state.json's text, serialised once, plus the signature of its
        irreplaceable part (see `_durable_signature`)."""
        mixes = self._mixes_list()
        timer = self._sleep
        data = {
            "session": self.session.snapshot(),
            # The sleep timer's wall-clock end (ms), with what a restart needs to
            # show it again; all null when none is set. See `_apply_state`.
            "sleepTimerEndsAt": timer.ends_at if timer else None,
            "sleepTimerMinutes": timer.minutes if timer else None,
            "sleepTimerSetBy": timer.set_by if timer else None,
            "savedQueues": self._saved_queues_list(),
            "deletedSavedQueues": self.deleted_saved_queues,
            "mixes": mixes,
            # The stations (PROTOCOL §18.4), so a restart keeps them. Their play
            # history is NOT here: it has its own file (StationEngine).
            "stations": self.stations.to_state(),
            "lbIndex": self.lb_index,
            "devices": [
                {"id": d.id, "name": d.name, "platform": d.platform, "kind": d.kind,
                 "caps": d.caps, "volume": d.volume, "lastSeen": d.last_seen}
                for d in self.devices.values()
            ],
        }
        return json.dumps(data), _durable_signature(mixes, self.deleted_saved_queues)

    def _save(self, blocking_backup: bool = False) -> None:
        # Synchronous, on the loop, and NOT fsynced — on purpose. This runs on every
        # session broadcast, and a second writer in a thread could land an older
        # payload last. Durability for the part that needs it is the backup's job.
        try:
            # Built inside the try: _saved_queues_list() sorts records, and a persistence
            # failure must not escape into whichever act handler happened to trigger it.
            payload, sig = self._state_payload()
            os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, STATE_PATH)
        except Exception as e:  # noqa: BLE001
            log("failed to save state:", e)
            return
        self._request_backup(sig, payload, blocking=blocking_backup)

    def _request_backup(self, sig: Optional[str], payload: str,
                        blocking: bool = False) -> None:
        """Queue a durable copy of `payload` if its irreplaceable part has changed.

        Called on every save, and a no-op on nearly all of them: position, queue and
        device changes leave the signature alone, so they cost no fsync. Single
        flight, like the play-queue mirror — the newest payload replaces a waiting
        one and one worker does the writing.
        """
        if sig is None or sig == self._backup_sig:
            return
        self._backup_sig = sig
        self._backup_latest = (sig, payload)
        if self._backup_task is not None and not self._backup_task.done():
            self._backup_pending = True  # the running worker picks it up
            return
        if not blocking:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                blocking = True  # no loop (boot, or a test driving _save directly)
        if blocking:
            # Nothing is being served and no worker exists, so write it here.
            self._backup_latest = None
            self._write_backup(sig, payload)
            return
        self._backup_pending = True
        self._backup_task = asyncio.create_task(self._backup_worker())

    def _write_backup(self, sig: str, payload: str) -> None:
        try:
            _write_state_backup_blocking(STATE_PATH + STATE_BACKUP_SUFFIX, payload)
        except Exception as e:  # noqa: BLE001
            self._backup_failed(sig, e)

    def _backup_failed(self, sig: str, err: Exception) -> None:
        log("failed to write state backup:", err)
        # Forget the signature so the next save tries again — unless a newer one
        # has been queued since, in which case that attempt is already on its way.
        if self._backup_sig == sig:
            self._backup_sig = None

    async def _backup_worker(self) -> None:
        try:
            while self._backup_pending:
                self._backup_pending = False
                sig, payload = self._backup_latest
                self._backup_latest = None
                try:
                    await asyncio.to_thread(
                        _write_state_backup_blocking,
                        STATE_PATH + STATE_BACKUP_SUFFIX, payload)
                except Exception as e:  # noqa: BLE001
                    self._backup_failed(sig, e)
        finally:
            self._backup_task = None

    def set_lb_index(self, hint: dict) -> None:
        """Adopt a pushed index head and persist it, throttled (LB_INDEX_SAVE_THROTTLE).
        A new epoch is written at once: that is the change a restarted hub must
        not forget, since it is what tells a client its whole mirror is void."""
        new_epoch = self.lb_index is None or self.lb_index.get("epoch") != hint["epoch"]
        self.lb_index = dict(hint)
        now = time.monotonic()
        if new_epoch or now - self._last_lb_index_save_at >= LB_INDEX_SAVE_THROTTLE:
            self._last_lb_index_save_at = now
            self._save()

    # ----- send helpers ---------------------------------------------------- #
    async def _send(self, dev: Optional[Device], obj: dict) -> None:
        if dev and dev.ws is not None:
            try:
                await dev.ws.send(json.dumps(obj))
            except Exception:  # noqa: BLE001 — drop; close handler will clean up
                pass

    async def _send_required(self, dev: Optional[Device], obj: dict) -> bool:
        """Send a frame whose delivery the caller's correctness depends on.

        `_send` is right for fan-out — one dead observer must not fail a broadcast —
        but a transfer's do:release / do:load are half of a two-phase handoff. Swallowing
        a send error there means the phase can only end by timing out: the hub sits the
        full RELEASE_TIMEOUT or LOAD_TIMEOUT waiting for an answer to a frame that never
        left the process, and logs it as if the receiver were merely slow.
        """
        if dev is None or dev.ws is None:
            log(f"required send '{obj.get('cmd') or obj.get('t')}' dropped: "
                f"{dev.name if dev else '?'} has no socket")
            return False
        try:
            await dev.ws.send(json.dumps(obj))
            return True
        except Exception as e:  # noqa: BLE001
            log(f"required send '{obj.get('cmd') or obj.get('t')}' to {dev.name} failed: {e}")
            return False

    @staticmethod
    def _ack_matches(dev: Device, msg: dict, expected: Optional[str], kind: str) -> bool:
        """Does this transfer acknowledgement belong to the phase we're waiting on?

        A receiver advertising `transferAckV2` must echo the directive's `transferId`,
        and an ack that doesn't match is discarded — that is the whole point of the cap.
        Everyone else is exempt and correlated by device as before: an older client
        never learned to echo anything, and holding it to a rule it can't know about
        would fail every transfer it wins.
        """
        if "transferAckV2" not in (dev.caps or []):
            return True
        got = msg.get("transferId")
        if got == expected:
            return True
        dlog(f"{kind.upper()} from {dev.name}/{dev.id[:8]} IGNORED — "
             f"transferId {got!r}, awaiting {expected!r}")
        return False

    async def _do(self, dev: Optional[Device], cmd: str, required: bool = False,
                  seq: Optional[int] = None, **fields: Any) -> bool:
        """Send one directive. EVERY `do` frame the hub writes is built here.

        That is what makes the echo sound: a directive that changes what a report
        says (ECHO_CMDS) carries a `seq` when its receiver has the cap, and the
        device's need is raised in the same breath, before the frame is written.
        A site that built its own frame would send an unnumbered pause the hub then
        could not tell a stale report from (tools/test_cmd_echo.py greps for one).

        `seq` is a number `_echo_expect` already handed out for this directive.
        `required` is `_send_required`: the caller's next step depends on delivery.
        """
        frame = {"t": "do", "cmd": cmd, **fields}
        if cmd in ECHO_CMDS and dev is not None and dev.ws is not None:
            stamped = self._echo_expect(dev, seq)
            if stamped is not None:
                frame["seq"] = stamped
                dlog(f"DO {cmd} seq={stamped} -> {dev.name}/{dev.id[:8]}")
        if required:
            return await self._send_required(dev, frame)
        await self._send(dev, frame)
        return True

    def _dev(self, device_id: Optional[str]) -> Optional[Device]:
        return self.devices.get(device_id) if device_id else None

    async def _broadcast(self, obj: dict) -> None:
        await asyncio.gather(
            *(self._send(d, obj) for d in self.devices.values() if d.online),
            return_exceptions=True,
        )

    async def _broadcast_session(self) -> None:
        self._save()
        # DIAG (echo): the authoritative state every client is about to receive.
        dlog(f"SESSION -> is_playing={self.session.is_playing} pos={self.session.position_ms} "
             f"idx={self.session.index} active={self.session.active_device_id}")
        frame = {"t": "session", **self._session_snapshot()}
        await asyncio.gather(
            *(self._send_session(d, frame) for d in self.devices.values() if d.online),
            return_exceptions=True,
        )
        self._mirror_play_queue()

    async def _send_session(self, dev: Device, frame: dict) -> None:
        # Noted in the same step as the write (`ws.send` writes before it first
        # suspends), so an ack built later reports a revision already on the wire.
        dev.session_rev_sent = frame["rev"]
        await self._send(dev, frame)

    async def _broadcast_devices(self) -> None:
        await self._broadcast({"t": "devices",
                               "devices": [d.info(self.session.active_device_id)
                                           for d in self.devices.values()]})

    def _mirror_play_queue(self) -> None:
        if not MIRROR_PLAYQUEUE:
            return
        ids = [t.get("id") for t in self.session.queue if t.get("id")]
        current = None
        if 0 <= self.session.index < len(self.session.queue):
            current = self.session.queue[self.session.index].get("id")
        # Record the latest intent; a single debounced worker serializes the
        # actual HTTP writes so rapid seeks/skips can't fire concurrent, out-of-
        # order savePlayQueue calls that persist a stale position.
        self._mirror_latest = (ids, current, self.session.position_ms)
        self._mirror_pending = True
        if self._mirror_task is None or self._mirror_task.done():
            self._mirror_task = asyncio.create_task(self._mirror_worker())

    async def _mirror_worker(self) -> None:
        try:
            while self._mirror_pending:
                self._mirror_pending = False
                await asyncio.sleep(MIRROR_DEBOUNCE)  # coalesce a burst of edits
                ids, current, position = self._mirror_latest
                await asyncio.to_thread(
                    _nd_save_play_queue_blocking, ids, current, position)
        finally:
            self._mirror_task = None

    # ----- saved-queue history (Continue Listening, shared + authoritative) - #
    def _mint_saved_queue_id(self) -> str:
        return f"q_{int(time.time() * 1000)}_{os.urandom(3).hex()}"

    def _saved_queues_list(self) -> list[dict]:
        """The history, newest-first, capped — the exact payload clients render.

        The live session's record is always included even if the cap would push it
        out (a big offline sync can carry newer rows), so clients never end up with
        a session.savedQueueId that matches nothing in the list they render.
        """
        # _as_int on the sort key, not r.get(..., 0): a record whose updatedAt is a
        # string (an older build stored client values unsanitised) would otherwise raise
        # TypeError here — and this runs inside _save(), so one bad row would break
        # persistence for the whole hub.
        ordered = sorted(self.saved_queues.values(),
                         key=lambda r: _as_int(r.get("updatedAt")), reverse=True)
        out = ordered[:SAVED_QUEUES_MAX]
        cur_id = self.session.saved_queue_id
        if cur_id and not any(r.get("id") == cur_id for r in out):
            cur = self.saved_queues.get(cur_id)
            if cur is not None:
                out = out[:SAVED_QUEUES_MAX - 1] + [cur]
        return out

    def _evict_saved_queues(self) -> None:
        if len(self.saved_queues) <= SAVED_QUEUES_MAX:
            return
        keep = {r["id"] for r in self._saved_queues_list()}
        # The record backing the LIVE session is never evictable — dropping it would
        # leave session.savedQueueId dangling and un-highlight the playing queue.
        if self.session.saved_queue_id:
            keep.add(self.session.saved_queue_id)
        dropped = [r for r in self.saved_queues if r not in keep]
        for rid in dropped:
            del self.saved_queues[rid]
        if dropped:
            log(f"saved-queue eviction dropped {len(dropped)} record(s), "
                f"{len(self.saved_queues)} kept")

    def _tombstone(self, rid: str) -> None:
        """Remember a deletion so a client's stale local copy can't re-add it."""
        self.deleted_saved_queues[rid] = int(time.time() * 1000)
        if len(self.deleted_saved_queues) > TOMBSTONE_MAX:
            for old in sorted(self.deleted_saved_queues,
                              key=lambda k: self.deleted_saved_queues[k],
                              )[:len(self.deleted_saved_queues) - TOMBSTONE_MAX]:
                del self.deleted_saved_queues[old]

    def _delete_saved_queue(self, rid: Optional[str]) -> tuple[bool, bool]:
        """Drop a record and remember the deletion. Returns (changed, detached-live-session).

        Tombstoned unconditionally, even when the hub holds no such record: a client can
        delete a row it captured offline before the hub ever merged it, and without the
        tombstone the next syncSavedQueues (from this client or another) re-adds it.

        Callers do the broadcasting, so a batch costs one broadcast rather than N.
        """
        if not rid:
            return (False, False)
        existed = self.saved_queues.pop(rid, None) is not None
        first_time = rid not in self.deleted_saved_queues
        self._tombstone(rid)
        detached = self.session.saved_queue_id == rid
        if detached:
            # Otherwise _sync_current_saved_queue re-creates it on the next queue edit
            # and the deleted row reappears.
            self.session.saved_queue_id = None
        return (existed or detached or first_time, detached)

    # Query params that mark a URL as carrying Subsonic (or plain-password) login,
    # rather than a public asset address — checked by param, not host, so a
    # self-hosted Navidrome under any domain is caught and a public cover (Cover
    # Art Archive, etc.) is never mistaken for one just because of where it lives.
    _CREDENTIAL_PARAMS = frozenset({"u", "t", "s", "p", "apikey"})

    @classmethod
    def _has_credential_params(cls, url: Any) -> bool:
        if not isinstance(url, str) or not url:
            return False
        try:
            keys = {k.lower() for k, _ in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(url).query)}
        except ValueError:
            return True  # unparsable: don't trust it either
        return bool(keys & cls._CREDENTIAL_PARAMS)

    @classmethod
    def _strip_credentialed_cover(cls, url: Optional[str]) -> Optional[str]:
        """Drop a record-level `coverImageUrl` if it carries Subsonic login (B-003,
        second half). Unlike a song's `imageUrl` this has no `ext:` id to key an
        exception on — the queue's cover is stamped from whichever track was first
        at birth, ordinary or preview — so the public/credentialed split is made by
        inspecting the URL itself instead.
        """
        return None if cls._has_credential_params(url) else url

    @classmethod
    def _scrub_history_song(cls, song: dict) -> dict:
        """Drop a track's credentialed Subsonic `streamUrl` AND `imageUrl` before it
        enters PERSISTED history (state.json / the `savedQueues` broadcast). The live
        queue keeps both — a cast bridge fetches `streamUrl` verbatim, and nothing
        needs `imageUrl` scrubbed there either (B-003) — this only touches the copy
        that lands in `self.saved_queues`.

        A preview (`ext:`) track keeps both fields: `streamUrl` is a short-lived
        signed capability minted by `preview_sign`, not a standing Subsonic
        credential, and is the ONLY thing that makes a preview replayable — there's
        no Navidrome id to re-derive it from; `imageUrl` is a public Cover Art
        Archive URL, not a Subsonic address, and is likewise the only source for a
        preview's art (HubManager.remoteTrackToDomainSong's documented exception to
        "never adopt a RemoteTrack.imageUrl"). Both clients re-resolve an ordinary
        track's stream URL AND cover from its id on restore anyway — Feishin never
        renders a saved-queue song's `imageUrl`/a record's `coverImageUrl` as `src`,
        always rebuilding art from the resume track's id with its own credentials
        (`saved-queue-format.ts` `savedQueueCoverSongId`, `saved-queues-route.tsx`);
        Navic does the same and says so explicitly (`HubManager.applySavedQueues`:
        "the record's own coverImageUrl stays ignored: it's the other client's
        authed URL"). So nothing the restore path needs is lost by dropping either
        field for an ordinary track.
        """
        if "streamUrl" not in song and "imageUrl" not in song:
            return song
        sid = song.get("id")
        if isinstance(sid, str) and sid.startswith("ext:"):
            return song
        return {k: v for k, v in song.items() if k not in ("streamUrl", "imageUrl")}

    def _upsert_saved_queue(self, rid: str, kind: str, name: Optional[str],
                            server_id: Optional[str] = None,
                            cover_image_url: Optional[str] = None) -> None:
        """Create/refresh the record for [rid] from the current live queue. Preserves a
        user-assigned name and the original createdAt across refreshes."""
        s = self.session
        if not rid or not s.queue:
            return
        # A deleted record stays deleted. The live device may still be publishing the id
        # it was born with (it only restarts its session when IT did the deleting), so
        # without this a delete from another device is undone by the next report.
        if rid in self.deleted_saved_queues:
            return
        now = int(time.time() * 1000)
        prev = self.saved_queues.get(rid)
        # Kind/name are established when the queue is BORN. A refresh (re-publish of the
        # same id — a reporter republish, or a device adopting then republishing the same
        # queue) must NOT clobber them, so a client republishing needn't know the kind.
        # But an established value only wins if it EXISTS: freezing a null at birth would
        # make the hole permanent, and a client that learns the real name a moment later
        # (Navic publishes before its collection metadata resolves) could never fill it.
        kind_final = (prev or {}).get("sourceKind") or kind or "manual"
        name_final = (prev or {}).get("sourceName") or name
        # Same rule for the card art: frozen at the queue's origin, so it doesn't
        # change as playback moves through the queue. Scrubbed for the same reason
        # as a song's imageUrl (B-003) — `prev`'s copy was already scrubbed when it
        # was first stored, so only a freshly-supplied cover needs checking here.
        cover_final = (prev or {}).get("coverImageUrl") or \
            self._strip_credentialed_cover(cover_image_url)
        self.saved_queues[rid] = {
            "id": rid,
            "serverId": server_id if server_id is not None else (prev or {}).get("serverId"),
            "songs": [self._scrub_history_song(t) for t in s.queue],
            "songCount": len(s.queue),
            "currentIndex": s.index,
            "positionMs": s.position_ms,
            "sourceKind": kind_final,
            "sourceName": name_final,
            "coverImageUrl": cover_final,
            "name": (prev or {}).get("name"),   # user rename survives a refresh
            "shuffle": s.shuffle,
            "repeat": s.repeat,
            "createdAt": (prev or {}).get("createdAt", now),
            "updatedAt": now,
        }
        # Reflect the (possibly preserved) kind/name back onto the live session.
        s.source_kind = kind_final
        s.source_name = name_final
        self._evict_saved_queues()

    def _sync_current_saved_queue(self) -> None:
        """Mirror the (possibly grown/reordered) live queue into the current record — the
        'the current queue updates dynamically' path for enqueue/remove/move."""
        s = self.session
        rid = s.saved_queue_id
        if not rid:
            return
        rec = self.saved_queues.get(rid)
        if rec is None:
            self._upsert_saved_queue(rid, s.source_kind, s.source_name)
            return
        rec["songs"] = [self._scrub_history_song(t) for t in s.queue]
        rec["songCount"] = len(s.queue)
        rec["currentIndex"] = s.index
        rec["positionMs"] = s.position_ms
        rec["updatedAt"] = int(time.time() * 1000)

    def _touch_saved_queue_progress(self) -> None:
        """Cheap cursor update so a queue that later becomes 'previous' resumes at the
        right spot. In-memory; persisted by the next _save()."""
        rid = self.session.saved_queue_id
        rec = self.saved_queues.get(rid) if rid else None
        if rec is None:
            return
        rec["currentIndex"] = self.session.index
        rec["positionMs"] = self.session.position_ms
        rec["updatedAt"] = int(time.time() * 1000)

    # Fields a client is allowed to contribute through syncSavedQueues. Anything else
    # (including a stray `token`) is dropped rather than stored and rebroadcast.
    SQ_FIELDS = ("id", "serverId", "songs", "songCount", "currentIndex", "positionMs",
                 "sourceKind", "sourceName", "coverImageUrl", "name", "shuffle",
                 "shuffleMode", "repeat", "createdAt", "updatedAt")

    # Same idea one level down: a client-supplied *track* is whitelisted too, so a song
    # object can't smuggle extra keys into state.json and out to every other device.
    SQ_TRACK_FIELDS = ("id", "serverId", "title", "artist", "album", "durationMs",
                       "coverArtId", "imageUrl", "streamUrl", "mime")

    @classmethod
    def _sanitize_saved_queue(cls, raw: Any,
                              songs_max: int = SAVED_QUEUE_SONGS_MAX) -> Optional[dict]:
        """A client-supplied history record, coerced into the shape the hub stores.

        Returns None for anything unusable. Everything that survives is safe to sort,
        serialize and rebroadcast — which matters because these records are persisted
        and fanned out to devices that never saw the sender.
        """
        if not isinstance(raw, dict):
            return None
        rid = _as_str(raw.get("id"), 128)
        songs_raw = raw.get("songs")
        if not rid or not isinstance(songs_raw, list) or not songs_raw:
            return None
        # Scrubbed here too: this path also re-sanitises `_load()`'s state.json read, so
        # a file an older build wrote with credentials in history gets cleaned on load
        # (B-003's migration — cheap because every persisted record already flows
        # through here).
        songs = [
            cls._scrub_history_song({k: t[k] for k in cls.SQ_TRACK_FIELDS if k in t})
            for t in songs_raw[:songs_max] if isinstance(t, dict)
        ]
        if not songs:
            return None
        repeat = raw.get("repeat")
        rec: dict = {
            "id": rid,
            "songs": songs,
            "songCount": len(songs),
            "currentIndex": _as_int(raw.get("currentIndex"), 0, 0, len(songs) - 1),
            "positionMs": _as_int(raw.get("positionMs"), 0, 0),
            "sourceKind": _as_str(raw.get("sourceKind"), 32) or "manual",
            "shuffle": bool(raw.get("shuffle")),
            "repeat": repeat if repeat in ("none", "one", "all") else "none",
            "createdAt": _as_int(raw.get("createdAt"), 0, 0),
            "updatedAt": _as_int(raw.get("updatedAt"), 0, 0),
        }
        # Optional strings: absent rather than null, so the merge's "a hole can be
        # filled, an established value can't be blanked" rule reads naturally.
        for k in ("serverId", "sourceName", "coverImageUrl", "name", "shuffleMode"):
            v = _as_str(raw.get(k))
            if k == "coverImageUrl":
                v = cls._strip_credentialed_cover(v)
            if v is not None:
                rec[k] = v
        return rec

    def _merge_saved_queues(self, incoming: list[dict]) -> bool:
        """Union-merge client-supplied offline history by id (newest updatedAt wins),
        field by field. Returns True if anything changed.

        Field-level rather than wholesale replace: a client's copy of a record can be
        missing metadata the hub has (name/sourceName/cover), and a newer updatedAt
        from an unrelated edit shouldn't erase it.
        """
        changed = False
        # One client's offline history can't exceed the whole cap: accepting twice the
        # cap let a single reconnecting device evict every record the other device is
        # looking at, including the one its UI was highlighting.
        for raw in incoming[:SAVED_QUEUES_MAX]:
            rec = self._sanitize_saved_queue(raw)
            if rec is None:
                continue
            rid = rec["id"]
            if rid in self.deleted_saved_queues:
                continue  # deleted on the hub; the client's copy is stale
            cur = self.saved_queues.get(rid)
            # The live session's record is decided FIRST, before the insert branch below:
            # after a hub restart that kept session.savedQueueId but lost the record, an
            # `cur is None` insert would let a stale client copy become the playing queue.
            if rid == self.session.saved_queue_id:
                if cur is None:
                    if self.session.queue:
                        # Rebuild from what is actually playing, not from the client copy.
                        self._upsert_saved_queue(rid, self.session.source_kind,
                                                 self.session.source_name)
                        cur = self.saved_queues.get(rid)
                        changed = changed or cur is not None
                    else:
                        self.saved_queues[rid] = rec
                        changed = True
                        continue
                if cur is None:
                    continue
                # Metadata-only reconciliation: never let an offline copy overwrite the
                # songs/cursor of the queue that's playing right now. Holes get filled,
                # and a genuinely newer `name` is accepted — an offline rename of the
                # CURRENT queue is otherwise the one edit that could never sync back.
                for k in ("sourceName", "name", "coverImageUrl", "serverId"):
                    if not cur.get(k) and rec.get(k):
                        cur[k] = rec[k]
                        changed = True
                if rec.get("name") and rec["name"] != cur.get("name") and \
                        rec.get("updatedAt", 0) > cur.get("updatedAt", 0):
                    cur["name"] = rec["name"]
                    changed = True
                continue
            if cur is None:
                self.saved_queues[rid] = rec
                changed = True
                continue
            if rec.get("updatedAt", 0) <= cur.get("updatedAt", 0):
                # Older overall, but it may still fill holes the hub has.
                for k in ("sourceName", "name", "coverImageUrl", "serverId"):
                    if cur.get(k) in (None, "") and rec.get(k):
                        cur[k] = rec[k]
                        changed = True
                continue
            merged = dict(cur)
            merged.update(rec)
            # Never let a newer-but-emptier copy blank out established metadata.
            for k in ("sourceName", "name", "coverImageUrl", "serverId"):
                if not merged.get(k) and cur.get(k):
                    merged[k] = cur[k]
            self.saved_queues[rid] = merged
            changed = True
        if changed:
            self._evict_saved_queues()
        return changed

    async def _broadcast_saved_queues(self) -> None:
        self._save()
        await self._broadcast({"t": "savedQueues", "queues": self._saved_queues_list()})

    # ----- "Mixed for You" (recipes, regenerated client-side) --------------- #
    #
    # The hub stores `{kind, seedId, moodCharacter, count}` and broadcasts it. It
    # never generates a track from one: each client regenerates locally with the
    # engine it already has (Navic's RadioManager, Feishin's auto-dj/*), which is
    # what keeps this process audio-free and AudioMuse-free.
    #
    # That is also the whole difference from a saved queue, and the gap this closes:
    # a saved queue stores a RESULT and replays it; a mix stores a RECIPE and
    # regenerates. Nothing in either client persisted one before — RadioManager took
    # these values as arguments and dropped them, and `playMix` exited into a frozen
    # SavedQueueEntity.
    def _mint_mix_id(self) -> str:
        return f"mx_{int(time.time() * 1000)}_{os.urandom(3).hex()}"

    @staticmethod
    def _sanitize_mix(raw: Any) -> Optional[dict]:
        """A client-supplied recipe, coerced into the shape the hub stores.

        Returns None for anything unusable. Same contract as `_sanitize_saved_queue`
        and for the same reason: these records are persisted and fanned out to
        devices that never saw the sender, so a bad value would land in state.json
        and break every later `_save()`.
        """
        if not isinstance(raw, dict):
            return None
        mid = _as_str(raw.get("id"), 128)
        if not mid:
            return None
        kind = _as_str(raw.get("kind"), 32)
        if kind not in MIX_KINDS:
            return None
        name = (_as_str(raw.get("name")) or "").strip()
        if not name:
            return None
        now = int(time.time() * 1000)
        rec: dict = {
            "id": mid,
            "name": name,
            "kind": kind,
            "count": _as_int(raw.get("count"), 50, 1, MIX_COUNT_MAX),
            "createdAt": _as_int(raw.get("createdAt"), now, 0),
            "updatedAt": _as_int(raw.get("updatedAt"), now, 0),
            "lastPlayedAt": _as_int(raw.get("lastPlayedAt"), 0, 0),
        }
        # Optional strings: absent rather than null, matching the saved-queue rule.
        # `moodCharacter` is validated against the three AudioMuse presets rather
        # than merely length-capped — it names a generator on the client, and an
        # unknown one is a recipe no client can run.
        for k in ("seedId", "seedName", "coverArtId"):
            v = _as_str(raw.get(k))
            if v:
                rec[k] = v
        # `character` (§18) is the station character; a record without one (a
        # client from before stations) has it derived from `moodCharacter`, and a
        # record with one has the legacy name written beside it, so that client
        # still reads `moodCharacter`. When both arrive, `character` wins.
        mood = _as_str(raw.get("moodCharacter"), 32)
        character = _as_str(raw.get("character"), 16)
        if character not in STATION_CHARACTERS:
            character = STATION_FROM_LEGACY.get(mood) if mood in MIX_MOODS else None
        if character:
            rec["character"] = character
            rec["moodCharacter"] = STATION_LEGACY[character]
        return rec

    def _mixes_list(self) -> list[dict]:
        """The recipes, newest-updated first, capped — the payload clients render."""
        ordered = sorted(self.mixes.values(),
                         key=lambda r: _as_int(r.get("updatedAt")), reverse=True)
        return ordered[:MIXES_MAX]

    def _evict_mixes(self) -> None:
        if len(self.mixes) <= MIXES_MAX:
            return
        keep = {r["id"] for r in self._mixes_list()}
        dropped = [mid for mid in self.mixes if mid not in keep]
        for mid in dropped:
            del self.mixes[mid]
        if dropped:
            log(f"mix eviction dropped {len(dropped)} recipe(s), {len(self.mixes)} kept")

    def _save_mix(self, msg: dict) -> Optional[str]:
        """Create or update one recipe from an act. Returns its id, or None if unusable.

        An absent `id` mints one — that is the create path. An `id` naming a recipe
        the hub does not hold is NOT an error: a client can have minted it offline,
        and refusing would leave the two permanently disagreeing with nothing to
        notice it (the failure mode `renameSavedQueue` answers with an error for).
        """
        mid = _as_str(msg.get("id"), 128) or self._mint_mix_id()
        existing = self.mixes.get(mid)
        now = int(time.time() * 1000)
        rec = self._sanitize_mix({
            **msg, "id": mid,
            # createdAt and lastPlayedAt are the hub's to keep: a client re-saving a
            # recipe sends the recipe, not its history, and taking its word would
            # reset the age of a mix every time its count was edited.
            "createdAt": (existing or {}).get("createdAt", now),
            "lastPlayedAt": (existing or {}).get("lastPlayedAt", 0),
            "updatedAt": now,
        })
        if rec is None:
            return None
        self.mixes[mid] = rec
        self._evict_mixes()
        return mid

    def _rename_mix(self, mid: Optional[str], name: Optional[str]) -> bool:
        rec = self.mixes.get(mid) if mid else None
        clean = (name or "").strip()
        if rec is None or not clean:
            return False
        rec["name"] = clean
        rec["updatedAt"] = int(time.time() * 1000)
        return True

    def _delete_mix(self, mid: Optional[str]) -> bool:
        if not mid:
            return False
        return self.mixes.pop(mid, None) is not None

    def _touch_mix(self, mid: Optional[str]) -> bool:
        """Record that a recipe was just played. Bumps `lastPlayedAt` ONLY.

        Not `updatedAt`: that is the eviction and sort key, and folding "played"
        into it would make a mix the user listens to outrank one they just edited,
        and reorder the list under them every time they pressed play.
        """
        rec = self.mixes.get(mid) if mid else None
        if rec is None:
            return False
        rec["lastPlayedAt"] = int(time.time() * 1000)
        return True

    async def _broadcast_mixes(self) -> None:
        self._save()
        await self._broadcast({"t": "mixes", "mixes": self._mixes_list()})

    # ----- queue / order maths --------------------------------------------- #
    def _clamp_index(self, i: int) -> int:
        """Keep a client-supplied index inside the queue (0 for an empty queue)."""
        return max(0, min(i, len(self.session.queue) - 1)) if self.session.queue else 0

    def _play_order(self) -> list[int]:
        n = len(self.session.queue)
        if self.session.order and len(self.session.order) == n:
            return self.session.order
        return list(range(n))

    def _rebuild_order(self) -> None:
        """Shuffle queue indices from scratch, keeping the current track first.

        Only for setQueue / shuffle-toggle. Plain queue edits (enqueue/remove/
        move) must NOT reshuffle — they patch `order` incrementally below so the
        user's upcoming shuffled order is preserved.
        """
        n = len(self.session.queue)
        if not self.session.shuffle or n == 0:
            self.session.order = None
            return
        rest = [i for i in range(n) if i != self.session.index]
        random.shuffle(rest)
        self.session.order = [self.session.index] + rest

    def _order_after_insert(self, at: int, count: int, play_next: bool) -> None:
        """Patch shuffle order for `count` items inserted at raw position `at`."""
        order = self.session.order
        if order is None or count <= 0:
            return  # sequential order; nothing to track
        new_order = [v + count if v >= at else v for v in order]
        new_vals = list(range(at, at + count))
        if play_next:
            try:
                pos = new_order.index(self.session.index) + 1
            except ValueError:
                pos = len(new_order)
            new_order[pos:pos] = new_vals
        else:
            new_order.extend(new_vals)
        self.session.order = new_order

    def _order_after_remove(self, at: int) -> None:
        """Patch shuffle order for the item removed at raw position `at`."""
        order = self.session.order
        if order is None:
            return
        patched = [v - 1 if v > at else v for v in order if v != at]
        self.session.order = patched or None

    def _order_after_move(self, fr: int, to: int) -> None:
        """Patch shuffle order for a raw move (queue.insert(to, queue.pop(fr)))."""
        order = self.session.order
        if order is None:
            return

        def remap(v: int) -> int:
            if v == fr:
                return to
            if fr < to and fr < v <= to:
                return v - 1
            if to <= v < fr:
                return v + 1
            return v

        self.session.order = [remap(v) for v in order]

    def _step_index(self, delta: int) -> Optional[int]:
        """Next/previous queue index respecting repeat + shuffle order."""
        order = self._play_order()
        if not order:
            return None
        if self.session.repeat == "one":
            return self.session.index
        try:
            pos = order.index(self.session.index)
        except ValueError:
            pos = 0
        nxt = pos + delta
        if nxt < 0 or nxt >= len(order):
            if self.session.repeat == "all":
                nxt %= len(order)
            else:
                return None  # ran off the end
        return order[nxt]

    # ----- connection lifecycle -------------------------------------------- #
    @staticmethod
    async def _close(ws: Any, code: int, reason: str) -> None:
        """Close a socket, tolerating a peer that has already gone.

        `ws.close()` writes a close frame and drains, so it raises ConnectionClosedError
        when the peer vanished first — which is the norm for the codes we use it with
        (a client that sent garbage and hung up, a superseded socket). Raised from
        inside an `except` block it escapes the handler entirely, and `websockets`
        logs it as an unhandled error in the connection handler: a scary traceback for
        the most routine thing a network can do.
        """
        try:
            await ws.close(code, reason)
        except Exception:  # noqa: BLE001 — closing is best-effort by definition
            pass

    async def handler(self, ws: Any) -> None:
        dev: Optional[Device] = None
        # PROTOCOL §2: clients connect to /connect. `/` is still accepted (older
        # builds of both clients used it) but logged so it can be retired.
        path = (getattr(ws, "request", None) and ws.request.path) or getattr(ws, "path", "/")
        path = (path or "/").split("?")[0].rstrip("/") or "/"
        if path not in ("/connect", "/"):
            await self._close(ws, 4004, "bad path")
            return
        if path == "/":
            dlog("client connected on deprecated path '/'; use /connect")
        try:
            # First frame MUST be hello + valid token.
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                await self._close(ws, 4002, "protocol")
                return
            token_ok = not TOKEN or hmac.compare_digest(str(msg.get("token") or ""), TOKEN)
            if msg.get("t") != "hello" or not token_ok:
                got = str(msg.get("token") or "")
                name = (msg.get("device") or {}).get("name", "?")
                # Length only, never a prefix of what was supplied. The prefix was there to
                # catch a quoted value from docker --env-file, and the length difference it
                # produces says the same thing without putting four characters of somebody's
                # secret in a log file that gets pasted into bug reports.
                log(f"AUTH REJECTED for {name!r}: supplied token has length {len(got)}, "
                    f"expected {len(TOKEN)} — check HUB_TOKEN "
                    f"(note: docker --env-file does NOT strip quotes, which shows up here "
                    f"as a length two greater than expected)")
                await ws.send(json.dumps({"t": "error", "code": "auth", "message": "bad token"}))
                await self._close(ws, 4001, "auth")
                return

            # Before registering, so a device is never half-announced while its
            # welcome waits. A no-op once the hub's first lb-bot probe is in.
            await LB.wait_first_probe(LB_PROBE_STARTUP_WAIT)
            # NOTHING may suspend between here and the write of `welcome`. _register
            # adopts the socket, so from that line every directive for this device
            # is written to it, and `welcome` has to be the first frame on it: it is
            # where both sides zero the command echo (PROTOCOL §5.2), and a `do`
            # ahead of it would be numbered for a connection that has not begun.
            # `ws.send` writes its frame before it first suspends.
            dev, arrival = self._register(msg.get("device", {}), ws)
            welcome = {
                "t": "welcome",
                "deviceId": dev.id,
                "features": list(HUB_FEATURES),
                "session": self._session_snapshot(),
                "savedQueues": self._saved_queues_list(),
                "mixes": self._mixes_list(),
                "devices": [d.info(self.session.active_device_id) for d in self.devices.values()],
                # lb-bot's availability and the routes this hub proxies — the
                # `/lb/status` answer, delivered without a client awaiting it.
                "lb": LB.welcome_info(),
                # The station the session is bound to (§18.2), or null: a device
                # joining mid-station learns `after` without a round trip.
                "station": self.stations.welcome_station(),
            }
            if self.lb_index is not None:
                welcome["lbIndex"] = dict(self.lb_index)
            self._echo_reset(dev)
            dev.session_rev_sent = welcome["session"]["rev"]
            await self._send(dev, welcome)
            await self._broadcast_devices()
            log(f"+ {dev.name} ({dev.id[:8]}) connected; caps={dev.caps}; {arrival}")

            async for raw in ws:
                if dev.ws is not ws:
                    # Superseded: a newer socket registered this id and this one is
                    # being closed in the background. What it still delivers was
                    # sent on a connection the hub has moved on from. A report on it
                    # carries the old connection's `applied`, and a `released` or
                    # `loaded` would answer a question now put to the new socket;
                    # those fall to their timeouts.
                    dlog(f"frame from a superseded socket of {dev.name}/{dev.id[:8]} dropped")
                    continue
                try:
                    await self._on_message(dev, json.loads(raw))
                except Exception as e:  # noqa: BLE001 — never let one bad frame kill the socket
                    log("message error:", e)
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await self._close(ws, 4002, "protocol")
        except websockets.ConnectionClosed:
            pass
        finally:
            if dev:
                await self._disconnect(dev, ws)

    def _register(self, desc: dict, ws: Any) -> tuple[Device, str]:
        """Bind `ws` to the device `desc` names. Returns it, and a note for the log
        on where it came from (new, back after N s, replacing a live socket).

        Not a coroutine, on purpose: see the caller. Nothing here may wait.
        """
        did = desc.get("id") or os.urandom(8).hex()
        dev = self.devices.get(did)
        now_ms = int(time.time() * 1000)
        if dev is None:
            dev = Device(id=did)
            self.devices[did] = dev
            arrival = "new id"
        else:
            # How long this id was gone, for the question I-035 asks of the log:
            # does a dropped active device come back within a few seconds? Measured
            # from its last frame, which for a clean disconnect is the disconnect.
            away = max(0, now_ms - dev.last_seen) / 1000 if dev.last_seen else None
            arrival = f"away {away:.1f}s" if away is not None else "away for an unknown time"
        # Evict a prior live socket for this device before adopting the new one.
        # Without this, a reconnect while the old WS is still half-open leaves
        # two sockets bound to one Device, both driving session state.
        old_ws = dev.ws
        dev.name = desc.get("name", dev.name)
        dev.platform = desc.get("platform", dev.platform)
        dev.caps = desc.get("caps", dev.caps)
        # A `kind` that does not have the shape is dropped, which reads as "not
        # given", and a hello that gives none leaves the stored one alone, like
        # `name` and `platform` above: the hardware is still what it was.
        kind = _as_kind(desc.get("kind"))
        if kind is not None:
            dev.kind = kind
        # A bridged device (a Chromecast) is claimed on behalf of another client.
        # Recording who holds it lets a picker say "bridged by the desktop" and lets
        # a would-be second bridge see the slot is taken (PROTOCOL §12.2).
        bridged_by = desc.get("bridgedBy")
        dev.bridged_by = str(bridged_by) if bridged_by else None
        # A fresh registration says nothing about the speaker yet — the bridge asserts
        # that separately with `deviceState`. Starting from unknown rather than keeping
        # the previous socket's verdict stops a stale "up" surviving a bridge restart.
        dev.reachable = None
        dev.reachable_at = 0
        dev.app_running = None
        dev.online = True
        # Adopt the new socket BEFORE closing the old one: _disconnect bails unless
        # it owns dev.ws, so claiming it first is what stops the superseded socket's
        # teardown from marking the device offline / clearing the active slot.
        dev.ws = ws
        if old_ws is not None and old_ws is not ws:
            # Closed in the background. `close()` waits for the peer's close frame,
            # and the socket being replaced is typically one the peer can no longer
            # hear (Wi-Fi to mobile, a laptop lid): awaited here, a device coming
            # back behind such a socket got its `welcome` 20 s late.
            self._spawn(self._close(old_ws, 4003, "superseded"))
            arrival += ", replacing a live socket"
            if self.session.active_device_id == dev.id:
                arrival += " (the active device)"
        dev.last_seen = now_ms
        self._save()
        return dev, arrival

    async def _disconnect(self, dev: Device, ws: Any) -> None:
        # Only tear down if THIS socket is still the device's live socket. On a
        # reconnect blip the new connection may have already re-registered
        # (dev.ws = new_ws) before this old socket's finally fires; nulling it
        # here would kill the live new socket and spuriously mark the device
        # offline (and pause/relinquish active if it was the active receiver).
        if dev.ws is not ws:
            return
        dev.online = False
        dev.ws = None
        dev.last_seen = int(time.time() * 1000)
        # Reachability was the bridge's claim; with the bridge gone nobody is making it.
        dev.reachable = None
        dev.reachable_at = 0
        dev.app_running = None
        dev.bridged_by = None
        # Nothing reports on this connection again; stop its backstop clock. (The
        # echo's own reset point is the next `welcome`.)
        self._echo_reset(dev)
        # Whether it held the session is the other half of what I-035 needs from the
        # log (the `+` line says how long an id was away).
        held = ""
        if self.session.active_device_id == dev.id:
            held = (" (was the active device, "
                    f"{'playing' if self.session.is_playing else 'paused'})")
        log(f"- {dev.name} ({dev.id[:8]}) disconnected{held}")
        # If the active receiver dropped, pause the session AND relinquish the active
        # slot (keep the queue/position). Clearing active_device_id is the "no live
        # receiver" signal controllers use to adopt the last-known queue locally
        # (paused) so a still-open client isn't stranded mirroring a dead device. A
        # device that is genuinely still playing re-claims active via its reporter on
        # reconnect, so this doesn't disrupt a brief network blip.
        if self.session.active_device_id == dev.id:
            # Flush the resume cursor unthrottled: this device's last report IS the
            # final position, and after the clear below nothing else will write it.
            self._touch_saved_queue_progress()
            self.session.is_playing = False
            self.session.set_active(None)
            self.session.bump()
            await self._broadcast_session()
        await self._broadcast_devices()

    # ----- message dispatch ------------------------------------------------ #
    async def _on_message(self, dev: Device, msg: dict) -> None:
        t = msg.get("t")
        dev.last_seen = int(time.time() * 1000)
        if t == "act":
            await self._on_act(dev, msg)
        elif t == "deviceState":
            await self._on_device_state(dev, msg)
        elif t == "loaded":
            fut = dev.load_future
            if fut is None or fut.done():
                dlog(f"LOADED from {dev.name}/{dev.id[:8]} ignored — nothing pending")
            elif not self._ack_matches(dev, msg, dev.load_tid, "loaded"):
                pass
            else:
                if not bool(msg.get("ok", True)):
                    # The waiter this wakes rolls the slot back, but only on a later
                    # turn of the loop, and a `report` queued right behind this frame
                    # is read first: the device is still active, so the state of a
                    # player that failed to start (index 0, 0 ms) would overwrite the
                    # resume point the rollback is about to hand back. De-authorize
                    # it now, as `released` does below; _await_load_ack lifts it.
                    dev.load_refused = fut
                fut.set_result(msg)
        elif t == "report":
            await self._on_report(dev, msg)
        elif t == "released":
            # The released frame may carry the authoritative final position —
            # apply it atomically, AND de-authorize the device immediately so a
            # straggler report (e.g. a cast device's stop() emitting position 0)
            # can't clobber the resume point before the transfer completes.
            # Only the current active device (or one we're actively awaiting a
            # handoff from) may speak here: a late/duplicate `released` from a
            # device that handed off two transfers ago would otherwise rewind the
            # session to that device's ancient position.
            fut = dev.release_future
            authoritative = self.session.active_device_id == dev.id or fut is not None
            if not authoritative:
                dlog(f"RELEASED from {dev.name}/{dev.id[:8]} IGNORED (not active, no pending release)")
                return
            # Being the active device isn't enough on its own. A duplicate `released`
            # from a handoff two transfers ago arrives from a device that is active
            # again, and would complete the release future armed for the CURRENT
            # handoff — finishing phase one on the strength of an old answer.
            if fut is not None and not self._ack_matches(dev, msg, dev.release_tid, "released"):
                return
            changed = False
            if "positionMs" in msg:
                pos = max(0, int(msg["positionMs"]))
                if pos != self.session.position_ms:
                    self.session.position_ms = pos
                    changed = True
            if "index" in msg:
                idx = self._clamp_index(int(msg["index"]))
                if idx != self.session.index:
                    self.session.index = idx
                    changed = True
            if self.session.active_device_id == dev.id:
                self.session.set_active(None)
                changed = True
            if fut and not fut.done():
                fut.set_result(True)
            elif changed:
                # No transfer in flight to broadcast on our behalf — publish it.
                self.session.bump()
                await self._broadcast_session()
                await self._broadcast_devices()
        elif t == "ping":
            await self._send(dev, {"t": "pong"})
        # 'do'/'session'/'progress' are hub-authored; ignore if a client sends them.

    async def _on_report(self, dev: Device, msg: dict) -> None:
        # `applied` is read from every report, also one that is about to be ignored:
        # a receiver confirms a directive whether or not it holds the session, and
        # the backstop clock must hear it.
        echo = ECHO_CAP in (dev.caps or [])
        applied = _as_int(msg.get("applied"), 0, lo=0) if echo else 0
        if echo:
            self._echo_confirm(dev, applied)
        # Only the active receiver is authoritative for live playback truth.
        if dev.id != self.session.active_device_id:
            return
        s = self.session
        who = f"{dev.name}/{dev.id[:8]}"
        if dev.load_refused is not None:
            # It answered a transfer's load with ok:false and the rollback is on its
            # way (see the `loaded` branch of _on_message). Not believed until then.
            dlog(f"REPORT from {who} DROPPED(load refused, rollback pending)")
            return
        # The command echo. `exact` = this report was read from the player after
        # every directive the hub has sent it took effect, by the receiver's own
        # word (PROTOCOL §5.3), so index and play state need no clock.
        exact = False
        if echo:
            if applied >= dev.echo_need:
                exact = True
            elif not dev.echo_backstop:
                # Read before a directive took effect: every field in it may be the
                # state just left. Dropped whole; the receiver owes a fresh report
                # the moment `applied` moves.
                self._stale_reports += 1
                if not dev.echo_sent:
                    # Nothing is timing this wait (the clock for that directive was
                    # stopped while the device did not hold the session). A drop
                    # must always have the backstop behind it.
                    dev.echo_sent.append((dev.echo_need, time.monotonic()))
                    self._echo_arm(dev)
                dlog(f"REPORT from {who} DROPPED(stale: applied={applied} < "
                     f"need={dev.echo_need}) idx={msg.get('index')} "
                     f"pos={msg.get('positionMs')} isPlaying={msg.get('isPlaying')}")
                return
            # else: past the backstop. This receiver has left a directive unconfirmed
            # for ECHO_BACKSTOP, so it is judged by the clocks below like a device
            # without the cap until `applied` reaches `need`.
        changed = False
        now = time.monotonic()
        # Where the track was before this report: a report that moves the index
        # already carries the NEW track's position (PROTOCOL §18.3).
        left_at = s.position_ms
        if "positionMs" in msg:
            # Right after a seek/jump/skip the receiver may still emit an in-flight
            # 1 Hz report carrying the OLD position, which would rewind the scrubber.
            # Within the grace window, reject a report that sits closer to where we
            # just left than to where we intend to be — that's a stale tick. A report
            # near (or past) the new target is real progress and is accepted.
            #
            # Kept for a `cmdEcho` device too. It guards something the echo cannot
            # see: a position POLL inside the receiver that straddles the seek, so a
            # report sent after the seek took effect still carries the old position.
            report_pos = max(0, int(msg["positionMs"]))
            target = s.position_ms
            # "Stale" means the report still sits essentially AT the position we just
            # left. A relative compare (closer-to-pre than to-target) wrongly rejected
            # legitimate reports after a backward seek, where real progress from the
            # new target is still nearer the old spot than the target is.
            stale = (now - self._position_intent_at < INTENT_GRACE
                     and abs(report_pos - self._pre_intent_position) < 1500
                     and abs(report_pos - target) >= 1500)
            if not stale:
                s.position_ms = report_pos
            else:
                dlog(f"REPORT pos={report_pos} from {who} "
                     f"IGNORED(pos-grace; pre={self._pre_intent_position} target={target})")
        if "index" in msg:
            idx = self._clamp_index(int(msg["index"]))
            if idx != s.index:
                # The mirror of the position rule, for a report judged by the clock:
                # inside the grace after a skip or jump to another index, one that
                # still names the index just left is a tick sampled before the skip.
                left_behind = (not exact
                               and now - self._index_intent_at < INTENT_GRACE
                               and idx == self._pre_intent_index)
                if left_behind:
                    dlog(f"REPORT idx={idx} from {who} "
                         f"IGNORED(index-grace; session is at {s.index})")
                else:
                    self._judge_departure(s.index, idx, left_at)
                    s.index = idx; changed = True
        if "isPlaying" in msg and msg["isPlaying"] != s.is_playing:
            playing = bool(msg["isPlaying"])
            if playing and not s.queue:
                # Nothing is queued, so nothing can be playing, whoever says so and
                # whenever: a receiver whose `clear` empties its queue without
                # pausing keeps reporting "playing" over an empty session.
                verdict = "IGNORED(empty queue)"
            elif exact:
                verdict = "APPLIED(echo)"
                s.is_playing = playing; changed = True
            # A report may have been sent BEFORE the receiver processed a fresh
            # play/pause command — accepting it would flip the user's intent
            # back (and the next transfer would then carry the wrong state).
            elif now - self._play_intent_at < INTENT_GRACE:
                verdict = "IGNORED(grace)"
            else:
                verdict = "APPLIED"
                s.is_playing = playing; changed = True
            # DIAG (echo): a report that contradicts current play-state.
            dlog(f"REPORT from {who} isPlaying={msg.get('isPlaying')} "
                 f"pos={msg.get('positionMs')} applied={msg.get('applied')} "
                 f"| is_playing was {not playing} {verdict}")

        if msg.get("ended"):
            # The whole queue finished: its last track was played through. Judged
            # once per position, however many reports repeat the flag.
            mark = (s.saved_queue_id, s.index)
            if self._ended_judged != mark:
                self._judge_departure(s.index, None, s.position_ms, ended=True)
                self._ended_judged = mark
            self.session.is_playing = False
            changed = True

        # Keep the current history record's resume cursor fresh (in-memory; the
        # _save() below or the next setQueue switch persists it) so a queue that
        # later becomes "previous" resumes where the user actually left off.
        if now - self._last_sq_progress_at >= SQ_PROGRESS_THROTTLE:
            self._last_sq_progress_at = now
            self._touch_saved_queue_progress()

        if changed:
            self.session.bump()
            await self._broadcast_session()
        else:
            # Position-only ticks don't bump/broadcast the session, so without this the
            # persisted position only advances on the next real state change — a hub
            # kill mid-track would resume from wherever the track started.
            if now - self._last_position_save_at >= POSITION_SAVE_THROTTLE:
                self._last_position_save_at = now
                self._save()
            # The throttle is for the 1 Hz stream of a PLAYING receiver. A paused one
            # sends a single report of where it actually stopped and then goes quiet;
            # throttling that one away (it lands well inside a second of the last
            # tick) left every controller up to ~1 s off until playback resumed.
            if (not self.session.is_playing
                    or now - self._last_progress_sent >= PROGRESS_THROTTLE):
                self._last_progress_sent = now
                await self._broadcast({"t": "progress",
                                       "positionMs": self.session.position_ms,
                                       "index": self.session.index,
                                       "isPlaying": self.session.is_playing})

    async def _on_act(self, dev: Device, msg: dict) -> None:
        """Route one act: run it now, or queue it behind the transfer in flight.

        A transfer is never awaited here. It waits for the old device's `released`
        frame, and the overwhelmingly common case is the active receiver
        transferring away from itself: same socket, so awaiting inside its read
        loop means the hub cannot read the very frame it is waiting for. That
        deadlock always ended in a RELEASE_TIMEOUT and a resume up to one report
        interval (~1 s) stale. For the same reason an act that has to wait for a
        transfer is queued for the task that carries it out, not awaited: the
        socket it came from may be the one the transfer is listening to.

        The hold lasts from the transfer act's arrival until the queue is empty
        again, so everything held runs in arrival order, also relative to an act
        that arrives while the held ones are being run.
        """
        act = Act(dev, msg)
        if act.action == "transfer" or (self._drain_task is not None
                                        and act.action in HELD_ACTIONS):
            if act.action != "transfer":
                dlog(f"ACT {act.action} from {dev.name}/{dev.id[:8]} HELD behind a "
                     f"transfer ({len(self._held)} ahead of it)")
            self._held.append(act)
            if self._drain_task is None:
                self._drain_task = asyncio.create_task(self._drain_held())
            return
        await self._run_act(act)

    async def _drain_held(self) -> None:
        try:
            while self._held:
                act = self._held.popleft()
                try:
                    if act.stale:
                        # Same rule as a frame still arriving on a superseded
                        # socket: it was sent on a connection that is over. (A held
                        # `play` would otherwise promote a device that is gone.)
                        dlog(f"held ACT {act.action} from {act.dev.name}/"
                             f"{act.dev.id[:8]} dropped: its connection is gone")
                    elif act.action == "transfer":
                        await self._run_transfer(act)
                    else:
                        await self._run_act(act)
                except Exception as e:  # noqa: BLE001 — one act must not strand the rest
                    log("held act error:", e)
        finally:
            # No await between the last emptiness check and this line, so an act
            # arriving now finds either the task or an empty queue, never neither.
            self._drain_task = None

    async def _run_act(self, act: Act) -> None:
        try:
            await self._apply_act(act)
        except Exception as e:  # noqa: BLE001 — never let one bad frame kill the socket
            # Raised while reading the act's fields (`_check_act` sees to it that
            # this is before anything changed), so ok:false is the truth.
            act.fail("bad_request", f"{type(e).__name__}: {e}"[:200])
            log("message error:", e)
        finally:
            await self._ack(act)

    async def _ack(self, act: Act) -> None:
        """Answer an act that carried a `cid`. The LAST frame the act causes.

        Callers send it after awaiting every broadcast the act made, and a
        broadcast returns only once each socket's frame is written, so on the
        sender's socket the ack follows them. When it arrives the sender's mirror
        already holds the hub's answer, which is what lets a controller drop the
        value it was showing in the meantime.
        """
        if act.cid is None or act.acked:
            return
        act.acked = True
        if act.stale:
            return  # a cid means nothing on another connection
        frame: dict = {"t": "ack", "cid": act.cid, "ok": act.ok,
                       "rev": act.dev.session_rev_sent}
        if act.code:
            frame["code"] = act.code
            if act.message:
                frame["message"] = act.message
        await self._send(act.dev, frame)

    async def _refuse(self, act: Act, code: str, message: str, *, ok: bool = False,
                      to: Optional[Device] = None, everyone: bool = False) -> None:
        """Every `error` about an act goes through here.

        With a `cid` the failure is reported in the ack and nowhere else: to the
        one who asked. Without one, the frames go where they always went (`to`,
        default the sender; `everyone` for the two transfer refusals that used to
        be broadcast), because a client that sends no `cid` reads nothing else.
        `ok=True` is "applied to the session, though no device heard it".
        """
        act.fail(code, message, ok=ok)
        if act.cid is not None:
            return
        frame = {"t": "error", "code": code, "message": message}
        if everyone:
            await self._broadcast(frame)
        else:
            await self._send(to if to is not None else act.dev, frame)

    @staticmethod
    def _check_act(action: Any, msg: dict) -> None:
        """Raise if an act's fields cannot be read. Run before anything changes.

        `ok:false` promises that the act changed nothing, so every conversion that
        can fail has to fail here. `setQueue` used to assign the queue and then
        raise on a bad `index`: the session held a queue no client had been told
        about until something else broadcast it.
        """
        if action in ("setQueue", "enqueue"):
            q = msg.get("tracks", [])
            if not isinstance(q, list) or any(not isinstance(t, dict) for t in q):
                raise ValueError("tracks must be a list of track objects")
        if action == "setQueue":
            int(msg.get("index", 0)), int(msg.get("positionMs", 0))
        elif action in ("remove", "jump"):
            int(msg["index"])
        elif action == "move":
            int(msg["from"]), int(msg["to"])
        elif action == "seek":
            int(msg["positionMs"])
        elif action == "volume":
            int(msg.get("level", 100))
            target = msg.get("target")
            if target is not None and not isinstance(target, str):
                raise ValueError("target must be a device id")
        elif action == "sleepTimer":
            minutes = msg.get("minutes")
            # An int and nothing that merely converts to one: "30" or 30.5 from a
            # client is a bug to report, and True would otherwise set one minute.
            if type(minutes) is not int or not 0 <= minutes <= SLEEP_MINUTES_MAX:
                raise ValueError(f"minutes must be an integer 0..{SLEEP_MINUTES_MAX}")
        elif action == "rating":
            int(msg.get("rating", 0))

    async def _apply_act(self, act: Act) -> None:
        dev, msg = act.dev, act.msg
        action = msg.get("action")
        s = self.session
        active = s.active_device_id
        # DIAG (echo): every act frame, with the fields that move play-state.
        dlog(f"ACT {action} from {dev.name}/{dev.id[:8]} "
             f"play={msg.get('play')} pos={msg.get('positionMs')} idx={msg.get('index')} "
             f"| pre is_playing={s.is_playing}")
        self._check_act(action, msg)

        # A volume for a device other than the playing one is not an act on the
        # session at all, so none of what follows applies to it: nobody is
        # promoted, and "no active device" is no reason to refuse.
        if action == "volume" and msg.get("target") not in (None, active):
            await self._set_idle_volume(act, msg["target"])
            return

        # Promote the sender to active when there's nothing playing yet.
        promoted = False
        takeover_seq = None
        if active is None and action in ("play", "setQueue"):
            active = dev.id
            s.set_active(active)
            promoted = True
            if action == "play" and s.queue:
                # A takeover: this device is about to be handed the session's queue
                # (the `play` branch below). An act that waited behind a transfer
                # runs off its sender's read loop, so the sender's own reports can
                # arrive during the broadcast below, and it is the active device
                # from this line on. Same rule as a transfer target: its need is
                # raised here, to the number the load will carry.
                takeover_seq = self._echo_expect(dev)
            await self._broadcast_devices()
        # The device every directive of this act goes to.
        adev = self._dev(active)

        # Acts that only make sense against a live receiver. We still apply the intent
        # (so the session stays coherent and clients mirror it), but the sender is told
        # the directive went nowhere instead of it vanishing silently.
        if active is None and action in ("pause", "playpause", "next", "previous",
                                         "jump", "seek", "volume"):
            # ok:true where the intent is applied to the session all the same;
            # `volume` applies nothing, so for it this is a refusal.
            await self._refuse(act, "no_active_device",
                               f"{action}: no device is currently active",
                               ok=action != "volume")

        if action == "setQueue":
            # Flush where we left the OUTGOING queue before it becomes "previous".
            self._touch_saved_queue_progress()
            # The track it was on may have been heard: the play history hears it,
            # the station does not (a new queue is never a skip, §18.3).
            self._judge_departure(s.index, None, s.position_ms, steer=False)
            s.queue = msg.get("tracks", [])
            s.index = self._clamp_index(int(msg.get("index", 0)))
            s.position_ms = max(0, int(msg.get("positionMs", 0)))
            s.is_playing = bool(msg.get("play", True))
            self._mark_play_intent()
            self._rebuild_order()
            # Saved-queue identity: adopt the client's id (or mint one) and record it as
            # the current history entry. Re-publishing the SAME id (e.g. a reporter
            # re-publish) just refreshes that record rather than forking a new one.
            # An empty queue gets no history record — minting one here would leave
            # session.savedQueueId pointing at a record _upsert_saved_queue refuses
            # to create, which then reads as "nothing is playing" forever.
            if s.queue:
                requested = _as_str(msg.get("savedQueueId"), 128)
                if requested and requested in self.deleted_saved_queues:
                    # Deleted from another device while this one kept playing. Start a
                    # NEW session rather than resurrect the record the user just removed
                    # — the hub-side mirror of the client's restartQueueSession().
                    requested = None
                s.saved_queue_id = requested or self._mint_saved_queue_id()
                s.source_kind = msg.get("sourceKind") or "manual"
                s.source_name = msg.get("sourceName")
                self._upsert_saved_queue(s.saved_queue_id, s.source_kind, s.source_name,
                                         server_id=msg.get("serverId"),
                                         cover_image_url=msg.get("coverImageUrl"))
            else:
                s.saved_queue_id = None
                s.source_kind = "manual"
                s.source_name = None
            # §18.2: the new queue continues as the station it names, and a
            # setQueue naming none (or one this hub does not hold) unbinds.
            sid = _as_str(msg.get("stationId"), 64)
            s.station_id = (sid if sid and s.queue and self.stations.get(sid) is not None
                            else None)
            s.bump()
            # Only push a load to the active receiver if it ISN'T the device that
            # sent the queue. When a device publishes the queue it's already
            # playing locally (e.g. Feishin claiming active), so echoing do:load
            # back would reload/restart it.
            if active and active != dev.id:
                await self._do(adev, "load", tracks=s.queue, index=s.index,
                               positionMs=s.position_ms, play=s.is_playing)
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action == "enqueue":
            tracks = msg.get("tracks", [])
            at = msg.get("at", "end")
            if at == "next":
                insert_pos = s.index + 1
                s.queue[insert_pos:insert_pos] = tracks
                self._order_after_insert(insert_pos, len(tracks), play_next=True)
            else:
                insert_pos = len(s.queue)
                s.queue.extend(tracks)
                self._order_after_insert(insert_pos, len(tracks), play_next=False)
            # Auto DJ / manual enqueue grows the CURRENT saved queue in place.
            self._sync_current_saved_queue()
            s.bump()
            await self._do(adev, "queueChanged", tracks=s.queue, index=s.index)
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action == "clear":
            # Flush where we left off BEFORE detaching, so the cleared queue stays
            # resumable from the history, then stop pointing the live session at it.
            self._touch_saved_queue_progress()
            self._judge_departure(s.index, None, s.position_ms, steer=False)
            s.queue = []
            s.index = 0
            s.position_ms = 0
            s.is_playing = False
            s.saved_queue_id = None
            s.source_kind = "manual"
            s.source_name = None
            s.station_id = None
            self._mark_play_intent()
            self._rebuild_order()
            s.bump()
            # `queueChanged` carries a track list and receivers ignore an empty one,
            # so an emptied queue needs its own command to actually stop the device.
            await self._do(adev, "clear")
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action in ("remove", "move"):
            # Removing the CURRENT track leaves s.index pointing at what was the next
            # song, so the active device has to be told to switch to it — queueChanged
            # alone would leave the removed track playing.
            jump_after_remove = False
            if action == "remove":
                i = int(msg["index"])
                if 0 <= i < len(s.queue):
                    s.queue.pop(i)
                    self._order_after_remove(i)
                    if i < s.index:
                        s.index -= 1
                    elif i == s.index:
                        if s.index >= len(s.queue):
                            # Dropped the last track: wrap when repeating, else stop.
                            s.index = 0 if (s.repeat == "all" and s.queue) else max(
                                len(s.queue) - 1, 0
                            )
                            s.is_playing = bool(s.queue) and s.repeat == "all"
                        s.position_ms = 0
                        jump_after_remove = True
            else:
                fr, to = int(msg["from"]), int(msg["to"])
                if 0 <= fr < len(s.queue) and 0 <= to < len(s.queue):
                    s.queue.insert(to, s.queue.pop(fr))
                    self._order_after_move(fr, to)
                    # Keep s.index pointing at the SAME (currently-playing) song
                    # after the reorder, so the active device doesn't restart/jump.
                    if fr == s.index:
                        s.index = to
                    else:
                        if fr < s.index:
                            s.index -= 1
                        if to <= s.index:
                            s.index += 1
            self._sync_current_saved_queue()
            s.bump()
            if not s.queue:
                await self._do(adev, "clear")
            else:
                await self._do(adev, "queueChanged", tracks=s.queue, index=s.index)
                if jump_after_remove:
                    await self._do(adev, "jump", index=s.index)
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action == "play":
            s.is_playing = True; self._mark_play_intent(); s.bump()
            if promoted and s.queue:
                # The sender just took over an orphaned session (e.g. the previous
                # device was force-stopped). It has no idea what the session holds, so
                # a bare do:play would resume ITS own stale local queue/position —
                # hand it the session's queue + resume point instead (PROTOCOL §5.1).
                await self._do(adev, "load", seq=takeover_seq, tracks=s.queue,
                               index=s.index, positionMs=s.position_ms, play=True)
            else:
                await self._do(adev, "play")
            await self._broadcast_session()

        elif action == "pause":
            s.is_playing = False; self._mark_play_intent(); s.bump()
            await self._do(adev, "pause")
            await self._broadcast_session()

        elif action == "playpause":
            s.is_playing = not s.is_playing; self._mark_play_intent(); s.bump()
            await self._do(adev, "play" if s.is_playing else "pause")
            await self._broadcast_session()

        elif action in ("next", "previous"):
            nxt = self._step_index(1 if action == "next" else -1)
            if nxt is None:
                s.is_playing = False; self._mark_play_intent(); s.bump()
                await self._do(adev, "pause")
            else:
                self._mark_position_intent()
                self._mark_index_intent(nxt)
                s.index = nxt; s.position_ms = 0; s.bump()
                await self._do(adev, "jump", index=s.index)
            await self._broadcast_session()

        elif action == "jump":
            target_index = self._clamp_index(int(msg["index"]))
            self._mark_position_intent()
            self._mark_index_intent(target_index)
            s.index = target_index; s.position_ms = 0; s.bump()
            await self._do(adev, "jump", index=s.index)
            await self._broadcast_session()

        elif action == "seek":
            target_ms = max(0, int(msg["positionMs"]))
            self._mark_position_intent()
            s.position_ms = target_ms; s.bump()
            await self._do(adev, "seek", positionMs=s.position_ms)
            await self._broadcast_session()

        elif action == "repeat":
            s.repeat = msg.get("mode", "none"); s.bump()
            await self._do(adev, "setRepeat", mode=s.repeat)
            await self._broadcast_session()

        elif action == "shuffle":
            s.shuffle = bool(msg.get("on", False))
            self._rebuild_order(); s.bump()
            await self._do(adev, "setShuffle", on=s.shuffle, order=s.order)
            await self._broadcast_session()

        elif action == "volume":
            level = max(0, min(100, int(msg.get("level", 100))))
            active_dev = self.devices.get(active) if active else None
            if active_dev is not None:
                active_dev.volume = level
            await self._do(adev, "setVolume", level=level)
            await self._broadcast_devices()

        elif action == "sleepTimer":
            minutes = msg["minutes"]
            if minutes:
                # Replaces a running timer: there is one, and the later word wins.
                length = minutes * SLEEP_MINUTE_S
                self._sleep_set(SleepTimer(
                    minutes=minutes, set_by=dev.id,
                    ends_at=int((time.time() + length) * 1000),
                    deadline=time.monotonic() + length))
            elif self._sleep is not None:
                self._sleep_set(None)
            else:
                return  # 0 with no timer set: nothing changed, so nothing to say
            log(f"sleep timer {f'set to {minutes} min' if minutes else 'cleared'} "
                f"by {dev.name} ({dev.id[:8]})")
            s.bump()
            await self._broadcast_session()

        elif action in ("favorite", "rating"):
            # Relay a purpose-built directive with whitelisted fields only — echoing
            # the raw act back would forward the sender's auth token to the receiver.
            if action == "favorite":
                await self._do(adev, "favorite", id=msg.get("id"),
                               favorite=bool(msg.get("favorite", True)))
            else:
                await self._do(adev, "rating", id=msg.get("id"),
                               rating=max(0, min(5, int(msg.get("rating", 0)))))

        elif action == "renameSavedQueue":
            rid = msg.get("id")
            rec = self.saved_queues.get(rid) if rid else None
            if rec is not None:
                rec["name"] = (_as_str(msg.get("name")) or "").strip() or None
                rec["updatedAt"] = int(time.time() * 1000)
                await self._broadcast_saved_queues()
            else:
                # The client has already applied the rename locally, so silence would
                # leave the two permanently disagreeing with nothing to notice it.
                await self._refuse(act, "unknown_saved_queue", f"no saved queue {rid!r}")

        elif action == "deleteSavedQueue":
            changed, detached = self._delete_saved_queue(_as_str(msg.get("id"), 128))
            if detached:
                s.bump()
                await self._broadcast_session()
            if changed:
                await self._broadcast_saved_queues()

        elif action == "deleteSavedQueues":
            # Batched form: clear-all / delete-others used to send one act per row, so
            # 20 deletions meant 20 full-history broadcasts and 20 state.json rewrites.
            ids = msg.get("ids")
            if isinstance(ids, list):
                changed = False
                detached = False
                for raw_id in ids[:SAVED_QUEUES_MAX]:
                    c, d = self._delete_saved_queue(_as_str(raw_id, 128))
                    changed = changed or c
                    detached = detached or d
                if detached:
                    s.bump()
                    await self._broadcast_session()
                if changed:
                    await self._broadcast_saved_queues()

        elif action == "saveMix":
            # Create (no `id`) or update one "Mixed for You" recipe.
            mid = self._save_mix(msg)
            if mid is not None:
                await self._broadcast_mixes()
            else:
                # A recipe with no name, or a `kind` this hub has never heard of.
                # Answered rather than dropped: the client has already shown the
                # user a mix it believes it saved.
                await self._refuse(act, "bad_mix", "a mix needs a name and a known kind")

        elif action == "renameMix":
            if self._rename_mix(_as_str(msg.get("id"), 128), _as_str(msg.get("name"))):
                await self._broadcast_mixes()
            else:
                await self._refuse(act, "unknown_mix", f"no mix {msg.get('id')!r}")

        elif action == "deleteMix":
            if self._delete_mix(_as_str(msg.get("id"), 128)):
                await self._broadcast_mixes()

        elif action == "touchMix":
            # "This recipe was just played." Broadcast like the others so a second
            # device's list re-sorts, but note it does NOT move `updatedAt` — see
            # `_touch_mix`.
            if self._touch_mix(_as_str(msg.get("id"), 128)):
                await self._broadcast_mixes()

        elif action == "syncSavedQueues":
            # A (re)connecting client pushes its local/offline history up; union-merge
            # and rebroadcast the reconciled list to everyone.
            changed = False
            detached = False
            # Deletions first: a client that deleted a row while offline still holds it
            # in `queues` (it can't have re-fetched), so merging first would re-add it.
            deleted = msg.get("deleted")
            if isinstance(deleted, list):
                for raw_id in deleted[:SAVED_QUEUES_MAX]:
                    c, d = self._delete_saved_queue(_as_str(raw_id, 128))
                    changed = changed or c
                    detached = detached or d
            incoming = msg.get("queues")
            if isinstance(incoming, list) and self._merge_saved_queues(incoming):
                changed = True
            if detached:
                s.bump()
                await self._broadcast_session()
            if changed:
                await self._broadcast_saved_queues()

        else:
            await self._refuse(act, "bad_action", f"unknown action {action!r}")

    async def _set_idle_volume(self, act: Act, target_id: str) -> None:
        """`act:volume` naming a device that is not the playing one (PROTOCOL §5.1):
        turn a speaker down BEFORE sending it music.

        Refused unless the device can be told now and has said it will listen.
        Every check comes before anything is recorded, so a refusal leaves the row
        at its old level, which is what the slider that sent it falls back to.
        """
        target = self.devices.get(target_id)
        if target is None or not target.online or target.ws is None:
            await self._refuse(act, "target_offline", "target device is not connected")
            return
        if target.effective_reachable() is False:
            await self._refuse(act, "target_unreachable", f"{target.name} is not responding")
            return
        if IDLE_VOLUME_CAP not in (target.caps or []):
            await self._refuse(act, "not_supported",
                               f"{target.name} cannot set its volume while it is not playing")
            return
        target.volume = max(0, min(100, int(act.msg.get("level", 100))))
        # `setVolume` is not one of ECHO_CMDS, so `_do` gives it no `seq` and the
        # target's need is untouched: its reports are still not mirrored, and a
        # volume change is nothing a report could be stale about.
        await self._do(target, "setVolume", level=target.volume)
        await self._broadcast_devices()

    async def _on_device_state(self, dev: Device, msg: dict) -> None:
        """What a device says about itself between `hello`s (PROTOCOL §5.3).

        `reachable` / `appRunning` are a bridge's verdict on the hardware behind
        it (§12.2), only meaningful from a bridged device: an ordinary client IS
        its own hardware, so a socket from it already proves reachability.

        `volume` is the device's own level, from ANY device: the row then shows
        what the device is set to, also after it was changed at the device. It is
        a report, never a request, so nothing is sent back; in particular not a
        `do:setVolume`, which the device would apply and report again.

        Rebroadcast only on an actual change. Bridges re-assert on a timer and
        every device repeats its level on each connect: a device fan-out to every
        client for those would be pure noise.
        """
        reachable = msg.get("reachable")
        app_running = msg.get("appRunning")
        before = dev.effective_reachable()
        if reachable is not None:
            dev.reachable = bool(reachable)
            dev.reachable_at = int(time.time() * 1000)
        if app_running is not None:
            dev.app_running = bool(app_running)
        level = _as_level(msg.get("volume"))
        volume_changed = level is not None and level != dev.volume
        if volume_changed:
            dlog(f"VOLUME {dev.name}/{dev.id[:8]} reports {level} (row said {dev.volume})")
            dev.volume = level
        if dev.effective_reachable() != before:
            log(f"~ {dev.name} ({dev.id[:8]}) reachable={dev.reachable}")
            await self._broadcast_devices()
            # A speaker that just went away while holding the session is not paused by
            # anything else: its bridge keeps a healthy hub socket, so _disconnect never
            # fires. Without this the whole stack shows a playing bar over silence.
            if dev.reachable is False and self.session.active_device_id == dev.id:
                self._touch_saved_queue_progress()
                self.session.is_playing = False
                self.session.set_active(None)
                self.session.bump()
                await self._broadcast_session()
                await self._broadcast_devices()
        elif volume_changed:
            # (A reachability change above has already sent the row, level included.)
            await self._broadcast_devices()

    async def _run_transfer(self, act: Act) -> None:
        """Carry one transfer out and ack it: when the handoff is committed or
        refused, not when the target has loaded (`load_failed` stays a broadcast,
        since it changes what everyone sees)."""
        folded: list[Act] = []      # acts this handoff carried in its load
        try:
            await self._transfer(act, folded)
        except Exception as e:  # noqa: BLE001 — a detached task's traceback would be lost
            log("transfer error:", e)
            if not act.committed:
                reason = f"{type(e).__name__}: {e}"[:200]
                act.fail("bad_request", reason)
                for f in folded:
                    if f.ok:
                        f.fail("bad_request", "the transfer it was folded into failed: " + reason)
        finally:
            # After the commit's broadcasts and the load: `_transfer` has returned.
            await self._ack(act)
            for f in folded:
                await self._ack(f)

    async def _transfer(self, act: Act, folded: list) -> None:
        s = self.session
        target_id, play = act.msg.get("target"), act.msg.get("play")
        target = self.devices.get(target_id) if target_id else None
        if not target or not target.online:
            await self._refuse(act, "target_offline", "target device is not connected",
                               everyone=True)
            return
        # `online` only means a bridge holds the socket. For a Chromecast the speaker
        # itself can be off, asleep, or on a network this hub can't reach — handing it
        # the session there is exactly the "player says playing, nothing plays" bug.
        if target.effective_reachable() is False:
            await self._refuse(act, "target_unreachable", f"{target.name} is not responding",
                               everyone=True)
            log(f"transfer rejected: {target.name} reported unreachable")
            return
        if "receiver" not in (target.caps or []):
            # Without a cid this still goes to the TARGET, as it always did.
            await self._refuse(act, "not_a_receiver", "target device cannot play audio",
                               to=target)
            log(f"transfer rejected: {target.name} has no 'receiver' cap")
            return

        # Default: preserve the play state (Spotify behaviour — transferring a
        # paused session keeps it paused). Captured BEFORE release, because the
        # old device's final report sets is_playing False.
        play = s.is_playing if play is None else bool(play)

        old_id = s.active_device_id
        if old_id == target_id:
            # Transferring to the device that's already active. A do:load here would
            # reload it at session.position_ms, which lags the live position by up to a
            # report interval — i.e. the "transfer to myself rewinds playback" bug. At
            # most nudge play-state; otherwise this is a no-op.
            if play != s.is_playing:
                s.is_playing = play
                self._mark_play_intent()
                s.bump()
                await self._do(target, "play" if play else "pause")
                await self._broadcast_session()
            log(f"transfer -> {target.name}: already active, no-op")
            return

        tid = self._next_transfer_id()
        old = self.devices.get(old_id) if old_id else None
        if old is not None and old.online:
            fut = asyncio.get_running_loop().create_future()
            old.release_future = fut
            old.release_tid = tid
            sent = await self._do(old, "release", required=True, transferId=tid)
            try:
                # A frame that never left the process will never be answered. Waiting
                # RELEASE_TIMEOUT for it just delays the handoff by a second and a half
                # and then reports it as the old device being slow.
                if sent:
                    await asyncio.wait_for(fut, RELEASE_TIMEOUT)
            except asyncio.TimeoutError:
                log(f"release timed out for {old.name}; using last known position")
            finally:
                old.release_future = None
                old.release_tid = None
        # s.position_ms / s.index now reflect the old device's final report (or last known).

        # Fold in what was pressed while the old device was letting go. A play, a
        # pause or a seek that arrived then is the user's latest word on exactly
        # the two things the load carries, so the load carries it: the new device
        # starts paused, or at the sought position, and never plays what had
        # already been stopped. Only the LEADING run of such acts, though. Behind
        # a skip, a new queue or another transfer they mean something else (a seek
        # after `next` is a position in the next track), so from the first act of
        # another kind on, everything runs after the load, in arrival order.
        # No await from here to the commit: what arrives later is not folded.
        while self._held and self._held[0].action in FOLDED_ACTIONS:
            h = self._held.popleft()
            folded.append(h)
            if h.stale:
                continue
            dlog(f"ACT {h.action} from {h.dev.name}/{h.dev.id[:8]} FOLDED into transfer {tid}")
            if h.action == "seek":
                try:
                    sought = max(0, int(h.msg["positionMs"]))
                except (KeyError, TypeError, ValueError) as e:
                    h.fail("bad_request", f"{type(e).__name__}: {e}"[:200])
                    continue
                self._mark_position_intent()
                s.position_ms = sought
            else:
                play = {"play": True, "pause": False}.get(h.action, not play)

        gen = s.set_active(target_id)
        # The load's number is taken HERE, not where the load is written. From this
        # line the target is the active device and its reports are mirrored; until
        # the load has taken effect they describe whatever it was doing before
        # (probe P4: index 3 at 99 s over a session at index 1, 42 s). Raising its
        # need now drops them all, and the two broadcasts below can no longer open
        # a window in which they are believed.
        load_seq = self._echo_expect(target)
        s.is_playing = play
        self._mark_play_intent()
        s.bump()
        act.committed = True
        # Broadcast BEFORE the load so the target learns it is active first —
        # otherwise its do:load side effects (play events) fire while it still
        # believes another device is active, which can misroute them.
        await self._broadcast_session()
        await self._broadcast_devices()
        # Only receivers that advertise `loadAck` are held to it. An older client
        # never sends `loaded`, and treating its silence as failure would roll back
        # every transfer it ever wins. Armed BEFORE the send so a fast receiver's ack
        # can't arrive first and be dropped on the floor.
        fut = None
        if "loadAck" in (target.caps or []):
            # Whatever the previous attempt was waiting for, it is not this. Leaving it
            # alive would keep a second waiter running against the same device until it
            # timed out, and racing this one to roll the slot back.
            prev = target.load_future
            if prev is not None and not prev.done():
                prev.cancel()
            fut = asyncio.get_running_loop().create_future()
            target.load_future = fut
            target.load_tid = tid
        # The receiver needs the SAME deadline the hub is holding it to. Without it a
        # bridge picks its own — Navic allowed 25s per connect attempt, twice — and a
        # load that lands after the rollback is audio on a speaker no client believes
        # is active. Measured from dispatch; the receiver must answer inside it.
        await self._do(target, "load", required=True, seq=load_seq,
                       transferId=tid, timeoutMs=int(LOAD_TIMEOUT * 1000),
                       tracks=s.queue, index=s.index,
                       positionMs=s.position_ms, play=play)
        log(f"transfer {tid} -> {target.name} @ index {s.index}, {s.position_ms}ms")
        # Awaited off to the side: the hold on session acts ends when this returns
        # (the load is written), and waiting LOAD_TIMEOUT here would keep every
        # pause and skip queued for ten seconds while a slow speaker warms up.
        if fut is not None:
            asyncio.create_task(self._await_load_ack(target, old_id, fut, tid, gen, load_seq))

    async def _await_load_ack(self, target: Device, old_id: Optional[str], fut: Any,
                              tid: str, gen: int, load_seq: Optional[int] = None) -> None:
        """Roll the active slot back if a transfer target never starts playing.

        The hub commits `active_device_id` before sending `do:load` on purpose (the
        target must know it is active before its load side effects fire), which used
        to mean a failed load was invisible: every client showed a playing bar over a
        silent speaker until someone pressed something. A receiver that cannot start
        answers `loaded {ok:false}`; one that is wedged answers nothing.
        """
        try:
            await self._settle_load_ack(target, old_id, fut, tid, gen, load_seq)
        finally:
            # Whatever happened, the question this device was de-authorized over is
            # answered (see the `loaded` branch of _on_message).
            if target.load_refused is fut:
                target.load_refused = None

    async def _settle_load_ack(self, target: Device, old_id: Optional[str], fut: Any,
                               tid: str, gen: int, load_seq: Optional[int]) -> None:
        ok, err = False, "timeout"
        try:
            msg = await asyncio.wait_for(fut, LOAD_TIMEOUT)
            ok = bool(msg.get("ok", True))
            err = str(msg.get("error") or "load failed")
        except asyncio.CancelledError:
            # Superseded by a newer transfer to this same device, which cancelled us
            # on its way past. It owns the outcome now.
            return
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # noqa: BLE001
            err = str(e)
        finally:
            if target.load_future is fut:
                target.load_future = None
                target.load_tid = None
        if ok:
            return
        # The hub has given up on that load, so nothing is left to wait for on it.
        self._echo_settle(target, load_seq)
        s = self.session
        # A newer transfer (or a takeover) already moved on — that decision wins.
        #
        # The generation, not just the device id: A -> X, B -> Y, C -> X leaves X active
        # again, so an id-only check let A's expired rollback undo C. `gen` is the exact
        # assignment this transfer made, and any later one — including C's — invalidates it.
        if s.active_gen != gen or s.active_device_id != target.id:
            log(f"load ack {tid} for {target.name} failed ({err}) but a newer decision "
                f"({s.active_gen} > {gen}) already owns the slot")
            return
        # Hand the slot back only if the previous owner is still there to take it;
        # otherwise leave it orphaned, which is the signal every client already knows
        # how to read (adopt the queue locally, paused).
        old = self.devices.get(old_id) if old_id else None
        s.set_active(old.id if (old is not None and old.transferable()) else None)
        s.is_playing = False
        s.bump()
        log(f"transfer to {target.name} FAILED ({err}); active -> {s.active_device_id}")
        await self._broadcast_session()
        await self._broadcast_devices()
        await self._broadcast({"t": "error", "code": "load_failed",
                               "message": f"{target.name} could not start playback"})


# --------------------------------------------------------------------------- #
# Serving the proxy on the WebSocket port
#
# `websockets`' legacy asyncio server calls process_request() after the request
# line + headers, before the handshake; returning a triple short-circuits the
# connection into a plain HTTP response. Two things need a protocol subclass
# rather than the plain `process_request=` callable:
#   * its read_request() hard-rejects any method other than GET, and two proxy
#     routes are POSTs;
#   * process_request() as a bare callable gets no access to the StreamReader,
#     so it could never read a request body.
# This is legacy-API territory (requirements.txt pins websockets<14): if the
# internals ever move, the import below fails, the hub logs it and keeps serving
# WebSocket traffic with the proxy off rather than refusing to start.
# --------------------------------------------------------------------------- #
def _build_proxy_protocol() -> Optional[type]:
    try:
        from websockets.legacy.http import read_headers, read_line
        from websockets.legacy.server import WebSocketServerProtocol
    except Exception as e:  # noqa: BLE001
        log("audiomuse proxy unavailable (websockets internals moved):", e)
        return None

    class ProxyProtocol(WebSocketServerProtocol):  # type: ignore[misc,valid-type]
        http_method = "GET"
        body_consumed = False

        async def read_http_request(self):  # type: ignore[override]
            # Same as upstream's, minus the GET-only assertion, and remembering the
            # method so process_request can route POSTs.
            request_line = await read_line(self.reader)
            try:
                method, raw_path, version = request_line.split(b" ", 2)
            except ValueError:
                raise ValueError(f"invalid HTTP request line: {request_line!r}") from None
            if version != b"HTTP/1.1":
                raise ValueError(f"unsupported HTTP version: {version!r}")
            self.http_method = method.decode("ascii", "surrogateescape")
            path = raw_path.decode("ascii", "surrogateescape")
            headers = await read_headers(self.reader)
            self.path = path
            self.request_headers = headers
            return path, headers

        async def process_request(self, path, request_headers):  # type: ignore[override]
            # Checked ahead of the proxies: /lb/notify, /lb/fill and /lb/index
            # sit under the lb prefix but are inbound, so LbProxy would 404 them
            # against its route table. (/lb/index matches only exactly, so the
            # proxied /lb/index/changes and /lb/index/keys fall through.)
            result = await _handle_lb_notify(self, path, request_headers,
                                             self.http_method)
            if result is None:
                result = await _handle_lb_fill(self, path, request_headers,
                                               self.http_method)
            if result is None:
                result = await _handle_lb_index(self, path, request_headers,
                                                self.http_method)
            if result is None:
                for proxy in PROXIES:
                    result = await proxy.handle(self, path, request_headers,
                                                self.http_method)
                    if result is not None:
                        break
            if result is None and self.http_method != "GET":
                # Not a proxy route and not a handshake — don't fall through into the
                # WS upgrade with a method it can't answer.
                result = _http_json(405, {"error": "method not allowed"})
            if result is not None:
                await _drain_body(self, request_headers)
                return result
            return None

    return ProxyProtocol


async def _serve_health(hub: Hub, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    """Answer any HTTP request on the health port with the hub's status JSON."""
    try:
        await reader.readline()  # request line; content ignored
        while True:               # drain headers up to the blank line
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        body = json.dumps(hub.health()).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
    except Exception:  # noqa: BLE001 — health probe must never raise
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _check_auth_config() -> None:
    """Refuse to run as an open control plane. See ALLOW_INSECURE_NO_AUTH."""
    if TOKEN:
        return
    if not ALLOW_INSECURE_NO_AUTH:
        raise SystemExit(
            "HUB_TOKEN is not set.\n"
            "The WebSocket control plane has administrative reach — claiming device ids, "
            "rewriting the queue, moving playback — so the hub will not run without a token.\n"
            "Set HUB_TOKEN, or for a loopback-only experiment set "
            "HUB_ALLOW_INSECURE_NO_AUTH=true with HUB_HOST=127.0.0.1."
        )
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"HUB_ALLOW_INSECURE_NO_AUTH is set but HUB_HOST is {HOST!r}.\n"
            "Unauthenticated mode is restricted to loopback — bind 127.0.0.1, or set HUB_TOKEN."
        )
    log("WARNING: running with NO AUTHENTICATION on loopback only "
        "(HUB_ALLOW_INSECURE_NO_AUTH). Never do this on a reachable interface.")


async def main() -> None:
    _check_auth_config()
    if MIRROR_PLAYQUEUE and not NAVIDROME_URL:
        log("WARNING: HUB_MIRROR_PLAYQUEUE is on but NAVIDROME_URL is unset — "
            "the savePlayQueue mirror is disabled. Set it in .env.")
    if AUDIOMUSE_URL and not TOKEN:
        log("WARNING: AUDIOMUSE_URL is set but HUB_TOKEN is empty — the AudioMuse "
            "proxy is DISABLED (it would be an open relay to the core API).")
    if LBBOT_URL and not TOKEN:
        log("WARNING: LBBOT_URL is set but HUB_TOKEN is empty — the lb-bot proxy is "
            "DISABLED. lb-bot's API has no auth of its own, so relaying it without "
            "a hub token would publish every route on the whitelist.")
    if PREVIEW_URL and TOKEN and not PREVIEW_SECRET:
        log("WARNING: PREVIEW_URL is set but PREVIEW_SECRET is empty — the preview "
            "proxy is DISABLED. Every stream URL the hub minted would be signed with "
            "an empty key and refused by the sidecar, i.e. the feature would fail at "
            "playback rather than at configuration. Set the SAME value on both, and "
            "not HUB_TOKEN.")
    if PREVIEW_URL and PREVIEW_SECRET and not PREVIEW_PUBLIC_URL:
        log("NOTE: PREVIEW_PUBLIC_URL is unset — previews play for a client that can "
            "reach PREVIEW_URL itself, but not on a Chromecast. The hub advertises "
            "previewCastable=false and both clients refuse a cast transfer while an "
            "ext: track is queued.")
    global HUB_INSTANCE
    hub = Hub()
    HUB_INSTANCE = hub
    log(f"navi-connect hub on ws://{HOST}:{PORT}  "
        f"(Navidrome: {NAVIDROME_URL or '<unset>'}, "
        f"mirror={'on' if MIRROR_PLAYQUEUE and NAVIDROME_URL else 'off'})")

    # Installed unconditionally — NOT gated on AUDIOMUSE_URL. Without the custom
    # protocol nothing routes /sonic/*, so the request falls through to the WebSocket
    # upgrade and the client gets a stock 426 text/plain "Upgrade Required". That is
    # not JSON, so the clients' Tier-2 probe can't parse it, reads as a hard failure,
    # and never reaches the "hub has no AudioMuse → fall back to the direct route"
    # path that SonicProxy.handle's `if not self.enabled` branch exists to trigger.
    proxy_protocol = _build_proxy_protocol()
    if proxy_protocol is not None:
        for proxy in PROXIES:
            if proxy.enabled:
                log(f"{proxy.label} proxy on http://{HOST}:{PORT}{proxy.prefix}/* "
                    f"-> {proxy.upstream}")
            else:
                # Routed but not forwarding: the probe route answers
                # {"configured": false} and everything else a clean 503, so clients
                # demote instead of erroring.
                log(f"{proxy.label} proxy routed but NOT forwarding "
                    f"({proxy.disabled_reason})")

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    # Graceful shutdown: on SIGTERM/SIGINT (container stop), stop serving and flush
    # state + any pending mirror write, so an in-flight edit isn't lost on teardown.
    def _request_stop() -> None:
        if not stop.done():
            stop.set_result(None)
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass  # Windows dev; the container runs on Linux

    health_server = await asyncio.start_server(
        lambda r, w: _serve_health(hub, r, w), HOST, HEALTH_PORT)
    log(f"health endpoint on http://{HOST}:{HEALTH_PORT}/")

    serve_kwargs: dict[str, Any] = {}
    if proxy_protocol is not None:
        serve_kwargs["create_protocol"] = proxy_protocol

    sweep = asyncio.create_task(hub._device_sweep_loop())  # noqa: SLF001 — same module
    # The station engine's tier probe (PROTOCOL §18.1: every 10 min, and after a
    # failure), for the characters route's `available` and `engine`.
    station_probe = (asyncio.create_task(hub.stations.probe_loop())
                     if STATION.enabled else None)
    # The hub's own lb-bot probe, for `welcome.lb` (see LB_PROBE_STARTUP_WAIT).
    lb_probe = asyncio.create_task(LB.probe_loop()) if LB.enabled else None
    # `open_timeout` is the WebSocket *handshake* deadline, and it defaults to 10s
    # -- but the HTTP proxies answer from inside `process_request`, which runs
    # during that handshake. So a proxied request slower than the handshake
    # deadline has its connection aborted before any response is written: the
    # client sees a dropped socket rather than a status, and both clients report
    # that as "lb-bot is busy or unreachable".
    #
    # That silently capped every slow route at 10s and made PROXY_SLOW_TIMEOUT
    # unreachable. `/lb/album/sources` is a live slskd fan-out measured at 45s, so
    # it could never answer at all; cold `/lb/fresh-releases`, `/lb/gap/search`
    # and the MusicBrainz-backed routes were on the same cliff. The deadline has
    # to clear the slowest thing the proxy is allowed to wait for.
    handshake_timeout = max(PROXY_TIMEOUT, PROXY_SLOW_TIMEOUT,
                            PROXY_SEARCH_TIMEOUT, STATION_BUDGET_S) + 15
    async with websockets.serve(hub.handler, HOST, PORT,
                                ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                                open_timeout=handshake_timeout,
                                max_size=4 * 1024 * 1024, **serve_kwargs):
        await stop  # run until a stop signal arrives

    sweep.cancel()
    if lb_probe is not None:
        lb_probe.cancel()
    if station_probe is not None:
        station_probe.cancel()
    hub.stations.close()   # the play history, written now rather than in 30 s

    health_server.close()
    await health_server.wait_closed()
    hub._save()
    if hub._backup_task is not None and not hub._backup_task.done():
        try:  # that save may have queued a backup: let the worker write it (bounded)
            await asyncio.wait_for(asyncio.shield(hub._backup_task),
                                   STATE_BACKUP_FLUSH_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass
    if hub._mirror_task is not None and not hub._mirror_task.done():
        try:  # let a debounced savePlayQueue write finish (best-effort, short)
            await asyncio.wait_for(asyncio.shield(hub._mirror_task), 3)
        except Exception:  # noqa: BLE001
            pass
    log("hub stopped; state flushed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
