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
import json
import os
import random
import time
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
TOKEN = os.environ.get("HUB_TOKEN", "")
STATE_PATH = os.environ.get("HUB_STATE", "/data/state.json")

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
MIRROR_PLAYQUEUE = os.environ.get("HUB_MIRROR_PLAYQUEUE", "true").lower() == "true"
ND_USER = os.environ.get("HUB_ND_USER", "")
ND_PASS = os.environ.get("HUB_ND_PASS", "")

DEBUG = os.environ.get("HUB_DEBUG", "").lower() in ("1", "true", "yes")

PING_INTERVAL = 10  # seconds (matches Feishin's heartbeat)
PING_TIMEOUT = 10
RELEASE_TIMEOUT = 1.5  # seconds to wait for an old device to hand off
PROGRESS_THROTTLE = 1.0  # seconds between fanned-out progress broadcasts
INTENT_GRACE = 2.0  # seconds during which receiver reports can't contradict a
                    # fresh user play/pause intent (guards against stale
                    # in-flight 1 Hz reports flipping the state back)
MIRROR_DEBOUNCE = 2.5  # seconds to coalesce rapid savePlayQueue mirror writes


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
# Hub
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self) -> None:
        self.session = Session()
        self.devices: dict[str, Device] = {}
        self._last_progress_sent = 0.0
        self._play_intent_at = 0.0  # monotonic time of the last user play/pause intent
        self._position_intent_at = 0.0  # monotonic time of the last seek/jump/skip intent
        self._pre_intent_position = 0  # position_ms just BEFORE that seek/jump/skip
        self._mirror_task: Optional[asyncio.Task] = None  # single mirror worker
        self._mirror_pending = False  # a newer snapshot is waiting to be written
        self._mirror_latest: tuple = ([], None, 0)  # (ids, current, position_ms)
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
            )
            for d in data.get("devices", []):
                self.devices[d["id"]] = Device(
                    id=d["id"], name=d.get("name", "Unknown"),
                    platform=d.get("platform", "unknown"),
                    caps=d.get("caps", ["controller"]),
                    volume=d.get("volume", 100), last_seen=d.get("lastSeen", 0),
                )
            log(f"loaded state: {len(self.session.queue)} queued, {len(self.devices)} known devices")
        except FileNotFoundError:
            log("no prior state, starting fresh")
        except Exception as e:  # noqa: BLE001
            log("failed to load state:", e)

    def _save(self) -> None:
        data = {
            "session": self.session.snapshot(),
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
        if old_ws is not None and old_ws is not ws:
            try:
                await old_ws.close(4003, "superseded")
            except Exception:  # noqa: BLE001 — best-effort; its finally still runs
                pass
        dev.name = desc.get("name", dev.name)
        dev.platform = desc.get("platform", dev.platform)
        dev.caps = desc.get("caps", dev.caps)
        dev.online = True
        dev.ws = ws
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
            if "positionMs" in msg:
                self.session.position_ms = int(msg["positionMs"])
            if "index" in msg:
                self.session.index = int(msg["index"])
            if self.session.active_device_id == dev.id:
                self.session.active_device_id = None
            fut = dev.release_future
            if fut and not fut.done():
                fut.set_result(True)
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
            report_pos = int(msg["positionMs"])
            target = self.session.position_ms
            stale = (now - self._position_intent_at < INTENT_GRACE
                     and abs(report_pos - self._pre_intent_position) < abs(report_pos - target))
            if not stale:
                self.session.position_ms = report_pos
            else:
                dlog(f"REPORT pos={report_pos} from {dev.name}/{dev.id[:8]} "
                     f"IGNORED(pos-grace; pre={self._pre_intent_position} target={target})")
        if "index" in msg and msg["index"] != self.session.index:
            self.session.index = int(msg["index"]); changed = True
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

        if changed:
            self.session.bump()
            await self._broadcast_session()
        else:
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
        if active is None and action in ("play", "setQueue"):
            s.active_device_id = active = dev.id
            await self._broadcast_devices()

        if action == "setQueue":
            s.queue = msg.get("tracks", [])
            s.index = self._clamp_index(int(msg.get("index", 0)))
            s.position_ms = max(0, int(msg.get("positionMs", 0)))
            s.is_playing = bool(msg.get("play", True))
            self._mark_play_intent()
            self._rebuild_order()
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
            s.bump()
            await self._send_to(active, {"t": "do", "cmd": "queueChanged",
                                         "tracks": s.queue, "index": s.index})
            await self._broadcast_session()

        elif action == "clear":
            s.queue = []
            s.index = 0
            s.position_ms = 0
            s.is_playing = False
            self._mark_play_intent()
            self._rebuild_order()
            s.bump()
            # `queueChanged` carries a track list and receivers ignore an empty one,
            # so an emptied queue needs its own command to actually stop the device.
            await self._send_to(active, {"t": "do", "cmd": "clear"})
            await self._broadcast_session()

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

        elif action == "play":
            s.is_playing = True; self._mark_play_intent(); s.bump()
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
            if active:
                self.devices[active].volume = level
            await self._send_to(active, {"t": "do", "cmd": "setVolume", "level": level})
            await self._broadcast_devices()

        elif action in ("favorite", "rating"):
            await self._send_to(active, {"t": "do", **{k: v for k, v in msg.items() if k != "t"}})

        elif action == "transfer":
            await self._transfer(msg.get("target"), msg.get("play"))

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

        # Default: preserve the play state (Spotify behaviour — transferring a
        # paused session keeps it paused). Captured BEFORE release, because the
        # old device's final report sets is_playing False.
        if play is None:
            play = s.is_playing

        old_id = s.active_device_id
        if old_id and old_id != target_id and self.devices.get(old_id, Device("x")).online:
            old = self.devices[old_id]
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


async def main() -> None:
    if not TOKEN:
        log("WARNING: HUB_TOKEN is empty — the hub will accept any client. Set it!")
    if MIRROR_PLAYQUEUE and not NAVIDROME_URL:
        log("WARNING: HUB_MIRROR_PLAYQUEUE is on but NAVIDROME_URL is unset — "
            "the savePlayQueue mirror is disabled. Set it in .env.")
    hub = Hub()
    log(f"navi-connect hub on ws://{HOST}:{PORT}  "
        f"(Navidrome: {NAVIDROME_URL or '<unset>'}, "
        f"mirror={'on' if MIRROR_PLAYQUEUE and NAVIDROME_URL else 'off'})")
    async with websockets.serve(hub.handler, HOST, PORT,
                                ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                                max_size=4 * 1024 * 1024):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
