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
import os
import random
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
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
STATE_PATH = os.environ.get("HUB_STATE", "/data/state.json")
# Drop known devices not seen within this many days when the hub loads. 0 = keep forever.
DEVICE_TTL_DAYS = float(os.environ.get("HUB_DEVICE_TTL_DAYS", "0") or "0")

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

DEBUG = os.environ.get("HUB_DEBUG", "").lower() in ("1", "true", "yes")

PING_INTERVAL = 10  # seconds (matches Feishin's heartbeat)
PING_TIMEOUT = 10
RELEASE_TIMEOUT = 1.5  # seconds to wait for an old device to hand off
PROGRESS_THROTTLE = 1.0  # seconds between fanned-out progress broadcasts
INTENT_GRACE = 2.0  # seconds during which receiver reports can't contradict a
                    # fresh user play/pause intent (guards against stale
                    # in-flight 1 Hz reports flipping the state back)
MIRROR_DEBOUNCE = 2.5  # seconds to coalesce rapid savePlayQueue mirror writes
SAVED_QUEUES_MAX = 20  # rolling saved-queue history cap (matches both clients)
SQ_PROGRESS_THROTTLE = 5.0  # seconds between cursor writes to the current saved-queue record
TOMBSTONE_MAX = 200  # remembered saved-queue deletions (so a client re-sync can't resurrect)
SAVED_QUEUE_SONGS_MAX = 1000  # per-record track cap for CLIENT-SUPPLIED history (syncSavedQueues)
# Reload ceiling. Records the hub built itself from a live queue are uncapped (the session
# queue is), so re-reading state.json must not truncate one — this is only a sanity bound.
SAVED_QUEUE_SONGS_HARD_MAX = 20000
SQ_STR_MAX = 512  # longest accepted string field inside a client-supplied saved-queue record
POSITION_SAVE_THROTTLE = 10.0  # seconds between state writes driven by position-only reports

# --- proxy tuning (shared by the AudioMuse and lb-bot proxies) ---
PROXY_MAX_INFLIGHT = 4      # concurrent upstream calls; a hung core must not eat the
                            # default thread pool and stall the 1 Hz progress fan-out
PROXY_TIMEOUT = 20          # seconds per upstream socket op (urllib has one knob for
                            # connect+read); Tier 2 is in-memory lookups, so fail fast
PROXY_SLOW_TIMEOUT = 45     # for routes that are known to sit on a rate-limited third
                            # party (lb-bot's MusicBrainz-backed album lookups)
PROXY_CACHE_TTL = 60.0      # shared result cache — both clients asking the same
                            # question cost one upstream call
PROXY_CACHE_TTL_LONG = 6 * 3600.0  # for effectively immutable upstream answers (a
                            # release's tracklist and editions do not change)
