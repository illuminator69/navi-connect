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
POSITION_SAVE_THROTTLE = 10.0  # seconds between state writes driven by position-only reports

# --- AudioMuse proxy tuning ---
PROXY_MAX_INFLIGHT = 4      # concurrent upstream calls; a hung core must not eat the
                            # default thread pool and stall the 1 Hz progress fan-out
PROXY_TIMEOUT = 20          # seconds per upstream socket op (urllib has one knob for
                            # connect+read); Tier 2 is in-memory lookups, so fail fast
PROXY_CACHE_TTL = 60.0      # shared result cache — both clients asking the same
                            # question cost one upstream call
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
# AudioMuse-AI Tier-2 proxy  (design: ../DESIGN-hub-audiomuse-proxy.md)
#
# Plain HTTP served on the WebSocket port, so the clients reach AudioMuse through
# the one component that is already authenticated and remotely reachable. They
# stop holding the AudioMuse base URL, the AudioMuse bearer token and (on the
# fingerprint call) the Navidrome password.
#
# This is a WHITELIST, not a forwarder: HUB_TOKEN is on every device, so a
# pass-through proxy would hand it AudioMuse's analysis/clustering/embedding
# admin endpoints. Unknown path or method → 404 with no upstream call; unknown
# parameters are dropped rather than relayed.
# --------------------------------------------------------------------------- #
#   (method, hub path) -> upstream method/path + what may be forwarded
#     params: allowed query parameters (GET)
#     body:   allowed top-level JSON body keys (POST)
#     nd:     inject HUB_ND_USER/HUB_ND_PASS (only the fingerprint route needs them)
#     cache:  cacheable for PROXY_CACHE_TTL
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


