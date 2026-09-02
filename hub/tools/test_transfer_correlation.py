#!/usr/bin/env python3
"""
Regression tests for operation-scoped transfer acknowledgement (PROTOCOL §7.2).

Before `transferId`, a transfer's replies were correlated to a DEVICE, not to the
attempt that asked for them. Three things followed, all of which end with the hub
holding a session state nobody's speaker agrees with:

1. A `loaded` that arrives after its own transfer gave up satisfies the NEXT
   transfer to the same device — the hub calls a load good on the strength of a
   reply to a question it stopped asking.
2. ABA: transfer A to X times out, B moves to Y, C moves back to X. A's expired
   rollback saw "X is active" — which was true again, for a completely different
   reason — and rolled back C.
3. A duplicate `released` from an earlier handoff completes the release future
   armed for the current one, ending phase 1 on an old answer (and, because
   `released` carries a position, rewinding the session to where that device was
   two transfers ago).

A receiver advertising `transferAckV2` must echo the id; one that doesn't is
correlated by device exactly as before, so older clients are unaffected.

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

PORT = 4795
TOKEN = "test-token"
URL = f"ws://localhost:{PORT}"

V2 = ["receiver", "controller", "loadAck", "transferAckV2"]


class Client:
    """A receiver whose acknowledgements can be delayed, withheld, or misaddressed."""

    def __init__(self, name, device_id=None, caps=V2):
        self.name = name
        self.device_id = device_id or name.lower()
        self.caps = list(caps)
        self.ws = None
        self.active = None
        self.is_playing = None
        self.dos = []
        self.errors = []
        # None = never ack a load. True/False = ack with that ok, after ack_delay.
        self.ack_loads = True
        self.ack_delay = 0.0
        # When set, every `loaded` we send carries THIS id instead of the one the
        # directive asked for — how a straggler from an earlier attempt looks.
        self.force_tid = None
        self.auto_release = True
        self.loads = []     # every do:load we were sent, in order

    async def connect(self):
        self.ws = await websockets.connect(URL)
        await self.ws.send(json.dumps({"t": "hello", "token": TOKEN, "device": {
            "id": self.device_id, "name": self.name, "platform": "test", "caps": self.caps}}))
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
                elif t == "session":
                    self.active = msg.get("activeDeviceId")
                    self.is_playing = msg.get("isPlaying")
                elif t == "error":
                    self.errors.append(msg)
                elif t == "do":
                    self.dos.append(msg)
                    if msg.get("cmd") == "release" and self.auto_release:
                        await self.send(t="released", index=0, positionMs=0,
                                        transferId=msg.get("transferId"))
                    if msg.get("cmd") == "load":
                        self.loads.append(msg)
                        asyncio.create_task(self._ack_load(msg))
        except websockets.ConnectionClosed:
            pass

    async def _ack_load(self, do):
        if self.ack_loads is None:
            return
        if self.ack_delay:
            await asyncio.sleep(self.ack_delay)
        await self.send(t="loaded", ok=self.ack_loads,
                        transferId=self.force_tid or do.get("transferId"),
                        error=None if self.ack_loads else "refused")

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

        x = Client("X", device_id="x")
        y = Client("Y", device_id="y")
        ctl = Client("Ctl", device_id="ctl", caps=["controller"])
        for c in (x, y, ctl):
            await c.connect()

        await ctl.act(action="setQueue", tracks=TRACKS, index=0, positionMs=0, play=True)
        await asyncio.sleep(0.3)

        # ----- 0. the directives carry an id and a budget ------------------- #
        await ctl.act(action="transfer", target="x")
        await asyncio.sleep(0.8)
        if not x.loads:
            failures.append("transfer sent no do:load at all")
        else:
            first = x.loads[-1]
            if not first.get("transferId"):
                failures.append("do:load carries no transferId")
            if not first.get("timeoutMs"):
                failures.append("do:load carries no timeoutMs budget")
        if ctl.active != "x":
            failures.append(f"clean transfer did not commit: active={ctl.active}")

        # ----- 1. a stale `loaded` cannot satisfy a later transfer ---------- #
        # X answers every load with the id of the FIRST one — a straggler from an
        # attempt that has already been given up on. The hub must not accept it, so
        # this transfer times out and rolls back like any unanswered load.
        await ctl.act(action="transfer", target="y")
        await asyncio.sleep(0.8)
        stale_tid = x.loads[0].get("transferId")
        x.force_tid = stale_tid
        ctl.errors.clear()
        await ctl.act(action="transfer", target="x")
        await asyncio.sleep(1.0)
        if ctl.active != "x":
            failures.append("transfer should commit optimistically while the ack is pending")
        await asyncio.sleep(11.0)          # LOAD_TIMEOUT is 10 s
        if ctl.active == "x":
            failures.append("a `loaded` bearing a stale transferId completed a later transfer")
        if "load_failed" not in ctl.err_codes():
            failures.append(f"stale ack should read as no ack at all: {ctl.err_codes()}")
        x.force_tid = None

        # ----- 2. ABA: an expired rollback must not undo a later transfer --- #
        # A: -> X, never answered.  B: -> Y.  C: -> X, answered properly.
        # When A's 10 s expires, X is active again — but because of C, not A.
        await ctl.act(action="transfer", target="y")
        await asyncio.sleep(0.8)
        x.ack_loads = None                 # A will get no answer
        await ctl.act(action="transfer", target="x")     # A
        await asyncio.sleep(0.6)
        await ctl.act(action="transfer", target="y")     # B
        await asyncio.sleep(0.6)
        x.ack_loads = True                 # C is answered normally
        ctl.errors.clear()
        await ctl.act(action="transfer", target="x")     # C
        await asyncio.sleep(1.0)
        if ctl.active != "x":
            failures.append(f"C should have committed X, got {ctl.active}")
        await asyncio.sleep(10.5)          # A's deadline passes here
        if ctl.active != "x":
            failures.append(f"A's expired rollback undid C: active={ctl.active}")
        if "load_failed" in ctl.err_codes():
            failures.append("A's expired rollback emitted a load_failed over a healthy session")

        # ----- 3. a duplicate `released` cannot complete a later handoff ---- #
        # X stops answering release and replays an ANCIENT released (an id from a
        # handoff several transfers ago, carrying position 0). The hub must ignore it:
        # the position must not be adopted, so the target still loads at 42 s.
        x.auto_release = False
        old_release_tid = "t1"
        await ctl.act(action="setQueue", tracks=TRACKS, index=1, positionMs=42_000, play=True)
        await asyncio.sleep(0.4)
        await ctl.act(action="transfer", target="x")
        await asyncio.sleep(1.2)
        ctl.errors.clear()
        y.loads.clear()
        await ctl.act(action="transfer", target="y")
        await asyncio.sleep(0.15)
        await x.send(t="released", index=0, positionMs=0, transferId=old_release_tid)
        await asyncio.sleep(3.0)           # RELEASE_TIMEOUT is 1.5 s
        if ctl.active != "y":
            failures.append(f"handoff did not complete: active={ctl.active}")
        loaded_at = y.loads[-1].get("positionMs") if y.loads else None
        if loaded_at == 0:
            failures.append("a stale `released` rewound the session to position 0")

        # ----- 3b. a device that re-claims the slot itself is not rolled back  #
        # The generation guard, not the cancel-on-supersede one: X is handed a load it
        # never answers, then drops and comes back and takes the orphaned session over
        # (act:play with an empty active slot). That is a DIFFERENT assignment of the
        # same id, so an id-only check would let the abandoned load's deadline tear down
        # a session X legitimately owns.
        x.ack_loads = None
        ctl.errors.clear()
        await ctl.act(action="transfer", target="x")
        await asyncio.sleep(0.6)
        await x.ws.close()
        await asyncio.sleep(0.5)
        x.ack_loads = True
        await x.connect()
        await x.act(action="play")
        await asyncio.sleep(0.5)
        if ctl.active != "x":
            failures.append(f"X did not re-claim the orphaned session: {ctl.active}")
        await asyncio.sleep(10.5)          # the abandoned load's deadline passes
        if ctl.active != "x":
            failures.append(f"an abandoned load rolled back a session X re-claimed: {ctl.active}")
        if "load_failed" in ctl.err_codes():
            failures.append("abandoned load emitted load_failed over a re-claimed session")

        # ----- 4. a receiver without transferAckV2 is correlated as before -- #
        x.auto_release = True              # done withholding; keep this handoff quick
        legacy = Client("Legacy", device_id="legacy", caps=["receiver", "loadAck"])
        await legacy.connect()
        legacy.force_tid = "nonsense"      # it echoes garbage; nobody asked it to echo
        ctl.errors.clear()
        await ctl.act(action="transfer", target="legacy")
        await asyncio.sleep(1.0)
        if ctl.active != "legacy":
            failures.append("legacy receiver's transfer was rejected")
        await asyncio.sleep(11.0)
        if ctl.active != "legacy":
            failures.append(f"legacy ack was discarded for a bad id: active={ctl.active}")
        if "load_failed" in ctl.err_codes():
            failures.append("legacy receiver held to a cap it never advertised")

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
    print("PASS - transfer correlation: id+budget-on-load/stale-loaded-rejected/"
          "aba-rollback/stale-released-rejected/legacy-exempt")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
