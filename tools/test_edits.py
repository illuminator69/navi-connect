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

    async def connect(self):
        self.ws = await websockets.connect(URL)
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

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    self._apply_session(msg.get("session", {}))
                    for d in msg.get("devices", []):
                        self.online[d["id"]] = d["online"]
                elif t == "session":
                    self._apply_session(msg)
                elif t == "devices":
                    for d in msg.get("devices", []):
                        self.online[d["id"]] = d["online"]
        except websockets.ConnectionClosed:
            pass

    async def act(self, **kw):
        await self.ws.send(json.dumps({"t": "act", **kw}))

    async def report(self, **kw):
        await self.ws.send(json.dumps({"t": "report", **kw}))


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


if __name__ == "__main__":
    asyncio.run(main())