def _sonic_upstream_blocking(method: str, url: str,
                             body: Optional[bytes]) -> tuple[int, bytes, str]:
    """One upstream call, off the event loop (same shape as the savePlayQueue mirror).

    Upstream status codes are passed through (503 cold index, 404 unanalyzed track,
    400 feature disabled) so the clients' existing fail-soft branches keep working.
    """
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if AUDIOMUSE_TOKEN:
        req.add_header("Authorization", f"Bearer {AUDIOMUSE_TOKEN}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as r:
            return (r.status, r.read(PROXY_MAX_RESPONSE),
                    r.headers.get("Content-Type") or "application/json")
    except urllib.error.HTTPError as e:
        try:
            payload = e.read(PROXY_MAX_RESPONSE)
        except Exception:  # noqa: BLE001
            payload = b""
        return e.code, payload, e.headers.get("Content-Type") or "application/json"
    except Exception as e:  # noqa: BLE001 — never raise into the WS loop
        log("audiomuse proxy: upstream failed:", e)
        return 502, b'{"error":"audiomuse unreachable"}', "application/json"


class SonicProxy:
    """Route table + auth + concurrency cap + shared short-TTL result cache."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(PROXY_MAX_INFLIGHT)
        self._cache: dict[str, tuple[float, int, bytes, str]] = {}

    @property
    def enabled(self) -> bool:
        # An empty HUB_TOKEN already means "accept any client"; refusing to run the
        # proxy in that state is what stops it becoming an open relay to AudioMuse.
        return bool(AUDIOMUSE_URL and TOKEN)

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

    def _cache_put(self, key: str, status: int, body: bytes, ctype: str) -> None:
        # Only cache successes — a cold-index 503 must not stick for a minute.
        if status != 200:
            return
        if len(self._cache) >= PROXY_CACHE_MAX:
            for old in sorted(self._cache, key=lambda k: self._cache[k][0])[:PROXY_CACHE_MAX // 4]:
                del self._cache[old]
        self._cache[key] = (time.monotonic() + PROXY_CACHE_TTL, status, body, ctype)

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
        key = json.dumps([route, sorted(params), body], sort_keys=True, default=str)
        if spec.get("cache"):
            hit = self._cache_get(key)
            if hit is not None:
                return hit

        url = AUDIOMUSE_URL + spec["path"]
        # nd creds are injected here, not logged: keep them out of the cache key too.
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode() if body is not None else None

        async with self._sem:
            status, data, ctype = await asyncio.to_thread(
                _sonic_upstream_blocking, spec["method"], url, payload)

        if spec.get("probe"):
            status, data, ctype = self._augment_probe(status, data)
        if spec.get("cache"):
            self._cache_put(key, status, data, ctype)
        return status, data, ctype

    @staticmethod
    def _augment_probe(status: int, data: bytes) -> tuple[int, bytes, str]:
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

    async def handle(self, protocol: Any, raw_path: str,
                     headers: Any, method: str) -> Optional[tuple]:
        """Return an (status, headers, body) triple to answer as plain HTTP, or None
        to let the request continue into the WebSocket handshake."""
        path, _, query = raw_path.partition("?")
        path = path.rstrip("/") or "/"
        if path != "/sonic" and not path.startswith("/sonic/"):
            return None  # not ours — WS handshake (or its own 404) proceeds

        if not self.enabled:
            reason = ("AUDIOMUSE_URL is unset" if not AUDIOMUSE_URL
                      else "HUB_TOKEN is empty (refusing to run as an open relay)")
            # The probe still answers, so clients learn Tier 2 is off instead of erroring.
            if (method, path) == ("GET", "/sonic/clap/stats"):
                return _http_json(200, {"configured": False, "upstreamReachable": False})
            return _http_json(503, {"error": f"audiomuse proxy disabled: {reason}"})

        auth = (headers.get("Authorization") or "") if headers is not None else ""
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
            return _http_json(401, {"error": "unauthorized"})

        spec = SONIC_ROUTES.get((method, path))
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
            log("audiomuse proxy failed:", e)
            return _http_json(502, {"error": "proxy failure"})
        dlog(f"SONIC {method} {path} -> {status} ({len(data)}B)")
        return _http_response(status, data, ctype)


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
            for rec in data.get("savedQueues", []):
                rid = rec.get("id")
                if rid:
                    self.saved_queues[rid] = rec
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
        try:
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
        ordered = sorted(self.saved_queues.values(),
                         key=lambda r: r.get("updatedAt", 0), reverse=True)
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
        for rid in [r for r in self.saved_queues if r not in keep]:
            del self.saved_queues[rid]

    def _tombstone(self, rid: str) -> None:
        """Remember a deletion so a client's stale local copy can't re-add it."""
        self.deleted_saved_queues[rid] = int(time.time() * 1000)
        if len(self.deleted_saved_queues) > TOMBSTONE_MAX:
            for old in sorted(self.deleted_saved_queues,
                              key=lambda k: self.deleted_saved_queues[k],
                              )[:len(self.deleted_saved_queues) - TOMBSTONE_MAX]:
                del self.deleted_saved_queues[old]

    def _upsert_saved_queue(self, rid: str, kind: str, name: Optional[str],
                            server_id: Optional[str] = None,
                            cover_image_url: Optional[str] = None) -> None:
        """Create/refresh the record for [rid] from the current live queue. Preserves a
        user-assigned name and the original createdAt across refreshes."""
        s = self.session
        if not rid or not s.queue:
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

    def _merge_saved_queues(self, incoming: list[dict]) -> bool:
        """Union-merge client-supplied offline history by id (newest updatedAt wins),
        field by field. Returns True if anything changed.

        Field-level rather than wholesale replace: a client's copy of a record can be
        missing metadata the hub has (name/sourceName/cover), and a newer updatedAt
        from an unrelated edit shouldn't erase it.
        """
        changed = False
        for raw in incoming[:SAVED_QUEUES_MAX * 2]:
            if not isinstance(raw, dict):
                continue
            rid = raw.get("id")
            if not rid or not raw.get("songs"):
                continue
            if rid in self.deleted_saved_queues:
                continue  # deleted on the hub; the client's copy is stale
            rec = {k: raw[k] for k in self.SQ_FIELDS if k in raw}
            cur = self.saved_queues.get(rid)
            if cur is None:
                self.saved_queues[rid] = rec
                changed = True
                continue
            # The live session's record is authoritative — never let an offline copy
            # overwrite the queue that's playing right now.
            if rid == self.session.saved_queue_id:
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
    async def handler(self, ws: Any) -> None:
        dev: Optional[Device] = None
        # PROTOCOL §2: clients connect to /connect. `/` is still accepted (older
        # builds of both clients used it) but logged so it can be retired.
        path = (getattr(ws, "request", None) and ws.request.path) or getattr(ws, "path", "/")
        path = (path or "/").split("?")[0].rstrip("/") or "/"
        if path not in ("/connect", "/"):
            await ws.close(4004, "bad path")
            return
        if path == "/":
            dlog("client connected on deprecated path '/'; use /connect")
        try:
            # First frame MUST be hello + valid token.
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                await ws.close(4002, "protocol")
                return
            token_ok = not TOKEN or hmac.compare_digest(str(msg.get("token") or ""), TOKEN)
            if msg.get("t") != "hello" or not token_ok:
                got = str(msg.get("token") or "")
                name = (msg.get("device") or {}).get("name", "?")
                log(f"AUTH REJECTED for {name!r}: got token "
                    f"{got[:4]!r}…(len {len(got)}), expected …(len {len(TOKEN)}) — "
                    f"check HUB_TOKEN (note: docker --env-file does NOT strip quotes)")
                await ws.send(json.dumps({"t": "error", "code": "auth", "message": "bad token"}))
                await ws.close(4001, "auth")
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
            await ws.close(4002, "protocol")
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
                await old_ws.close(4003, "superseded")
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
                s.saved_queue_id = msg.get("savedQueueId") or self._mint_saved_queue_id()
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
                rec["name"] = (msg.get("name") or "").strip() or None
                rec["updatedAt"] = int(time.time() * 1000)
                await self._broadcast_saved_queues()

        elif action == "deleteSavedQueue":
            rid = msg.get("id")
            if rid and rid in self.saved_queues:
                del self.saved_queues[rid]
                self._tombstone(rid)
                if s.saved_queue_id == rid:
                    # Otherwise _sync_current_saved_queue re-creates it on the next
                    # queue edit and the deleted row reappears.
                    s.saved_queue_id = None
                    s.bump()
                    await self._broadcast_session()
                await self._broadcast_saved_queues()

        elif action == "syncSavedQueues":
            # A (re)connecting client pushes its local/offline history up; union-merge
            # and rebroadcast the reconciled list to everyone.
            incoming = msg.get("queues")
            if isinstance(incoming, list) and self._merge_saved_queues(incoming):
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
            result = await SONIC.handle(self, path, request_headers, self.http_method)
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
    hub = Hub()
    log(f"navi-connect hub on ws://{HOST}:{PORT}  "
        f"(Navidrome: {NAVIDROME_URL or '<unset>'}, "
        f"mirror={'on' if MIRROR_PLAYQUEUE and NAVIDROME_URL else 'off'})")

    proxy_protocol = _build_proxy_protocol() if AUDIOMUSE_URL else None
    if SONIC.enabled and proxy_protocol is not None:
        log(f"audiomuse proxy on http://{HOST}:{PORT}/sonic/* -> {AUDIOMUSE_URL}")
    elif AUDIOMUSE_URL:
        # Still routed (so /sonic/* answers a clean 503 instead of a WS handshake
        # error), just refusing to forward.
        log("audiomuse proxy NOT forwarding — see the warning above")

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
