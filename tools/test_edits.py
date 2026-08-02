#!/usr/bin/env python3
"""
Regression tests for two hub fixes from the 2026-07-20 audit:

1. Shuffle order is PRESERVED across queue edits (enqueue/remove) instead of
   being fully re-randomized on every edit.
2. A reconnect (a second socket for the same device id) does NOT let the old
   socket's teardown clobber the live device — it stays online, active, playing.

Spins up a real hub subprocess; asserts on the authoritative `session`/`welcome`
frames. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

import websockets

PORT = 4798
TOKEN = "test-token"
URL = f"ws://localhost:{PORT}"


class Client:
    def __init__(self, name, device_id=None, caps=("receiver", "controller")):
        self.name = name
        self.device_id = device_id or name.lower()
        self.caps = list(caps)
        self.ws = None
        self.order = None        # latest session.order
        self.active = None       # latest session.activeDeviceId
        self.is_playing = None
        self.online = {}         # id -> online from devices/welcome
        self.saved_queue_id = None   # latest session.savedQueueId
        self.saved_queues = []       # latest savedQueues broadcast (or welcome)
        self.position = None         # latest session.positionMs
        self.dos = []                # every `do` directive received
        self.errors = []             # every `error` frame received

    async def connect(self, url=URL):
        self.ws = await websockets.connect(url)
        await self.ws.send(json.dumps({
            "t": "hello", "token": TOKEN,
            "device": {"id": self.device_id, "name": self.name,
                       "platform": "test", "caps": self.caps},
        }))
        asyncio.create_task(self._loop())
        await asyncio.sleep(0.2)

    def _apply_session(self, s):
        self.order = s.get("order")
        self.active = s.get("activeDeviceId")
        self.is_playing = s.get("isPlaying")
        self.saved_queue_id = s.get("savedQueueId")
        self.position = s.get("positionMs")

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    self._apply_session(msg.get("session", {}))
                    self.saved_queues = msg.get("savedQueues", [])
                    for d in msg.get("devices", []):
                        self.online[d["id"]] = d["online"]
                elif t == "session":
                    self._apply_session(msg)
                elif t == "savedQueues":
                    self.saved_queues = msg.get("queues", [])
                elif t == "devices":
                    for d in msg.get("devices", []):
                        self.online[d["id"]] = d["online"]
                elif t == "do":
                    self.dos.append(msg)
                elif t == "error":
                    self.errors.append(msg)
        except websockets.ConnectionClosed:
            pass

    async def act(self, **kw):
        await self.ws.send(json.dumps({"t": "act", **kw}))

    async def report(self, **kw):
        await self.ws.send(json.dumps({"t": "report", **kw}))

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    def last_do(self, cmd):
        return next((d for d in reversed(self.dos) if d.get("cmd") == cmd), None)


def expected_after_remove(order, at):
    """Mirror hub._order_after_remove for assertion."""
    return [v - 1 if v > at else v for v in order if v != at]


async def main():
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(PORT),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1"}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []
    try:
        await asyncio.sleep(1.2)  # let the hub bind

        r = Client("Recv")
        c = Client("Ctl", caps=["controller"])
        await r.connect(); await c.connect()

        # ----- shuffle order preservation ---------------------------------- #
        tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(6)]
        await r.act(action="setQueue", tracks=tracks, index=0, play=True)
        await asyncio.sleep(0.2)
        await c.act(action="shuffle", on=True)
        await asyncio.sleep(0.3)
        o1 = c.order
        if not o1 or sorted(o1) != list(range(6)):
            failures.append(f"shuffle didn't produce a full order permutation: {o1}")
        elif o1[0] != 0:
            failures.append(f"shuffle order should keep current track first: {o1}")

        # enqueue one track at the end -> new raw index 6 appended to order,
        # the existing upcoming order untouched.
        await c.act(action="enqueue", tracks=[{"id": "t6", "title": "T6"}], at="end")
        await asyncio.sleep(0.3)
        o2 = c.order
        if [v for v in (o2 or []) if v < 6] != o1:
            failures.append(f"enqueue reshuffled the existing order: {o1} -> {o2}")
        if o2 and 6 not in o2:
            failures.append(f"enqueued track missing from order: {o2}")

        # remove raw index 3 -> that entry drops, higher indices renumber, the
        # rest of the shuffled order preserved.
        await c.act(action="remove", index=3)
        await asyncio.sleep(0.3)
        o3 = c.order
        exp = expected_after_remove(o2, 3)
        if o3 != exp:
            failures.append(f"remove reshuffled instead of patching: got {o3}, expected {exp}")

        # ----- reconnect clobber ------------------------------------------- #
        # Recv is the active, playing device. A second socket for the SAME id
        # connects (a reconnect); the old socket must not, on teardown, mark the
        # device offline / pause the session.
        if c.active != "recv":
            failures.append(f"active device should be 'recv', got {c.active!r}")

        r2 = Client("Recv", device_id="recv")  # same id, new socket
        await r2.connect()
        await asyncio.sleep(0.5)  # old socket gets closed (4003) + its finally runs

        probe = Client("Probe", caps=["controller"])
        await probe.connect()
        await asyncio.sleep(0.3)
        if probe.active != "recv":
            failures.append(f"reconnect dropped active device: active={probe.active!r}")
        if probe.is_playing is not True:
            failures.append(f"reconnect paused the session: isPlaying={probe.is_playing!r}")
        if probe.online.get("recv") is not True:
            failures.append(f"reconnect left device offline: online={probe.online.get('recv')!r}")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        os.unlink(state)

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - shuffle order preserved across enqueue/remove; reconnect kept "
          "the device online/active/playing")


async def test_prune():
    """A device not seen within HUB_DEVICE_TTL_DAYS is dropped on load; a fresh one is kept."""
    port = 4799
    url = f"ws://localhost:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    now = int(time.time() * 1000)
    with open(state, "w", encoding="utf-8") as f:
        json.dump({
            "session": {"queue": [], "index": 0},
            "devices": [
                {"id": "old", "name": "Old", "lastSeen": now - 10 * 86_400_000},   # 10 days ago
                {"id": "fresh", "name": "Fresh", "lastSeen": now},
            ],
        }, f)
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_DEVICE_TTL_DAYS": "1", "HUB_HEALTH_PORT": str(port + 1)}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []
    try:
        await asyncio.sleep(1.2)
        probe = Client("Probe", device_id="probe", caps=["controller"])
        await probe.connect(url)
        await asyncio.sleep(0.3)
        if "old" in probe.online:
            failures.append(f"stale device not pruned on load: {probe.online}")
        if "fresh" not in probe.online:
            failures.append(f"fresh device wrongly pruned: {probe.online}")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        os.unlink(state)

    if failures:
        print("FAIL (prune):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - stale device pruned on load; fresh device kept")


async def test_saved_queues():
    """Hub-owned saved-queue history: setQueue records a queue, enqueue grows the SAME
    record (no fork), a new setQueue adds a second record + flips 'current', a client's
    offline history union-merges, and delete removes."""
    port = 4800
    url = f"ws://localhost:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_HEALTH_PORT": str(port + 1)}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []

    def rec(client, rid):
        return next((r for r in client.saved_queues if r["id"] == rid), None)

    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await c.connect(url)

        # setQueue with an explicit id + kind → one record, current, tracks captured.
        tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album", sourceName="Album One",
                    coverImageUrl="http://cover/one.jpg")
        await asyncio.sleep(0.3)
        q1 = rec(c, "q1")
        if q1 is None:
            failures.append(f"setQueue didn't create a saved-queue record: {c.saved_queues}")
        elif q1["songCount"] != 4 or q1["sourceKind"] != "album":
            failures.append(f"record fields wrong: {q1}")
        elif q1.get("coverImageUrl") != "http://cover/one.jpg":
            failures.append(f"cover not stored on the record: {q1}")
        if c.saved_queue_id != "q1":
            failures.append(f"session.savedQueueId not set: {c.saved_queue_id!r}")

        # A reorder of the SAME session republishes the same id: the record is refreshed
        # in place (no fork), and its frozen identity — name, kind, cover — survives even
        # though the republish carries different/blank values.
        reordered = [tracks[2], tracks[0], tracks[3], tracks[1]]
        await r.act(action="setQueue", tracks=reordered, index=0, play=True,
                    savedQueueId="q1", sourceKind="manual", sourceName=None,
                    coverImageUrl="http://cover/other.jpg")
        await asyncio.sleep(0.3)
        if len([x for x in c.saved_queues if x["id"] == "q1"]) != 1:
            failures.append("reorder forked a second record for the same session")
        q1r = rec(c, "q1")
        if q1r is None or q1r["sourceName"] != "Album One" or q1r["sourceKind"] != "album":
            failures.append(f"reorder clobbered the frozen name/kind: {q1r}")
        elif q1r.get("coverImageUrl") != "http://cover/one.jpg":
            failures.append(f"reorder clobbered the frozen cover: {q1r}")
        elif [t["id"] for t in q1r["songs"]] != [t["id"] for t in reordered]:
            failures.append(f"reorder didn't refresh the record's tracks: {q1r}")

        # enqueue → SAME record grows (no new id minted).
        await c.act(action="enqueue", tracks=[{"id": "t4", "title": "T4"}], at="end")
        await asyncio.sleep(0.3)
        q1b = rec(c, "q1")
        if len([r for r in c.saved_queues if r["id"] == "q1"]) != 1:
            failures.append("enqueue forked a new record instead of growing q1")
        if q1b is None or q1b["songCount"] != 5:
            failures.append(f"enqueue didn't grow the record: {q1b}")

        # A second setQueue → a second record; q1 becomes 'previous', q2 current.
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q2", sourceKind="radio")
        await asyncio.sleep(0.3)
        if rec(c, "q1") is None or rec(c, "q2") is None:
            failures.append(f"expected both q1 and q2 in history: {[r['id'] for r in c.saved_queues]}")
        if c.saved_queue_id != "q2":
            failures.append(f"current didn't flip to q2: {c.saved_queue_id!r}")

        # Offline history union-merge: a client pushes a record the hub doesn't have.
        now = int(time.time() * 1000)
        await c.act(action="syncSavedQueues", queues=[{
            "id": "q3", "songs": [{"id": "z0"}], "songCount": 1, "currentIndex": 0,
            "positionMs": 0, "sourceKind": "manual", "updatedAt": now,
        }])
        await asyncio.sleep(0.3)
        if rec(c, "q3") is None:
            failures.append(f"syncSavedQueues didn't merge offline record: {[r['id'] for r in c.saved_queues]}")

        # Delete removes it everywhere.
        await c.act(action="deleteSavedQueue", id="q3")
        await asyncio.sleep(0.3)
        if rec(c, "q3") is not None:
            failures.append("deleteSavedQueue left the record behind")

        # Rename persists on the record.
        await c.act(action="renameSavedQueue", id="q1", name="My Mix")
        await asyncio.sleep(0.3)
        q1c = rec(c, "q1")
        if q1c is None or q1c.get("name") != "My Mix":
            failures.append(f"renameSavedQueue didn't stick: {q1c}")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        os.unlink(state)

    if failures:
        print("FAIL (saved queues):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - saved-queue history: record/enqueue-grow/second-record/merge/delete/rename")


async def test_audit_fixes():
    """Regressions for the 2026-07-26 audit fixes:

    takeover sends do:load (not a bare do:play) · self-transfer is a no-op ·
    a late/spurious `released` is ignored · a null source name is backfilled by a
    later publish · the current record survives eviction · `clear` detaches the
    session from its record · a deleted record stays deleted across a client re-sync.
    """
    port = 4801
    url = f"ws://localhost:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_HEALTH_PORT": str(port + 1)}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []

    def rec(client, rid):
        return next((r for r in client.saved_queues if r["id"] == rid), None)

    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        r2 = Client("Recv2", device_id="recv2")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await r2.connect(url); await c.connect(url)

        tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]

        # ----- name backfill ------------------------------------------------ #
        # Born without a name (Navic publishes before its collection metadata
        # resolves); a later publish of the SAME id must be able to fill the hole.
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album")
        await asyncio.sleep(0.3)
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album", sourceName="Album One")
        await asyncio.sleep(0.3)
        q1 = rec(c, "q1")
        if q1 is None or q1.get("sourceName") != "Album One":
            failures.append(f"null sourceName wasn't backfilled by a later publish: {q1}")

        # An established name still wins over a later null.
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album")
        await asyncio.sleep(0.3)
        if (rec(c, "q1") or {}).get("sourceName") != "Album One":
            failures.append("a later null publish clobbered an established sourceName")

        # ----- current record is never evicted ------------------------------ #
        now = int(time.time() * 1000)
        await c.act(action="syncSavedQueues", queues=[
            {"id": f"bulk{i}", "songs": [{"id": "z"}], "songCount": 1, "currentIndex": 0,
             "positionMs": 0, "sourceKind": "manual", "updatedAt": now + i}
            for i in range(24)
        ])
        await asyncio.sleep(0.4)
        if rec(c, "q1") is None:
            failures.append("the current (playing) record was evicted by a bulk sync")

        # ----- transfer sends the receiver a load --------------------------- #
        await r2.report(positionMs=1000, index=0, isPlaying=True)  # ignored: not active
        await c.act(action="transfer", target="recv2")
        # > RELEASE_TIMEOUT: this Client never answers do:release, so the handoff
        # completes on the timeout path. Waiting it out also means the `released`
        # frame sent below is genuinely late (no pending release_future).
        await asyncio.sleep(2.0)
        if r2.last_do("load") is None:
            failures.append(f"transfer didn't send do:load to the target: {r2.dos}")

        # r2 is the active receiver; establish a real playback position.
        await r2.report(positionMs=45_000, index=0, isPlaying=True)
        await asyncio.sleep(0.4)

        # ----- late/spurious `released` is ignored -------------------------- #
        # `r` handed off already; a straggler frame from it must not rewind the session.
        await r.send(t="released", positionMs=999_999, index=3)
        await asyncio.sleep(0.4)
        probe = Client("Probe", device_id="probe", caps=["controller"])
        await probe.connect(url)
        await asyncio.sleep(0.3)
        if probe.position == 999_999 or probe.active != "recv2":
            failures.append(f"late `released` was honoured: pos={probe.position} active={probe.active}")

        # ----- self-transfer is a no-op ------------------------------------- #
        before = len(r2.dos)
        await c.act(action="transfer", target="recv2", play=True)
        await asyncio.sleep(0.5)
        new_loads = [d for d in r2.dos[before:] if d.get("cmd") == "load"]
        if new_loads:
            failures.append(f"transfer to the already-active device reloaded it: {new_loads}")

        # ----- takeover of an orphaned session gets do:load ------------------ #
        await r2.ws.close()          # force-stop: active device vanishes
        await asyncio.sleep(0.5)
        r3 = Client("Recv3", device_id="recv3")
        await r3.connect(url)
        await r3.act(action="play")
        await asyncio.sleep(0.5)
        load = r3.last_do("load")
        if load is None:
            failures.append(f"takeover sent no do:load (bare do:play?): {r3.dos}")
        elif load.get("positionMs") != 45_000:
            failures.append(f"takeover load didn't carry the session position: {load.get('positionMs')}")

        # ----- clear detaches the session from its record -------------------- #
        await c.act(action="clear")
        await asyncio.sleep(0.4)
        if c.saved_queue_id is not None:
            failures.append(f"clear left session.savedQueueId set: {c.saved_queue_id!r}")
        if rec(c, "q1") is None:
            failures.append("clear deleted the history record (it should stay resumable)")

        # ----- delete is tombstoned ------------------------------------------ #
        await c.act(action="deleteSavedQueue", id="q1")
        await asyncio.sleep(0.3)
        if rec(c, "q1") is not None:
            failures.append("deleteSavedQueue left the record behind")
        await c.act(action="syncSavedQueues", queues=[{
            "id": "q1", "songs": [{"id": "t0"}], "songCount": 1, "currentIndex": 0,
            "positionMs": 0, "sourceKind": "album", "updatedAt": int(time.time() * 1000) + 5000,
        }])
        await asyncio.sleep(0.4)
        if rec(c, "q1") is not None:
            failures.append("a client re-sync resurrected a deleted record (no tombstone)")

        # ----- transfer to a controller-only device is rejected --------------- #
        before_ctl = len(c.dos)
        await c.act(action="transfer", target="ctl")
        await asyncio.sleep(0.4)
        if [d for d in c.dos[before_ctl:] if d.get("cmd") == "load"]:
            failures.append("transferred to a device without the 'receiver' cap")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        os.unlink(state)

    if failures:
        print("FAIL (audit fixes):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - audit fixes: takeover-load/self-transfer-noop/late-released/"
          "name-backfill/eviction-exempt/clear-detach/tombstone/receiver-cap")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_prune())
    asyncio.run(test_saved_queues())
    asyncio.run(test_audit_fixes())
