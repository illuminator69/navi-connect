#!/usr/bin/env python3
"""
Tests for the command echo (`do.seq` / `report.applied`, PROTOCOL §5.2-5.3) and
the reconnect path it shares code with.

The hub used to judge a receiver's report by a clock: for INTENT_GRACE (2 s)
after a play/pause it ignored a report that disagreed, and after that it believed
anything. Five probes showed what that costs. Each is a case here:

  P1  a 1 Hz tick sampled before a skip moved the session back to the old index
      (three `session` frames for one `next`);
  P2  a receiver that reports "not playing" 0.3 s after `play` was ignored, and
      the session said "playing" for good;
  P3  a "playing" sample 2.2 s after a pause flipped the session back;
  P4  a transfer target's report of what it was doing BEFORE the load overwrote
      the session's index and position;
  P5  a device reconnecting behind a socket that no longer answers got its
      `welcome` 20 s late.

A receiver with the cap "cmdEcho" is now judged by the number it echoes, so P1-P4
come out right with no clock. One without the cap keeps the clock, on purpose:
for it only P1 (a new index guard) and P5 change, and the cases below pin P2-P4
as they are so that "unchanged" is something a run can show.

Also here: the single `_do` helper (a grep over hub.py), which directives carry a
`seq`, no `do` before `welcome`, frames from a superseded socket, the 12 s
backstop against a receiver that never confirms, the two limits on belief (an
empty queue, a refused transfer load), and the /health counters.

Spins up real hub subprocesses, each on a free port. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

from fake_receiver import FakeReceiver
from test_edits import TOKEN, _free_port, unlink_state

HUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

ECHO = ["receiver", "controller", "loadAck", "transferAckV2", "cmdEcho"]
PLAIN = ["receiver", "controller", "loadAck", "transferAckV2"]
STAMPED = ("load", "play", "pause", "seek", "jump", "queueChanged", "clear")


def tracks(n, prefix="t"):
    return [{"id": f"{prefix}{i}", "title": f"T{i}", "durationMs": 180000} for i in range(n)]


class Hub:
    """One hub subprocess on a free port, with its own state file and log."""

    def __init__(self, debug=False):
        self.port, self.health_port = _free_port(), _free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
        self.debug = debug
        self.proc = None

    async def __aenter__(self):
        env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(self.port),
               "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": self.state,
               "HUB_HOST": "127.0.0.1", "HUB_HEALTH_PORT": str(self.health_port),
               "HUB_DEBUG": "1" if self.debug else ""}
        self.proc = subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                                     stdout=self.logfile, stderr=subprocess.STDOUT)
        # The health port is bound first, the WebSocket port right after it.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.health_port), 0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.4)
        return self

    async def __aexit__(self, *exc):
        self.proc.terminate()
        try:
            await asyncio.to_thread(self.proc.wait, 5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        unlink_state(self.state)
        self.logfile.close()
        os.unlink(self.logfile.name)

    def log(self):
        with open(self.logfile.name, encoding="utf-8", errors="replace") as f:
            return f.read()

    async def health(self):
        def get():
            with urllib.request.urlopen(f"http://127.0.0.1:{self.health_port}/", timeout=3) as r:
                return json.loads(r.read())
        return await asyncio.to_thread(get)


class Peer:
    """A scripted client. Records every frame; plays a receiver only when told to.

    `auto_echo`: carry out each directive on a small model of a player, move
    `applied` to its `seq` and report at once, which is the contract.
    `auto_release`: answer `do:release` with a final report and `released`.
    Both are switched off by a case that wants to break the contract by hand.
    """

    def __init__(self, name, device_id=None, caps=("receiver", "controller")):
        self.name = name
        self.device_id = device_id or name.lower()
        self.caps = list(caps)
        self.ws = None
        self.frames = []          # every frame, in arrival order
        self.sessions = []        # every `session` frame
        self.progress = []
        self.dos = []
        self.errors = []
        self.acks = []
        self.welcome = None
        self.hello_at = self.welcome_at = None
        self.active = self.index = self.position = self.is_playing = None
        self.rev = None
        self.queue_len = None
        self.auto_echo = False
        self.auto_release = False
        self.on_do = None         # async callable(msg), run before auto_echo
        self.on_frame = None      # async callable(msg), for any frame
        self.applied = 0
        self.model = {"index": 0, "positionMs": 0, "isPlaying": False}

    async def connect(self, url, settle=0.25):
        self.ws = await websockets.connect(url, max_size=4 * 1024 * 1024)
        self.hello_at = time.monotonic()
        await self.ws.send(json.dumps({
            "t": "hello", "token": TOKEN,
            "device": {"id": self.device_id, "name": self.name,
                       "platform": "test", "caps": self.caps}}))
        asyncio.create_task(self._loop())
        if settle:
            await asyncio.sleep(settle)

    def _mirror(self, s):
        self.active = s.get("activeDeviceId", self.active)
        self.index = s.get("index", self.index)
        self.position = s.get("positionMs", self.position)
        self.is_playing = s.get("isPlaying", self.is_playing)
        self.rev = s.get("rev", self.rev)
        if "queue" in s:
            self.queue_len = len(s["queue"])

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                t = msg.get("t")
                if t == "welcome":
                    self.welcome, self.welcome_at = msg, time.monotonic()
                    self.applied = 0
                    self._mirror(msg.get("session", {}))
                elif t == "session":
                    self.sessions.append(msg)
                    self._mirror(msg)
                elif t == "progress":
                    self.progress.append(msg)
                    self._mirror(msg)
                elif t == "error":
                    self.errors.append(msg)
                elif t == "ack":
                    self.acks.append(msg)
                elif t == "do":
                    self.dos.append(msg)
                    if self.on_do is not None:
                        await self.on_do(msg)
                    if msg.get("cmd") == "release" and self.auto_release:
                        self.model["isPlaying"] = False
                        await self.report()
                        await self.send(t="released", transferId=msg.get("transferId"),
                                        index=self.model["index"],
                                        positionMs=self.model["positionMs"])
                    elif self.auto_echo and msg.get("cmd") in STAMPED:
                        self.apply(msg)
                        await self.report()
                if self.on_frame is not None:
                    await self.on_frame(msg)
        except websockets.ConnectionClosed:
            pass

    def apply(self, do):
        """Carry a directive out on the model, and move `applied` to its number."""
        cmd, m = do.get("cmd"), self.model
        if cmd == "load":
            m.update(index=do.get("index", 0), positionMs=do.get("positionMs", 0),
                     isPlaying=bool(do.get("play", True)))
        elif cmd == "play":
            m["isPlaying"] = True
        elif cmd == "pause":
            m["isPlaying"] = False
        elif cmd == "jump":
            m.update(index=do.get("index", 0), positionMs=0)
        elif cmd == "seek":
            m["positionMs"] = do.get("positionMs", 0)
        elif cmd == "queueChanged":
            m["index"] = do.get("index", m["index"])
        elif cmd == "clear":
            m.update(index=0, positionMs=0, isPlaying=False)
        if isinstance(do.get("seq"), int):
            self.applied = max(self.applied, do["seq"])

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    async def act(self, **kw):
        await self.send(t="act", **kw)

    async def report(self, **kw):
        """The model's state, with `applied` when this peer echoes. `kw` overrides."""
        frame = {"t": "report", **self.model}
        if "cmdEcho" in self.caps:
            frame["applied"] = self.applied
        frame.update(kw)
        await self.ws.send(json.dumps(frame))

    def last_do(self, cmd):
        return next((d for d in reversed(self.dos) if d.get("cmd") == cmd), None)

    def err_codes(self):
        return [e.get("code") for e in self.errors]

    async def close(self):
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


