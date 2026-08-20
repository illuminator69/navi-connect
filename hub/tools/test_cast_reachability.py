#!/usr/bin/env python3
"""
Regression tests for the cast device model (PROTOCOL §12.2 reachability).

Covers the three things that made "transfer to my TV" show a playing bar over a
silent speaker:

1. `deviceState {reachable:false}` from a bridge marks the speaker unreachable
   even though the BRIDGE's socket is perfectly healthy, and a transfer to it is
   rejected with `target_unreachable` instead of silently committed.
2. A speaker that goes unreachable *while holding the session* pauses it and
   relinquishes the active slot — nothing else can notice, because the bridge
   never disconnects.
3. A transfer target that advertises `loadAck` and then fails (or never answers)
   has the active slot rolled back to the previous device, with a `load_failed`
   error broadcast. A target that does NOT advertise `loadAck` is untouched, so
   older clients keep working.

Spins up a real hub subprocess. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile

import websockets

PORT = 4797
TOKEN = "test-token"
URL = f"ws://localhost:{PORT}"


class Client:
    def __init__(self, name, device_id=None, caps=("receiver", "controller"),
                 platform="test", bridged_by=None, ack_loads=None):
        self.name = name
        self.device_id = device_id or name.lower()
        self.caps = list(caps)
        self.platform = platform
        self.bridged_by = bridged_by
        # None = never answer a do:load (a wedged receiver). True/False = ack with that ok.
        self.ack_loads = ack_loads
        self.ws = None
        self.active = None
        self.is_playing = None
        self.devices = {}     # id -> full device info dict
        self.dos = []
        self.errors = []

    async def connect(self):
        self.ws = await websockets.connect(URL)
        desc = {"id": self.device_id, "name": self.name,
                "platform": self.platform, "caps": self.caps}
        if self.bridged_by:
            desc["bridgedBy"] = self.bridged_by
        await self.ws.send(json.dumps({"t": "hello", "token": TOKEN, "device": desc}))
        asyncio.create_task(self._loop())
        await asyncio.sleep(0.25)

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    s = msg.get("session", {})
                    self.active = s.get("activeDeviceId")
                    self.is_playing = s.get("isPlaying")
                    self._apply_devices(msg.get("devices", []))
                elif t == "session":
                    self.active = msg.get("activeDeviceId")
                    self.is_playing = msg.get("isPlaying")
                elif t == "devices":
                    self._apply_devices(msg.get("devices", []))
                elif t == "error":
                    self.errors.append(msg)
                elif t == "do":
                    self.dos.append(msg)
                    if msg.get("cmd") == "release":
                        # Answer the handoff immediately; otherwise every transfer in
                        # this test costs the hub's full RELEASE_TIMEOUT.
                        await self.send(t="released", index=0, positionMs=0)
                    if msg.get("cmd") == "load" and self.ack_loads is not None:
                        await self.send(t="loaded", ok=self.ack_loads,
                                        error=None if self.ack_loads else "no route to host")
        except websockets.ConnectionClosed:
            pass

    def _apply_devices(self, devices):
        for d in devices:
            self.devices[d["id"]] = d

    async def act(self, **kw):
        await self.ws.send(json.dumps({"t": "act", **kw}))

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    def err_codes(self):
        return [e.get("code") for e in self.errors]


TRACKS = [{"id": f"t{i}", "title": f"T{i}", "streamUrl": f"http://x/{i}"} for i in range(3)]


async def main():
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(PORT),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1"}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []
    try:
        await asyncio.sleep(1.2)

        desktop = Client("Desktop", caps=["receiver", "controller", "loadAck"])
        # A bridged speaker: the socket belongs to Desktop, the device is the TV.
        tv = Client("TV", device_id="cast-tv1", caps=["receiver", "loadAck"],
                    platform="chromecast", bridged_by="desktop", ack_loads=True)
        await desktop.connect()
        await tv.connect()

        # ----- 1. presence != reachability --------------------------------- #
        row = desktop.devices.get("cast-tv1") or {}
        if not row.get("online"):
            failures.append("bridged TV should be online (its bridge holds the socket)")
        if row.get("reachable") is not None:
            failures.append(f"unreported reachability should be unknown, got {row.get('reachable')}")
        if row.get("bridgedBy") != "desktop":
            failures.append(f"bridgedBy not recorded: {row.get('bridgedBy')}")

        await tv.send(t="deviceState", reachable=False, appRunning=False)
        await asyncio.sleep(0.3)
        row = desktop.devices.get("cast-tv1") or {}
        if row.get("reachable") is not False:
            failures.append(f"reachable=false not propagated: {row}")
        if not row.get("online"):
            failures.append("an unreachable speaker must stay online — the bridge is fine")

        # ----- 2. transfer to an unreachable speaker is refused ------------- #
        await desktop.act(action="setQueue", tracks=TRACKS, index=0, positionMs=0, play=True)
        await asyncio.sleep(0.3)
        desktop.errors.clear()
        await desktop.act(action="transfer", target="cast-tv1")
        await asyncio.sleep(0.4)
        if "target_unreachable" not in desktop.err_codes():
            failures.append(f"expected target_unreachable, got {desktop.err_codes()}")
        if desktop.active != "desktop":
            failures.append(f"refused transfer moved the active slot to {desktop.active}")

        # ----- 3. it becomes transferable once it answers ------------------- #
        await tv.send(t="deviceState", reachable=True, appRunning=True)
        await asyncio.sleep(0.3)
        desktop.errors.clear()
        await desktop.act(action="transfer", target="cast-tv1")
        await asyncio.sleep(0.8)
        if desktop.active != "cast-tv1":
            failures.append(f"transfer to a reachable speaker failed: active={desktop.active}")
        if desktop.err_codes():
            failures.append(f"clean transfer emitted errors: {desktop.err_codes()}")

        # ----- 4. going unreachable mid-session pauses and relinquishes ----- #
        await tv.send(t="deviceState", reachable=False)
        await asyncio.sleep(0.4)
        if desktop.active is not None:
            failures.append(f"speaker went away but active is still {desktop.active}")
        if desktop.is_playing:
            failures.append("speaker went away but the session still reads playing")

        # ----- 5. a failed load rolls the active slot back ------------------ #
        await tv.send(t="deviceState", reachable=True)
        await asyncio.sleep(0.3)
        await desktop.act(action="play")
        await asyncio.sleep(0.4)
        if desktop.active != "desktop":
            failures.append(f"desktop should have taken the orphaned session, got {desktop.active}")

        tv.ack_loads = False          # the speaker now refuses to start
        desktop.errors.clear()
        await desktop.act(action="transfer", target="cast-tv1")
        await asyncio.sleep(1.0)
        if desktop.active != "desktop":
            failures.append(f"failed load left active at {desktop.active}, expected rollback")
        if "load_failed" not in desktop.err_codes():
            failures.append(f"expected load_failed, got {desktop.err_codes()}")
        if desktop.is_playing:
            failures.append("failed load left the session reading playing")

        # ----- 6. a silent receiver times out too --------------------------- #
        tv.ack_loads = None           # never answers at all
        desktop.errors.clear()
        await desktop.act(action="transfer", target="cast-tv1")
        await asyncio.sleep(0.5)
        if desktop.active != "cast-tv1":
            failures.append("transfer should commit optimistically while the ack is pending")
        await asyncio.sleep(11.0)     # LOAD_TIMEOUT is 10 s
        if desktop.active != "desktop":
            failures.append(f"wedged receiver was not rolled back: active={desktop.active}")
        if "load_failed" not in desktop.err_codes():
            failures.append(f"timeout produced no load_failed: {desktop.err_codes()}")

        # ----- 7. a receiver without loadAck is never held to it ------------ #
        legacy = Client("Legacy", device_id="legacy", caps=["receiver"])
        await legacy.connect()
        desktop.errors.clear()
        await desktop.act(action="transfer", target="legacy")
        await asyncio.sleep(1.0)
        if desktop.active != "legacy":
            failures.append("a legacy receiver's transfer was rolled back")
        await asyncio.sleep(11.0)
        if desktop.active != "legacy":
            failures.append(f"legacy receiver rolled back after timeout: {desktop.active}")
        if "load_failed" in desktop.err_codes():
            failures.append("legacy receiver produced a spurious load_failed")

    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        try:
            os.unlink(state)
        except OSError:
            pass

    if failures:
        for f in failures:
            print("FAIL -", f)
        return 1
    print("PASS - cast reachability: presence-vs-reachable/unreachable-refused/"
          "mid-session-drop/load-ack-rollback/ack-timeout/legacy-exempt")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
