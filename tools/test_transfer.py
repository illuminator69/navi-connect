#!/usr/bin/env python3
"""
End-to-end proof of transfer-with-resume against a live hub subprocess.

Spins up hub.py, connects two receivers (A, B) and a controller, plays on A,
advances the position, then transfers to B and asserts B resumes at the same
queue index AND position. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile

import websockets

PORT = 4799
TOKEN = "test-token"
URL = f"ws://localhost:{PORT}"


class Client:
    def __init__(self, name, caps):
        self.name = name
        self.caps = caps
        self.ws = None
        self.loads = []      # (index, positionMs) for each do:load received
        self.released = False
        self.playing = False
        self.index = 0
        self.pos = 0

    async def connect(self):
        self.ws = await websockets.connect(URL)
        await self.ws.send(json.dumps({
            "t": "hello", "token": TOKEN,
            "device": {"id": self.name.lower(), "name": self.name,
                       "platform": "test", "caps": self.caps},
        }))
        asyncio.create_task(self._loop())
        await asyncio.sleep(0.2)

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("t") == "do":
                cmd = msg["cmd"]
                if cmd == "load":
                    self.index = msg["index"]; self.pos = msg["positionMs"]
                    self.playing = msg.get("play", True)
                    self.loads.append((self.index, self.pos, len(msg.get("tracks", []))))
                elif cmd == "play":
                    self.playing = True
                elif cmd == "pause":
                    self.playing = False
                elif cmd == "jump":          # a real receiver tracks queue jumps
                    self.index = msg["index"]; self.pos = 0
                elif cmd == "seek":
                    self.pos = msg["positionMs"]
                elif cmd == "release":
                    self.playing = False
                    await self.ws.send(json.dumps({"t": "report", "positionMs": self.pos,
                                                   "index": self.index, "isPlaying": False}))
                    await self.ws.send(json.dumps({"t": "released"}))
                    self.released = True

    async def act(self, **kw):
        await self.ws.send(json.dumps({"t": "act", **kw}))

    async def report(self, **kw):
        if "positionMs" in kw:
            self.pos = kw["positionMs"]
        if "index" in kw:
            self.index = kw["index"]
        await self.ws.send(json.dumps({"t": "report", **kw}))


async def main():
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(PORT),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1"}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures = []
    try:
        await asyncio.sleep(1.2)  # let the hub bind

        a = Client("Recv-A", ["receiver", "controller"])
        b = Client("Recv-B", ["receiver", "controller"])
        ctl = Client("Ctl", ["controller"])
        await a.connect(); await b.connect(); await ctl.connect()

        tracks = [{"id": f"t{i}", "title": f"T{i}", "durationMs": 180000} for i in range(5)]
        await a.act(action="setQueue", tracks=tracks, index=0, play=True)
        await asyncio.sleep(0.3)

        # A published its own queue, so it must NOT receive a do:load echo (it's
        # already playing what it set). Regression guard for the empty-queue bug.
        if a.loads:
            failures.append("A should not receive do:load for its own setQueue (echo loop)")
        # A "plays" to track 2, 65s in.
        await a.act(action="jump", index=2)
        await asyncio.sleep(0.2)
        await a.report(positionMs=65000, index=2, isPlaying=True)
        await asyncio.sleep(0.3)

        # Transfer to B.
        await ctl.act(action="transfer", target="recv-b", play=True)
        await asyncio.sleep(0.6)

        if not a.released:
            failures.append("A was not asked to release on transfer")
        if not b.loads:
            failures.append("B never received do:load on transfer")
        else:
            idx, pos, ntracks = b.loads[-1]
            if idx != 2:
                failures.append(f"B resumed at index {idx}, expected 2")
            if not (60000 <= pos <= 70000):
                failures.append(f"B resumed at {pos}ms, expected ~65000")
            if ntracks != 5:
                failures.append(f"B received {ntracks} tracks, expected 5 (queue not carried)")
            if not b.playing:
                failures.append("B is not playing after transfer")

        # Transfer BACK to A — it must get the full queue so it can resume (the
        # exact regression: Feishin pausing and not coming back).
        await ctl.act(action="transfer", target="recv-a", play=True)
        await asyncio.sleep(0.6)
        if not a.loads:
            failures.append("A never received do:load on transfer-back (would stay paused)")
        elif a.loads[-1][2] != 5:
            failures.append(f"A got {a.loads[-1][2]} tracks on transfer-back, expected 5")

        # Pause, then transfer WITHOUT a play flag — the paused state must carry
        # (no auto-unpause on the target).
        await ctl.act(action="pause")
        await asyncio.sleep(0.3)
        await ctl.act(action="transfer", target="recv-b")
        await asyncio.sleep(0.6)
        if b.playing:
            failures.append("transferring a paused session auto-unpaused the target")

        # STALE-REPORT RACE: a 1 Hz report sent before the receiver processed a
        # fresh pause must NOT flip the session back to playing (intent grace).
        await ctl.act(action="play")
        await asyncio.sleep(0.3)
        await ctl.act(action="pause")
        await b.report(positionMs=66000, index=2, isPlaying=True)  # stale in-flight tick
        await asyncio.sleep(0.3)
        await ctl.act(action="transfer", target="recv-a")
        await asyncio.sleep(0.6)
        if a.playing:
            failures.append("stale isPlaying report overrode a fresh pause intent "
                            "(transfer auto-unpaused)")
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
    print("PASS - queue carried on transfer both ways; B resumed at idx 2 ~65000ms; "
          "no echo to publisher")


if __name__ == "__main__":
    asyncio.run(main())