async def until(pred, timeout=2.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


async def send_together(peer, *frames):
    """Write several frames so they reach the hub in ONE read.

    Two back-to-back sends usually do, and "usually" would make the cases that
    depend on it pass on a hub that is wrong. TCP_CORK holds them until released.
    """
    sock = peer.ws.transport.get_extra_info("socket")
    cork = getattr(socket, "TCP_CORK", None)
    if cork is not None:
        sock.setsockopt(socket.IPPROTO_TCP, cork, 1)
    try:
        for f in frames:
            await peer.ws.send(json.dumps(f))
    finally:
        if cork is not None:
            sock.setsockopt(socket.IPPROTO_TCP, cork, 0)


# --------------------------------------------------------------------------- #
# 0. every directive is built in one place
# --------------------------------------------------------------------------- #
def case_single_helper(failures):
    with open(os.path.join(HUB_DIR, "hub.py"), encoding="utf-8") as f:
        src = f.read()
    # Any way of spelling a `do` frame literal: {"t": "do"…}, dict(t="do"…).
    literal = re.compile(r"""["']t["']\s*:\s*["']do["']|\bt\s*=\s*["']do["']""")
    hits = [(src.count("\n", 0, m.start()) + 1) for m in literal.finditer(src)]
    start = src.find("    async def _do(self")
    if start < 0:
        failures.append("hub.py has no `_do` helper: every directive must be built "
                        f"in one place, and there are {len(hits)} `do` literals")
        return
    nxt = re.compile(r"^    (?:async )?def ", re.M).search(src, start + 10)
    first = src.count("\n", 0, start) + 1
    last = src.count("\n", 0, nxt.start()) + 1 if nxt else first + 200
    outside = [n for n in hits if not first <= n < last]
    if len(hits) - len(outside) != 1:
        failures.append(f"`_do` should build exactly one `do` frame literal, found "
                        f"{len(hits) - len(outside)}")
    if outside:
        failures.append(f"`do` frame literal(s) outside `_do`, at hub.py line(s) "
                        f"{outside}: a directive built there gets no `seq`")


# --------------------------------------------------------------------------- #
# 1. P1-P4 for a receiver with the cap, and which directives are stamped
# --------------------------------------------------------------------------- #
async def group_echo_peer(failures):
    async with Hub(debug=True) as hub:
        r = Peer("Recv", "recv", ECHO)
        b = Peer("B", "b", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        for p in (r, b, c):
            await p.connect(hub.url)
        r.auto_echo = r.auto_release = True

        for p in (r, c):
            if "cmdEcho" not in ((p.welcome or {}).get("features") or []):
                failures.append(f"welcome.features of {p.name} lacks \"cmdEcho\": "
                                f"{(p.welcome or {}).get('features')!r}")

        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.2)
        r.model.update(index=0, positionMs=0, isPlaying=True)

        # ----- which directives carry a seq -------------------------------- #
        script = [
            ("load", dict(action="setQueue", tracks=tracks(5), index=0, play=True)),
            ("pause", dict(action="pause")),
            ("play", dict(action="play")),
            ("seek", dict(action="seek", positionMs=30000)),
            ("jump", dict(action="jump", index=2)),
            ("queueChanged", dict(action="enqueue", tracks=tracks(1, "x"))),
            ("setVolume", dict(action="volume", level=40)),
            ("setRepeat", dict(action="repeat", mode="all")),
            ("setShuffle", dict(action="shuffle", on=False)),
            ("favorite", dict(action="favorite", id="t1", favorite=True)),
            ("rating", dict(action="rating", id="t1", rating=4)),
        ]
        seqs = []
        for cmd, act in script:
            n = len(r.dos)
            await c.act(**act)
            if not await until(lambda: len(r.dos) > n):
                failures.append(f"stamping: act {act['action']} produced no directive")
                continue
            d = r.dos[n]
            if d.get("cmd") != cmd:
                failures.append(f"stamping: act {act['action']} sent do:{d.get('cmd')}, "
                                f"expected do:{cmd}")
            elif cmd in STAMPED:
                if type(d.get("seq")) is not int:
                    failures.append(f"do:{cmd} to a cmdEcho receiver carries no int "
                                    f"`seq`: {d.get('seq')!r}")
                else:
                    seqs.append(d["seq"])
            elif "seq" in d:
                failures.append(f"do:{cmd} carries a `seq` ({d['seq']}); only the seven "
                                "that change a report may")
        if seqs and (seqs != sorted(set(seqs)) or seqs[0] < 1):
            failures.append(f"`seq` must rise by the directive, from 1: {seqs}")
        await c.act(action="repeat", mode="none")
        await c.act(action="jump", index=0)
        await asyncio.sleep(0.3)

        # ----- P1: a tick sampled before the skip -------------------------- #
        await r.report(positionMs=5000)
        await asyncio.sleep(0.2)
        r.auto_echo = False
        c.sessions.clear()
        n = len(r.dos)
        await c.act(action="next")
        await until(lambda: len(r.dos) > n)
        jump = r.dos[-1]
        await r.report(index=0, positionMs=5000, isPlaying=True)   # the old number
        await asyncio.sleep(0.15)
        r.apply(jump)
        await r.report()
        await asyncio.sleep(0.3)
        got = [s["index"] for s in c.sessions]
        if got != [1]:
            failures.append(f"P1 (cmdEcho): one `next` should give one session at "
                            f"index 1, got indexes {got}")

        # ----- P2: it says it did not start -------------------------------- #
        r.auto_echo = True
        await c.act(action="pause")
        await asyncio.sleep(0.3)
        r.auto_echo = False
        c.sessions.clear()
        n = len(r.dos)
        await c.act(action="play")
        await until(lambda: len(r.dos) > n)
        await asyncio.sleep(0.3)
        r.applied = r.dos[-1].get("seq", 0)
        await r.report(isPlaying=False)        # carried out, and it is not playing
        await asyncio.sleep(0.4)
        if c.is_playing is not False:
            failures.append("P2 (cmdEcho): the receiver reported \"not playing\" with "
                            f"the play's own number, and the session still says "
                            f"isPlaying={c.is_playing}")
        got = [s["isPlaying"] for s in c.sessions]
        if got != [True, False]:
            failures.append(f"P2 (cmdEcho): expected sessions playing then not, got {got}")

        # ----- P3: a late "playing" sample --------------------------------- #
        r.auto_echo = True
        await c.act(action="play")
        await asyncio.sleep(0.3)
        r.auto_echo = False
        c.sessions.clear()
        n = len(r.dos)
        await c.act(action="pause")
        await until(lambda: len(r.dos) > n)
        pause = r.dos[-1]
        await asyncio.sleep(2.2)               # past INTENT_GRACE
        await r.report(isPlaying=True, positionMs=3000)   # still the old number
        await asyncio.sleep(0.3)
        if c.is_playing is not False:
            failures.append("P3 (cmdEcho): a \"playing\" sample 2.2 s after the pause, "
                            "carrying the number from before it, flipped the session "
                            "back to playing")
        r.apply(pause)
        await r.report(positionMs=3200)
        await asyncio.sleep(0.3)
        got = [s["isPlaying"] for s in c.sessions]
        if got != [False]:
            failures.append(f"P3 (cmdEcho): one pause should give one session, "
                            f"not playing; got {got}")

        # ----- P4: the transfer target's old local state ------------------- #
        r.auto_echo = True
        await c.act(action="setQueue", tracks=tracks(5), index=1, positionMs=42000, play=True)
        await asyncio.sleep(0.4)

        async def b_frame(msg):
            # Stale twice: on learning it is active (before the load arrives), and
            # again on the load, before carrying it out.
            if msg.get("t") == "session" and msg.get("activeDeviceId") == "b" \
                    and not b.last_do("load"):
                await b.report(index=3, positionMs=99000, isPlaying=True)
            if msg.get("t") == "do" and msg.get("cmd") == "load":
                await b.report(index=3, positionMs=99000, isPlaying=True)
        b.on_frame = b_frame
        c.sessions.clear()
        await c.act(action="transfer", target="b")
        await asyncio.sleep(0.5)
        if (c.active, c.index, c.position) != ("b", 1, 42000):
            failures.append("P4 (cmdEcho): the target's report of its own old state "
                            "(index 3, 99 s) overwrote the session before it loaded: "
                            f"active={c.active} index={c.index} positionMs={c.position}, "
                            "expected b / 1 / 42000")
        load = b.last_do("load")
        if load is None:
            failures.append("P4 (cmdEcho): the target got no do:load")
        else:
            b.on_frame = None
            b.apply(load)
            await b.send(t="loaded", ok=True, transferId=load.get("transferId"))
            await b.report()
            await asyncio.sleep(0.3)
            if (c.index, c.position, c.is_playing) != (1, 42000, True):
                failures.append(f"P4 (cmdEcho): after the load settled the session is "
                                f"index={c.index} positionMs={c.position} "
                                f"isPlaying={c.is_playing}, expected 1 / 42000 / True")
        # Two frames, not one: the old device's final report says "paused" (true,
        # and published as before), then the commit. Neither may leave index 1.
        got = [(s["activeDeviceId"], s["index"]) for s in c.sessions]
        if got != [("recv", 1), ("b", 1)]:
            failures.append(f"P4 (cmdEcho): a transfer should give the old device's "
                            f"pause and the commit, both at index 1; got {got}")

        # ----- the seventh stamped directive ------------------------------- #
        n = len(b.dos)
        await c.act(action="clear")
        await until(lambda: len(b.dos) > n)
        d = b.dos[-1]
        if d.get("cmd") != "clear" or type(d.get("seq")) is not int:
            failures.append(f"do:clear to a cmdEcho receiver carries no int `seq`: {d}")

        # ----- the counters, and the log line ------------------------------ #
        h = await hub.health()
        if not isinstance(h.get("staleReports"), int) or h["staleReports"] < 4:
            failures.append(f"/health staleReports should count the reports dropped "
                            f"above (at least 4): {h.get('staleReports')!r}")
        if h.get("echoBackstops") != 0:
            failures.append(f"/health echoBackstops should be 0 here: {h.get('echoBackstops')!r}")
        if "DROPPED(stale" not in hub.log():
            failures.append("HUB_DEBUG logged no line for a report dropped as stale")
        for p in (r, b, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 2. the same for a receiver without the cap: only P1 changes
# --------------------------------------------------------------------------- #
async def group_plain_peer(failures):
    async with Hub() as hub:
        r = Peer("Recv", "recv", PLAIN)
        b = Peer("B", "b", PLAIN)
        c = Peer("Ctl", "ctl", ["controller"])
        for p in (r, b, c):
            await p.connect(hub.url)
        r.auto_release = True
        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.2)
        r.model.update(index=0, positionMs=5000, isPlaying=True)
        await r.report()
        await asyncio.sleep(0.2)

        # ----- P1: the index guard, for every receiver ---------------------- #
        c.sessions.clear()
        n = len(r.dos)
        await c.act(action="next")
        await until(lambda: len(r.dos) > n)
        await r.report(index=0, positionMs=5000, isPlaying=True)   # sampled before
        await asyncio.sleep(0.15)
        r.apply(r.dos[-1])
        await r.report()
        await asyncio.sleep(0.3)
        got = [s["index"] for s in c.sessions]
        if got != [1]:
            failures.append(f"P1 (no cap): one `next` should give one session at "
                            f"index 1, got indexes {got}")

        # ----- P2, unchanged: the clock ignores an early "not playing" ----- #
        await c.act(action="pause")
        await asyncio.sleep(0.2)
        await c.act(action="play")
        await asyncio.sleep(0.3)
        await r.report(isPlaying=False)
        await asyncio.sleep(0.4)
        if c.is_playing is not True:
            failures.append("P2 (no cap) changed: a device without the cap keeps the "
                            f"2 s clock, which ignores this report; isPlaying={c.is_playing}")

        # ----- P3, unchanged: a "playing" sample after the clock ------------ #
        r.model["isPlaying"] = True
        await c.act(action="pause")
        await asyncio.sleep(2.2)
        await r.report(isPlaying=True, positionMs=3000)
        await asyncio.sleep(0.3)
        if c.is_playing is not True:
            failures.append("P3 (no cap) changed: past the clock a device without the "
                            f"cap is believed; isPlaying={c.is_playing}")

        # ----- a jump to the index it is already on arms nothing ------------ #
        # (More than INTENT_GRACE after the `next` above, so that skip's own guard
        # has run out.) The receiver then moves to another index by itself, and
        # that report must not be taken for a tick from before a skip.
        await r.report(isPlaying=True, index=1, positionMs=4000)
        await asyncio.sleep(0.2)
        await c.act(action="jump", index=1)
        await asyncio.sleep(0.2)
        r.model.update(index=0, positionMs=0)
        await r.report()
        await asyncio.sleep(0.3)
        if c.index != 0:
            failures.append("index guard: after a jump to the index the session was "
                            "already on, a report naming another index was ignored "
                            f"(session index {c.index}, receiver says 0)")

        # ----- no seq at all without the cap -------------------------------- #
        stamped = [d for d in r.dos if "seq" in d]
        if stamped:
            failures.append(f"a receiver without the cap was sent `seq`: {stamped[:2]}")

        # ----- P4, unchanged: nothing guards a plain transfer target -------- #
        await c.act(action="setQueue", tracks=tracks(5), index=1, positionMs=42000, play=True)
        await asyncio.sleep(0.3)
        r.model.update(index=1, positionMs=42000, isPlaying=True)

        async def b_frame(msg):
            if msg.get("t") == "do" and msg.get("cmd") == "load":
                await b.report(index=3, positionMs=99000, isPlaying=True)
                await b.send(t="loaded", ok=True, transferId=msg.get("transferId"))
        b.on_frame = b_frame
        await c.act(action="transfer", target="b")
        await asyncio.sleep(0.5)
        if (c.active, c.index, c.position) != ("b", 3, 99000):
            failures.append("P4 (no cap) changed: a device without the cap is believed "
                            f"as before; active={c.active} index={c.index} "
                            f"positionMs={c.position}, expected b / 3 / 99000")
        for p in (r, b, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 3. P5 and the rest of the reconnect path
# --------------------------------------------------------------------------- #
async def group_reconnect(failures, caps, label):
    async with Hub(debug=True) as hub:
        r = Peer("Recv", "recv", caps)
        c = Peer("Ctl", "ctl", ["controller"])
        await r.connect(hub.url)
        await c.connect(hub.url)
        r.auto_echo = True
        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.2)
        r.model.update(index=0, positionMs=1000, isPlaying=True)
        await r.report()
        await asyncio.sleep(0.2)

        # The old socket goes deaf: still open, reads nothing, so it will never
        # answer a close frame or a ping. A phone that moved from Wi-Fi to mobile.
        r.auto_echo = False
        r.ws.transport.pause_reading()

        async def press():
            # A controller pressing play/pause while the device comes back: each
            # press is a directive for `recv`, written to whichever socket the hub
            # holds for it at that instant.
            for i in range(60):
                await c.act(action="pause" if i % 2 == 0 else "play")
                await asyncio.sleep(0.01)
        pressing = asyncio.create_task(press())
        await asyncio.sleep(0.15)
        r2 = Peer("Recv", "recv", caps)
        await r2.connect(hub.url, settle=0)
        await until(lambda: r2.welcome is not None, timeout=3.0)
        await pressing
        await asyncio.sleep(0.2)

        if r2.welcome is None:
            failures.append(f"P5 ({label}): reconnecting behind a deaf socket, no "
                            "`welcome` within 3 s (the hub waits for the dead socket's "
                            "close handshake before greeting the new one)")
        else:
            took = r2.welcome_at - r2.hello_at
            if took > 1.0:
                failures.append(f"P5 ({label}): `welcome` took {took:.1f} s")
        kinds = [f.get("t") for f in r2.frames]
        if kinds[:1] != ["welcome"]:
            failures.append(f"({label}) the first frame on the new socket is "
                            f"{kinds[:1]}, not `welcome`: {kinds[:6]}")
        before = kinds.index("welcome") if "welcome" in kinds else len(kinds)
        early = [f.get("cmd") for f in r2.frames[:before] if f.get("t") == "do"]
        if early:
            failures.append(f"({label}) {len(early)} `do` frame(s) written before "
                            f"`welcome` on the new socket: {early[:4]}")
        if not [f for f in r2.frames if f.get("t") == "do"]:
            failures.append(f"({label}) setup: no directive reached the new socket at "
                            "all, so \"none before welcome\" proves nothing")

        # ----- a frame from the superseded socket --------------------------- #
        # The hub is still closing the deaf socket in the background and still
        # reads from it. Whatever arrives there was sent on a connection that is
        # over: this report would otherwise move the session to index 3.
        await r2.report(index=0, positionMs=2000, isPlaying=c.is_playing,
                        applied=max((d.get("seq", 0) for d in r2.dos), default=0))
        await asyncio.sleep(0.2)
        await r.ws.send(json.dumps({"t": "report", "index": 3, "positionMs": 77000,
                                    "isPlaying": c.is_playing, "applied": 10 ** 9}))
        await asyncio.sleep(0.4)
        if c.index != 0:
            failures.append(f"({label}) a report sent on the superseded socket moved "
                            f"the session to index {c.index}")
        if "superseded socket" not in hub.log():
            failures.append(f"({label}) the hub logged no dropped frame for the "
                            "superseded socket, so the report above may not have "
                            "arrived at all")

        # ----- the echo is cleared at `welcome` ----------------------------- #
        # r2 has left directives unconfirmed. A third socket for the same id starts
        # from nothing: its first report, with no `applied`, is the truth.
        n = len(r2.dos)
        await c.act(action="pause")
        await until(lambda: len(r2.dos) > n)
        r3 = Peer("Recv", "recv", caps)
        await r3.connect(hub.url)
        await r3.send(t="report", index=2, positionMs=500, isPlaying=False)
        await asyncio.sleep(0.3)
        if c.index != 2:
            failures.append(f"({label}) after a new `welcome` a report with no `applied` "
                            f"was not believed (session index {c.index}, expected 2): "
                            "the hub kept what it had sent the previous connection")

        # ----- what the log says about coming and going --------------------- #
        await r3.close()
        await asyncio.sleep(0.3)
        log = hub.log()
        if not re.search(r"\+ Recv \(recv\) connected; .*away \d+\.\d+s, replacing a live "
                         r"socket \(the active device\)", log):
            failures.append(f"({label}) the connect line does not say how long the id "
                            "was away and that it replaced the active device's socket")
        if not re.search(r"- Recv \(recv\) disconnected \(was the active device", log):
            failures.append(f"({label}) the disconnect line does not say the device "
                            "was the active one")
        r.ws.transport.abort()
        for p in (r2, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 4. the two limits on belief, and a takeover
# --------------------------------------------------------------------------- #
async def group_limits(failures):
    async with Hub() as hub:
        a = Peer("A", "a", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        await a.connect(hub.url)
        await c.connect(hub.url)
        a.auto_echo = a.auto_release = True
        await a.act(action="setQueue", tracks=tracks(5), index=1, positionMs=42000, play=True)
        await asyncio.sleep(0.2)
        a.model.update(index=1, positionMs=42000, isPlaying=True)
        await a.report()
        await asyncio.sleep(0.2)

        # ----- a report right behind a transfer's loaded{ok:false} ---------- #
        for caps, label in ((ECHO, "cmdEcho"), (PLAIN, "no cap")):
            b = Peer("B", f"b-{label[:2]}", caps)
            await b.connect(hub.url)

            async def b_do(msg, b=b):
                if msg.get("cmd") != "load":
                    return
                # It could not start. Then, against its contract, it reports the
                # state of the player that failed, with the load's own number.
                await send_together(
                    b, {"t": "loaded", "ok": False, "error": "refused",
                        "transferId": msg.get("transferId")},
                    {"t": "report", "index": 0, "positionMs": 0, "isPlaying": False,
                     "applied": msg.get("seq", 0)})
            b.on_do = b_do
            c.errors.clear()
            await c.act(action="transfer", target=b.device_id)
            await until(lambda: "load_failed" in c.err_codes(), timeout=3.0)
            await asyncio.sleep(0.2)
            if "load_failed" not in c.err_codes():
                failures.append(f"refused load ({label}): no load_failed broadcast")
            if (c.active, c.index, c.position) != ("a", 1, 42000):
                failures.append(
                    f"refused load ({label}): the target's report after "
                    "loaded{ok:false} was mirrored before the rollback: "
                    f"active={c.active} index={c.index} positionMs={c.position}, "
                    "expected a / 1 / 42000")
            # The rollback leaves the session paused on A; put it back as it was.
            await c.act(action="play")
            await asyncio.sleep(0.3)
            await b.close()

        # ----- a failed load that was not a transfer's ---------------------- #
        a.auto_echo = False
        c.errors.clear()
        n = len(a.dos)
        await c.act(action="setQueue", tracks=tracks(4, "n"), index=0, play=True)
        await until(lambda: len(a.dos) > n)
        load = a.dos[-1]
        a.model.update(index=0, positionMs=0, isPlaying=False)   # it pauses first
        a.applied = load.get("seq", 0)
        await a.send(t="loaded", ok=False, error="no such track")   # answers nothing
        await a.report()
        await asyncio.sleep(0.4)
        if c.is_playing is not False:
            failures.append("failed load (not a transfer): the receiver echoed \"not "
                            f"playing\" and the session still says isPlaying={c.is_playing}")
        if c.active != "a" or "load_failed" in c.err_codes():
            failures.append("failed load (not a transfer): a `loaded` that answers no "
                            f"pending transfer changed something: active={c.active} "
                            f"errors={c.err_codes()}")

        # ----- a takeover: the same hole as P4, on an orphaned session ------ #
        a.auto_echo = True
        await c.act(action="setQueue", tracks=tracks(5), index=1, positionMs=42000, play=True)
        await asyncio.sleep(0.3)
        await a.close()                      # the active device vanishes
        await until(lambda: c.active is None)
        t = Peer("T", "t", ECHO)
        await t.connect(hub.url)
        # It presses play and, before the load it is handed has taken effect,
        # reports what its own player was doing.
        await t.act(action="play")
        await t.report(index=3, positionMs=99000, isPlaying=True)
        await asyncio.sleep(0.4)
        if (c.active, c.index, c.position) != ("t", 1, 42000):
            failures.append("takeover: the new device's report of its own old state "
                            f"overwrote the resume point: active={c.active} "
                            f"index={c.index} positionMs={c.position}, expected t / 1 / 42000")
        for p in (t, c):
            await p.close()


async def group_clear(failures):
    """A receiver that empties its queue on `clear` and goes on saying "playing"."""
    for echo, label in ((True, "cmdEcho"), (False, "no cap")):
        async with Hub() as hub:
            c = Peer("Ctl", "ctl", ["controller"])
            await c.connect(hub.url)
            c.auto_release = True            # it holds the slot until the transfer
            fake = FakeReceiver(hub.url, TOKEN, "Fake", "fake", echo=echo,
                                playing_after_clear=True, quiet=True)
            run = asyncio.create_task(fake.run())
            await asyncio.sleep(0.4)
            await c.act(action="setQueue", tracks=tracks(3), index=0, play=True)
            await asyncio.sleep(0.2)
            await c.act(action="transfer", target="fake")
            await until(lambda: c.active == "fake" and fake.playing, timeout=3.0)
            await asyncio.sleep(0.3)
            if not (c.active == "fake" and c.is_playing):
                failures.append(f"clear ({label}): setup failed, active={c.active} "
                                f"isPlaying={c.is_playing}")
            c.sessions.clear()
            await c.act(action="clear")
            await asyncio.sleep(2.8)         # past the clock, with 1 Hz ticks behind
            if not fake.playing:
                failures.append(f"clear ({label}): setup: the fake receiver stopped, "
                                "so nothing claimed \"playing\"")
            flipped = [s for s in c.sessions if s.get("isPlaying")]
            if c.is_playing or flipped:
                failures.append(f"clear ({label}): a report turned the session to "
                                f"playing over an empty queue ({len(flipped)} such "
                                f"session frame(s), queue length {c.queue_len})")
            run.cancel()
            await c.close()


async def group_fake_receiver(failures):
    """P1 once more, through tools/fake_receiver.py: --stale-first reports the old
    state before each directive and --settle-ms applies it late, which is how a real
    client's 1 Hz tick and main-thread hop look from the hub. With --echo the hub
    drops the stale report by its number; without, the index guard ignores it."""
    for echo, label in ((True, "--echo"), (False, "no cap")):
        async with Hub() as hub:
            c = Peer("Ctl", "ctl", ["controller"])
            await c.connect(hub.url)
            c.auto_release = True
            fake = FakeReceiver(hub.url, TOKEN, "Fake", "fake", echo=echo,
                                stale_first=True, settle_ms=200, quiet=True)
            run = asyncio.create_task(fake.run())
            await asyncio.sleep(0.4)
            await c.act(action="setQueue", tracks=tracks(4), index=0, play=True)
            await asyncio.sleep(0.2)
            await c.act(action="transfer", target="fake")
            await until(lambda: c.active == "fake" and fake.playing, timeout=3.0)
            await asyncio.sleep(0.3)
            c.sessions.clear()
            for _ in range(2):
                await c.act(action="next")
                await asyncio.sleep(0.6)
            got = [s["index"] for s in c.sessions]
            if got != [1, 2] or fake.index != 2:
                failures.append(f"fake_receiver --stale-first --settle-ms ({label}): two "
                                f"skips should give sessions at 1 then 2, got {got} "
                                f"(the receiver is at {fake.index})")
            if echo and fake.applied < 3:
                failures.append(f"fake_receiver --echo never moved `applied`: {fake.applied}")
            run.cancel()
            await c.close()


# --------------------------------------------------------------------------- #
# 5. the backstop: a receiver that never confirms
# --------------------------------------------------------------------------- #
async def group_backstop(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        c.auto_release = True                # it holds the slot until the transfer
        fake = FakeReceiver(hub.url, TOKEN, "Fake", "fake", echo=True, quiet=True)
        run = asyncio.create_task(fake.run())
        await asyncio.sleep(0.4)
        await c.act(action="setQueue", tracks=tracks(3), index=0, play=True)
        await asyncio.sleep(0.2)
        await c.act(action="transfer", target="fake")
        await until(lambda: c.active == "fake" and fake.playing, timeout=3.0)
        await asyncio.sleep(0.3)

        # From here it takes directives and carries none out: still "playing", still
        # ticking once a second, `applied` stuck where it was.
        fake.never_settle = True
        stuck_at = fake.applied
        t0 = time.monotonic()
        for i in range(3):                   # three pauses, five seconds apart
            await asyncio.sleep(max(0.0, t0 + 5 * i - time.monotonic()))
            await c.act(action="pause")
        await asyncio.sleep(max(0.0, t0 + 11.4 - time.monotonic()))
        h = await hub.health()
        if h.get("echoBackstops") != 0:
            failures.append(f"backstop fired before 12 s: {h.get('echoBackstops')!r} at 11.4 s")
        if c.is_playing is not False:
            failures.append("backstop: before 12 s the ticks of a receiver that has "
                            "not confirmed the pause must be dropped, but the session "
                            f"says isPlaying={c.is_playing}")
        if not isinstance(h.get("staleReports"), int) or h["staleReports"] < 8:
            failures.append(f"backstop: ~11 ticks were dropped by then, staleReports "
                            f"says {h.get('staleReports')!r}")

        # 12 s after the FIRST pause, not the last: the later ones do not restart it.
        await asyncio.sleep(max(0.0, t0 + 13.7 - time.monotonic()))
        h = await hub.health()
        if h.get("echoBackstops") != 1:
            failures.append("backstop: 13.7 s after the first of three unconfirmed "
                            f"pauses, echoBackstops is {h.get('echoBackstops')!r}, "
                            "expected 1 (later directives must not restart the 12 s)")
        if c.is_playing is not True:
            failures.append("backstop: once it has fired the device goes by the old "
                            "rule, which believes a \"playing\" tick past the 2 s "
                            f"clock; the session says isPlaying={c.is_playing}")
        first_pause = stuck_at + 1
        want = f"has not confirmed do seq {first_pause} "
        if "echo backstop: Fake (fake)" not in hub.log() or want not in hub.log():
            failures.append("backstop: no log line naming the device and the oldest "
                            f"unconfirmed directive (seq {first_pause})")

        # It catches up: the echo is exact again from that report on.
        fake.never_settle = False
        await c.act(action="pause")
        await until(lambda: fake.applied > first_pause and c.is_playing is False,
                    timeout=2.0)
        if c.is_playing is not False:
            failures.append("backstop: the receiver confirmed a later pause and the "
                            f"session still says isPlaying={c.is_playing}")
        if fake.applied <= first_pause:
            failures.append(f"backstop: setup: the fake never moved `applied` ({fake.applied})")
        await asyncio.sleep(0.2)
        if "caught up" not in hub.log():
            failures.append("backstop: no log line when the device caught up")
        run.cancel()
        await c.close()


async def main():
    failures = []
    case_single_helper(failures)
    groups = [
        ("echo peer", group_echo_peer(failures)),
        ("plain peer", group_plain_peer(failures)),
        ("reconnect, cmdEcho", group_reconnect(failures, ECHO, "cmdEcho")),
        ("reconnect, no cap", group_reconnect(failures, PLAIN, "no cap")),
        ("limits", group_limits(failures)),
        ("clear", group_clear(failures)),
        ("fake receiver", group_fake_receiver(failures)),
        ("backstop", group_backstop(failures)),
    ]
    for name, coro in groups:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one group must not hide the others
            failures.append(f"[{name}] raised {type(e).__name__}: {e}")

    if failures:
        print("FAIL (cmd echo):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - cmd echo: one-do-helper/seq-on-the-seven/P1-P4-exact/no-cap-unchanged-"
          "but-P1/P5-welcome-at-once/no-do-before-welcome/superseded-socket-dropped/"
          "reset-at-welcome/refused-load-not-believed/empty-queue-never-playing/"
          "takeover/backstop-12s-from-the-oldest")


if __name__ == "__main__":
    asyncio.run(main())