PROXY_CACHE_MAX = 64
PROXY_MAX_BODY = 256 * 1024        # largest client request body accepted
PROXY_MAX_RESPONSE = 4 * 1024 * 1024


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
    online: bool = False
    volume: int = 100
    last_seen: int = 0
    ws: Any = None                       # live websocket, not persisted
    release_future: Any = None           # set during a transfer handoff

    def info(self, active_id: Optional[str]) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "caps": self.caps,
            "online": self.online,
            "isActive": self.id == active_id,
            "lastSeen": self.last_seen,
            "volume": self.volume,
        }


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
        "method": "GET", "path": "/get_score",
        "params": ("id",), "cache": True,
    },
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
LB_ROUTES: dict[tuple[str, str], dict] = {
    ("GET", "/lb/status"): {
        "method": "GET", "path": "/api/summary",
        "params": (), "cache": True, "probe": True,
    },
    ("GET", "/lb/artist/discography"): {
        "method": "GET", "path": "/api/artist/discography",
        "params": ("nd_id", "mbid"), "cache": True,
    },
    ("POST", "/lb/artist/discography"): {
        "method": "POST", "path": "/api/artist/discography",
        "body": ("mbid", "name", "nd_id", "external"), "cache": False,
    },
    ("GET", "/lb/fresh-releases"): {
        "method": "GET", "path": "/api/fresh-releases",
        "params": ("days",), "cache": True,
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
        "method": "GET", "path": "/api/album/sources",
        "params": ("rgid",), "cache": True,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("POST", "/lb/album/download"): {
        "method": "POST", "path": "/api/album/download",
        "body": ("rgid", "sourceUsername", "sourceFolder", "quality"),
        "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
    ("GET", "/lb/album/status"): {
        "method": "GET", "path": "/api/album/status",
        "params": ("release_mbid", "rgid"), "cache": False,
    },
    ("POST", "/lb/album/allow-mp3"): {
        # lb-bot puts the group id in the URL; the clients send it in the body so
        # the hub route table can stay a table of exact paths.
        "method": "POST", "path": "/api/gaps/{group_id}/allow-mp3",
        "path_args": ("group_id",), "body": ("group_id", "allow"), "cache": False,
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
        "strip": (("sources", "files"), ("sources", "filesTruncated")),
    },
    ("POST", "/lb/gap/auto"): {
        # Search, rank and enqueue the best source in one shot. Answers with a task
        # id the clients discard: the GET above carries `sourceTask` precisely so
        # nobody needs /api/tasks, which is not whitelisted and must stay that way.
        "method": "POST", "path": "/api/gaps/{group_id}/auto",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
    },
    ("POST", "/lb/gap/fetch"): {
        "method": "POST", "path": "/api/gaps/{group_id}/fetch",
        "path_args": ("group_id",), "body": ("group_id", "sourceId"), "cache": False,
    },
    ("POST", "/lb/gap/cancel"): {
        "method": "POST", "path": "/api/gaps/{group_id}/cancel",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
    },
    ("POST", "/lb/gap/rescan"): {
        # Re-reads the album from Navidrome and walks its folder for files
        # Navidrome hasn't indexed — slow enough to need the long timeout.
        "method": "POST", "path": "/api/gaps/{group_id}/rescan",
        "path_args": ("group_id",), "body": ("group_id",), "cache": False,
        "timeout": PROXY_SLOW_TIMEOUT,
    },
}

# Covers are deliberately absent. lb-bot's /api/cover/<id> serves *Navidrome*
# art keyed on a Navidrome album id — no use for a release the library does not
# have — and the art for unowned releases is a public Cover Art Archive URL the
# clients fetch directly. Proxying images would also put multi-megabyte bodies
# into a cache bounded by entry count, not bytes.

_PATH_ARG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


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
            return (r.status, r.read(PROXY_MAX_RESPONSE),
                    r.headers.get("Content-Type") or "application/json")
    except urllib.error.HTTPError as e:
        try:
            payload = e.read(PROXY_MAX_RESPONSE)
        except Exception:  # noqa: BLE001
            payload = b""
        return e.code, payload, e.headers.get("Content-Type") or "application/json"
    except Exception as e:  # noqa: BLE001 — never raise into the WS loop
        log(f"{label} proxy: upstream failed:", e)
        return 502, json.dumps({"error": f"{label} unreachable"}).encode(), "application/json"


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
        self._sem = asyncio.Semaphore(PROXY_MAX_INFLIGHT)
        self._cache: dict[str, tuple[float, int, bytes, str]] = {}

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

    @staticmethod
    def _strip(spec: dict, status: int, data: bytes) -> bytes:
        """Drop fields the clients never render from a successful response.

        Not a security control — the whitelist is that — but a bandwidth one, for
        the case where an upstream view is shaped for a local web UI and carries a
        payload nobody on a phone wants. Each rule is (list_key, field): the field
        is removed from every object in that top-level list. Anything unexpected
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
                for row in payload.get(list_key) or []:
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

        key = json.dumps([route, upstream_path, sorted(params), body],
                         sort_keys=True, default=str)
        if spec.get("cache"):
            hit = self._cache_get(key)
            if hit is not None:
                return hit

        url = self.upstream + upstream_path
        # nd creds are injected here, not logged: keep them out of the cache key too.
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode() if body is not None else None

        async with self._sem:
            status, data, ctype = await asyncio.to_thread(
                _proxy_upstream_blocking, spec["method"], url, payload,
                self.upstream_token, spec.get("timeout", PROXY_TIMEOUT), self.label)

        if spec.get("probe"):
            status, data, ctype = self.augment(spec, status, data)
        data = self._strip(spec, status, data)
        if spec.get("cache"):
            self._cache_put(key, status, data, ctype,
                            spec.get("ttl", PROXY_CACHE_TTL))
        return status, data, ctype

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
                return _http_json(413, {"error": "request body too large"})
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

    @property
    def upstream(self) -> str:
        return LBBOT_URL

    @property
    def disabled_reason(self) -> str:
        return ("LBBOT_URL is unset" if not LBBOT_URL
                else "HUB_TOKEN is empty (refusing to run as an open relay)")

    def augment(self, spec: dict, status: int, data: bytes) -> tuple[int, bytes, str]:
        """`/lb/status` is the liveness probe. lb-bot has no cheap dedicated health
        route, so it rides on `/api/summary`, and only the reachability verdict is
        passed on — the summary itself is a large object about the Fill-gaps
        workspace that no client renders.

        `routes` is what this hub can actually proxy. A client ships independently
        of the hub and the hub is a long-running process, so "my client is newer
        than the hub it is talking to" is a permanent condition, not an edge case —
        and without this it surfaces as a button that does nothing, because a 404
        from an unknown route is a perfectly ordinary HTTP response. Advertising
        the list lets a client say which feature its hub is too old for.
        """
        return 200, json.dumps({
            "configured": True,
            "upstreamReachable": status == 200,
            "routes": sorted(f"{method} {path}" for method, path in self.routes),
        }).encode(), "application/json"


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
        raw = b""
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


SONIC = SonicProxy()
LB = LbProxy()
# Order matters only in that each returns None for paths outside its own prefix;
# the first one that claims the path answers it.
PROXIES: tuple[HttpProxy, ...] = (SONIC, LB)

# Set once main() builds the hub, so the inbound notify handler below can reach
# the connected devices. The proxy protocol class is constructed by `websockets`
# per connection and has no other handle on it.
HUB_INSTANCE: Optional["Hub"] = None

# The one inbound path: lb-bot telling the hub an album just landed. Not a proxy
# route — nothing is forwarded — so it lives outside LB_ROUTES, and it answers
# whether or not LBBOT_URL is configured, because this direction doesn't need it.
LB_NOTIFY_PATH = "/lb/notify"
_LB_NOTIFY_STR_MAX = 200


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
    for key in ("event", "release_mbid", "rgid", "artist", "album"):
        value = _as_str(payload.get(key), _LB_NOTIFY_STR_MAX)
        if value:
            frame["releaseMbid" if key == "release_mbid" else key] = value

    if HUB_INSTANCE is not None:
        await HUB_INSTANCE._broadcast(frame)  # noqa: SLF001 — same module
    dlog("LB notify ->", frame)
    return _http_json(200, {"ok": True})


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
        self._last_sq_progress_at = 0.0  # throttle the current record's cursor writes
        self._last_position_save_at = 0.0  # throttle position-only state persistence
        self._last_progress_sent = 0.0
        self._play_intent_at = 0.0  # monotonic time of the last user play/pause intent
        self._position_intent_at = 0.0  # monotonic time of the last seek/jump/skip intent
        self._pre_intent_position = 0  # position_ms just BEFORE that seek/jump/skip
        self._mirror_task: Optional[asyncio.Task] = None  # single mirror worker
        self._mirror_pending = False  # a newer snapshot is waiting to be written
        self._mirror_latest: tuple = ([], None, 0)  # (ids, current, position_ms)
        self._started_at = time.monotonic()
        self._load()

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

    # ----- persistence ----------------------------------------------------- #
    def _load(self) -> None:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                raw = f.read().strip()
            if not raw:
                log("state file empty, starting fresh")
                return
            data = json.loads(raw)
            s = data.get("session", {})
            self.session = Session(
                rev=s.get("rev", 0),
                active_device_id=None,  # nothing is live yet after a restart
                queue=s.get("queue", []),
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
            )
            # Saved-queue history (rolling, capped). Keyed by id for cheap upsert.
            # Re-sanitised on the way in so a state.json written by an older build (which
            # stored client values verbatim) can't keep breaking _save() forever.
            for rec in data.get("savedQueues", []):
                clean = self._sanitize_saved_queue(rec, SAVED_QUEUE_SONGS_HARD_MAX)
                if clean is not None:
                    self.saved_queues[clean["id"]] = clean
            for rid, at in (data.get("deletedSavedQueues") or {}).items():
                try:
                    self.deleted_saved_queues[rid] = int(at)
                except (TypeError, ValueError):
                    continue
            for d in data.get("devices", []):
                self.devices[d["id"]] = Device(
                    id=d["id"], name=d.get("name", "Unknown"),
                    platform=d.get("platform", "unknown"),
                    caps=d.get("caps", ["controller"]),
                    volume=d.get("volume", 100), last_seen=d.get("lastSeen", 0),
                )
            self._prune_devices()
            log(f"loaded state: {len(self.session.queue)} queued, {len(self.devices)} known devices")
        except FileNotFoundError:
            log("no prior state, starting fresh")
        except Exception as e:  # noqa: BLE001
            log("failed to load state:", e)

    def _prune_devices(self) -> None:
        """Drop known devices not seen within DEVICE_TTL_DAYS (0 = keep forever).

        Runs on load only: the registry accretes a row per device that ever
        connected, so without this a long-lived hub grows a stale picker list.
        """
        if DEVICE_TTL_DAYS <= 0:
            return
        cutoff = int(time.time() * 1000) - int(DEVICE_TTL_DAYS * 86_400_000)
        stale = [d.id for d in self.devices.values()
                 if d.id != self.session.active_device_id and d.last_seen < cutoff]
        for did in stale:
            del self.devices[did]
        if stale:
            log(f"pruned {len(stale)} device(s) not seen in {DEVICE_TTL_DAYS}d")

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
        }

    def _save(self) -> None:
        try:
            # Built inside the try: _saved_queues_list() sorts records, and a persistence
            # failure must not escape into whichever act handler happened to trigger it.
            data = {
                "session": self.session.snapshot(),
                "savedQueues": self._saved_queues_list(),
                "deletedSavedQueues": self.deleted_saved_queues,
                "devices": [
                    {"id": d.id, "name": d.name, "platform": d.platform, "caps": d.caps,
                     "volume": d.volume, "lastSeen": d.last_seen}
                    for d in self.devices.values()
                ],
            }
            os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_PATH)
        except Exception as e:  # noqa: BLE001
            log("failed to save state:", e)

    # ----- send helpers ---------------------------------------------------- #
    async def _send(self, dev: Optional[Device], obj: dict) -> None:
        if dev and dev.ws is not None:
            try:
                await dev.ws.send(json.dumps(obj))
            except Exception:  # noqa: BLE001 — drop; close handler will clean up
                pass

    async def _send_to(self, device_id: Optional[str], obj: dict) -> None:
        if device_id:
            await self._send(self.devices.get(device_id), obj)

    async def _broadcast(self, obj: dict) -> None:
        await asyncio.gather(
            *(self._send(d, obj) for d in self.devices.values() if d.online),
            return_exceptions=True,
        )

    async def _broadcast_session(self) -> None:
        self._save()
        # DIAG (pause-echo hunt): the authoritative state every client is about to receive.
        dlog(f"SESSION -> is_playing={self.session.is_playing} pos={self.session.position_ms} "
             f"idx={self.session.index} active={self.session.active_device_id}")
        await self._broadcast({"t": "session", **self.session.snapshot()})
        self._mirror_play_queue()

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
        # change as playback moves through the queue.
        cover_final = (prev or {}).get("coverImageUrl") or cover_image_url
        self.saved_queues[rid] = {
            "id": rid,
            "serverId": server_id if server_id is not None else (prev or {}).get("serverId"),
            "songs": list(s.queue),
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
        rec["songs"] = list(s.queue)
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
        songs = [
            {k: t[k] for k in cls.SQ_TRACK_FIELDS if k in t}
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
                log(f"AUTH REJECTED for {name!r}: got token "
                    f"{got[:4]!r}…(len {len(got)}), expected …(len {len(TOKEN)}) — "
                    f"check HUB_TOKEN (note: docker --env-file does NOT strip quotes)")
                await ws.send(json.dumps({"t": "error", "code": "auth", "message": "bad token"}))
                await self._close(ws, 4001, "auth")
                return

            dev = await self._register(msg.get("device", {}), ws)
            await self._send(dev, {
                "t": "welcome",
                "deviceId": dev.id,
                "session": self.session.snapshot(),
                "savedQueues": self._saved_queues_list(),
                "devices": [d.info(self.session.active_device_id) for d in self.devices.values()],
            })
            await self._broadcast_devices()
            log(f"+ {dev.name} ({dev.id[:8]}) connected; caps={dev.caps}")

            async for raw in ws:
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

    async def _register(self, desc: dict, ws: Any) -> Device:
        did = desc.get("id") or os.urandom(8).hex()
        dev = self.devices.get(did)
        if dev is None:
            dev = Device(id=did)
            self.devices[did] = dev
        # Evict a prior live socket for this device before adopting the new one.
        # Without this, a reconnect while the old WS is still half-open leaves
        # two sockets bound to one Device, both driving session state.
        old_ws = dev.ws
        dev.name = desc.get("name", dev.name)
        dev.platform = desc.get("platform", dev.platform)
        dev.caps = desc.get("caps", dev.caps)
        dev.online = True
        # Adopt the new socket BEFORE awaiting the old one's close: _disconnect bails
        # unless it owns dev.ws, so claiming it first is what stops the superseded
        # socket's teardown from marking the device offline / clearing the active slot.
        dev.ws = ws
        if old_ws is not None and old_ws is not ws:
            try:
                await self._close(old_ws, 4003, "superseded")
            except Exception:  # noqa: BLE001 — best-effort; its finally still runs
                pass
        dev.last_seen = int(time.time() * 1000)
        self._save()
        return dev

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
        log(f"- {dev.name} ({dev.id[:8]}) disconnected")
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
            self.session.active_device_id = None
            self.session.bump()
            await self._broadcast_session()
        await self._broadcast_devices()

    # ----- message dispatch ------------------------------------------------ #
    async def _on_message(self, dev: Device, msg: dict) -> None:
        t = msg.get("t")
        dev.last_seen = int(time.time() * 1000)
        if t == "act":
            await self._on_act(dev, msg)
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
                self.session.active_device_id = None
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
        # Only the active receiver is authoritative for live playback truth.
        if dev.id != self.session.active_device_id:
            return
        changed = False
        now = time.monotonic()
        if "positionMs" in msg:
            # Right after a seek/jump/skip the receiver may still emit an in-flight
            # 1 Hz report carrying the OLD position, which would rewind the scrubber.
            # Within the grace window, reject a report that sits closer to where we
            # just left than to where we intend to be — that's a stale tick. A report
            # near (or past) the new target is real progress and is accepted.
            report_pos = max(0, int(msg["positionMs"]))
            target = self.session.position_ms
            # "Stale" means the report still sits essentially AT the position we just
            # left. A relative compare (closer-to-pre than to-target) wrongly rejected
            # legitimate reports after a backward seek, where real progress from the
            # new target is still nearer the old spot than the target is.
            stale = (now - self._position_intent_at < INTENT_GRACE
                     and abs(report_pos - self._pre_intent_position) < 1500
                     and abs(report_pos - target) >= 1500)
            if not stale:
                self.session.position_ms = report_pos
            else:
                dlog(f"REPORT pos={report_pos} from {dev.name}/{dev.id[:8]} "
                     f"IGNORED(pos-grace; pre={self._pre_intent_position} target={target})")
        if "index" in msg:
            idx = self._clamp_index(int(msg["index"]))
            if idx != self.session.index:
                self.session.index = idx; changed = True
        if "isPlaying" in msg and msg["isPlaying"] != self.session.is_playing:
            # A report may have been sent BEFORE the receiver processed a fresh
            # play/pause command — accepting it would flip the user's intent
            # back (and the next transfer would then carry the wrong state).
            within_grace = now - self._play_intent_at < INTENT_GRACE
            # DIAG (pause-echo hunt): a report that contradicts current play-state.
            dlog(f"REPORT from {dev.name}/{dev.id[:8]} isPlaying={msg.get('isPlaying')} "
                 f"pos={msg.get('positionMs')} | is_playing={self.session.is_playing} "
                 f"{'IGNORED(grace)' if within_grace else 'APPLIED'}")
            if not within_grace:
                self.session.is_playing = bool(msg["isPlaying"]); changed = True

        if msg.get("ended"):
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
            if now - self._last_progress_sent >= PROGRESS_THROTTLE:
                self._last_progress_sent = now
                await self._broadcast({"t": "progress",
                                       "positionMs": self.session.position_ms,
                                       "index": self.session.index,
                                       "isPlaying": self.session.is_playing})

    async def _on_act(self, dev: Device, msg: dict) -> None:
        action = msg.get("action")
        s = self.session
        active = s.active_device_id
        # DIAG (pause-echo hunt): every act frame, with the fields that move play-state.
        dlog(f"ACT {action} from {dev.name}/{dev.id[:8]} "
             f"play={msg.get('play')} pos={msg.get('positionMs')} idx={msg.get('index')} "
             f"| pre is_playing={s.is_playing}")

        # Promote the sender to active when there's nothing playing yet.
        promoted = False
        if active is None and action in ("play", "setQueue"):
            s.active_device_id = active = dev.id
            promoted = True
            await self._broadcast_devices()

        # Acts that only make sense against a live receiver. We still apply the intent
        # (so the session stays coherent and clients mirror it), but the sender is told
        # the directive went nowhere instead of it vanishing silently.
        if active is None and action in ("pause", "playpause", "next", "previous",
                                         "jump", "seek", "volume"):
            await self._send(dev, {"t": "error", "code": "no_active_device",
                                   "message": f"{action}: no device is currently active"})

        if action == "setQueue":
            # Flush where we left the OUTGOING queue before it becomes "previous".
            self._touch_saved_queue_progress()
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
            s.bump()
            # Only push a load to the active receiver if it ISN'T the device that
            # sent the queue. When a device publishes the queue it's already
            # playing locally (e.g. Feishin claiming active), so echoing do:load
            # back would reload/restart it.
            if active and active != dev.id:
                await self._send_to(active, {"t": "do", "cmd": "load",
                                             "tracks": s.queue, "index": s.index,
                                             "positionMs": s.position_ms, "play": s.is_playing})
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
            await self._send_to(active, {"t": "do", "cmd": "queueChanged",
                                         "tracks": s.queue, "index": s.index})
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action == "clear":
            # Flush where we left off BEFORE detaching, so the cleared queue stays
            # resumable from the history, then stop pointing the live session at it.
            self._touch_saved_queue_progress()
            s.queue = []
            s.index = 0
            s.position_ms = 0
            s.is_playing = False
            s.saved_queue_id = None
            s.source_kind = "manual"
            s.source_name = None
            self._mark_play_intent()
            self._rebuild_order()
            s.bump()
            # `queueChanged` carries a track list and receivers ignore an empty one,
            # so an emptied queue needs its own command to actually stop the device.
            await self._send_to(active, {"t": "do", "cmd": "clear"})
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
                await self._send_to(active, {"t": "do", "cmd": "clear"})
            else:
                await self._send_to(active, {"t": "do", "cmd": "queueChanged",
                                             "tracks": s.queue, "index": s.index})
                if jump_after_remove:
                    await self._send_to(active, {"t": "do", "cmd": "jump",
                                                 "index": s.index})
            await self._broadcast_session()
            await self._broadcast_saved_queues()

        elif action == "play":
            s.is_playing = True; self._mark_play_intent(); s.bump()
            if promoted and s.queue:
                # The sender just took over an orphaned session (e.g. the previous
                # device was force-stopped). It has no idea what the session holds, so
                # a bare do:play would resume ITS own stale local queue/position —
                # hand it the session's queue + resume point instead (PROTOCOL §5.1).
                await self._send_to(active, {"t": "do", "cmd": "load",
                                             "tracks": s.queue, "index": s.index,
                                             "positionMs": s.position_ms, "play": True})
            else:
                await self._send_to(active, {"t": "do", "cmd": "play"})
            await self._broadcast_session()

        elif action == "pause":
            s.is_playing = False; self._mark_play_intent(); s.bump()
            await self._send_to(active, {"t": "do", "cmd": "pause"})
            await self._broadcast_session()

        elif action == "playpause":
            s.is_playing = not s.is_playing; self._mark_play_intent(); s.bump()
            await self._send_to(active, {"t": "do", "cmd": "play" if s.is_playing else "pause"})
            await self._broadcast_session()

        elif action in ("next", "previous"):
            nxt = self._step_index(1 if action == "next" else -1)
            if nxt is None:
                s.is_playing = False; self._mark_play_intent(); s.bump()
                await self._send_to(active, {"t": "do", "cmd": "pause"})
            else:
                self._mark_position_intent()
                s.index = nxt; s.position_ms = 0; s.bump()
                await self._send_to(active, {"t": "do", "cmd": "jump", "index": s.index})
            await self._broadcast_session()

        elif action == "jump":
            self._mark_position_intent()
            s.index = self._clamp_index(int(msg["index"])); s.position_ms = 0; s.bump()
            await self._send_to(active, {"t": "do", "cmd": "jump", "index": s.index})
            await self._broadcast_session()

        elif action == "seek":
            self._mark_position_intent()
            s.position_ms = max(0, int(msg["positionMs"])); s.bump()
            await self._send_to(active, {"t": "do", "cmd": "seek", "positionMs": s.position_ms})
            await self._broadcast_session()

        elif action == "repeat":
            s.repeat = msg.get("mode", "none"); s.bump()
            await self._send_to(active, {"t": "do", "cmd": "setRepeat", "mode": s.repeat})
            await self._broadcast_session()

        elif action == "shuffle":
            s.shuffle = bool(msg.get("on", False))
            self._rebuild_order(); s.bump()
            await self._send_to(active, {"t": "do", "cmd": "setShuffle",
                                         "on": s.shuffle, "order": s.order})
            await self._broadcast_session()

        elif action == "volume":
            level = max(0, min(100, int(msg.get("level", 100))))
            active_dev = self.devices.get(active) if active else None
            if active_dev is not None:
                active_dev.volume = level
            await self._send_to(active, {"t": "do", "cmd": "setVolume", "level": level})
            await self._broadcast_devices()

        elif action in ("favorite", "rating"):
            # Relay a purpose-built directive with whitelisted fields only — echoing
            # the raw act back would forward the sender's auth token to the receiver.
            relay = {"t": "do", "cmd": action, "id": msg.get("id")}
            if action == "favorite":
                relay["favorite"] = bool(msg.get("favorite", True))
            else:
                relay["rating"] = max(0, min(5, int(msg.get("rating", 0))))
            await self._send_to(active, relay)

        elif action == "transfer":
            await self._transfer(msg.get("target"), msg.get("play"))

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
                await self._send(dev, {"t": "error", "code": "unknown_saved_queue",
                                       "message": f"no saved queue {rid!r}"})

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
            await self._send(dev, {"t": "error", "code": "bad_action",
                                   "message": f"unknown action {action!r}"})

    async def _transfer(self, target_id: Optional[str], play: Optional[bool]) -> None:
        s = self.session
        target = self.devices.get(target_id) if target_id else None
        if not target or not target.online:
            await self._broadcast({"t": "error", "code": "target_offline",
                                   "message": "target device is not connected"})
            return
        if "receiver" not in (target.caps or []):
            await self._send(target, {"t": "error", "code": "not_a_receiver",
                                      "message": "target device cannot play audio"})
            log(f"transfer rejected: {target.name} has no 'receiver' cap")
            return

        # Default: preserve the play state (Spotify behaviour — transferring a
        # paused session keeps it paused). Captured BEFORE release, because the
        # old device's final report sets is_playing False.
        if play is None:
            play = s.is_playing

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
                await self._send(target, {"t": "do", "cmd": "play" if play else "pause"})
                await self._broadcast_session()
            log(f"transfer -> {target.name}: already active, no-op")
            return

        old = self.devices.get(old_id) if old_id else None
        if old is not None and old.online:
            fut = asyncio.get_running_loop().create_future()
            old.release_future = fut
            await self._send(old, {"t": "do", "cmd": "release"})
            try:
                await asyncio.wait_for(fut, RELEASE_TIMEOUT)
            except asyncio.TimeoutError:
                log(f"release timed out for {old.name}; using last known position")
            finally:
                old.release_future = None
        # s.position_ms / s.index now reflect the old device's final report (or last known).

        s.active_device_id = target_id
        s.is_playing = play
        self._mark_play_intent()
        s.bump()
        # Broadcast BEFORE the load so the target learns it is active first —
        # otherwise its do:load side effects (play events) fire while it still
        # believes another device is active, which can misroute them.
        await self._broadcast_session()
        await self._broadcast_devices()
        await self._send(target, {"t": "do", "cmd": "load",
                                  "tracks": s.queue, "index": s.index,
                                  "positionMs": s.position_ms, "play": play})
        log(f"transfer -> {target.name} @ index {s.index}, {s.position_ms}ms")


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
            # Checked ahead of the proxies: /lb/notify sits under the lb prefix
            # but is inbound, so LbProxy would 404 it against its route table.
            result = await _handle_lb_notify(self, path, request_headers,
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


async def main() -> None:
    if not TOKEN:
        log("WARNING: HUB_TOKEN is empty — the hub will accept any client. Set it!")
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

    async with websockets.serve(hub.handler, HOST, PORT,
                                ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                                max_size=4 * 1024 * 1024, **serve_kwargs):
        await stop  # run until a stop signal arrives

    health_server.close()
    await health_server.wait_closed()
    hub._save()
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
