#!/usr/bin/env python3
"""
A simulated navi-connect receiver — no audio, just protocol.

It connects to the hub, advertises itself as a receiver, obeys `do` commands,
and ticks a fake playback position (1 Hz) that it reports back. Run two of
these with different --name to demonstrate transfer-with-resume end to end.

    python fake_receiver.py --name Living-Room
    python fake_receiver.py --name Bedroom

With --echo it also takes the cap "cmdEcho" and keeps the receiver's side of the
command echo (PROTOCOL §5.3): `applied` moves to a directive's `seq` only once the
directive has taken effect here, a report follows at once, and every report sent
before that carries the previous number. The other switches each break that
contract in one way a real client has been seen (or read) to, so the hub's answer
can be watched without a real client:

    --echo --stale-first          before applying a directive, report the state it
                                  is about to leave (a 1 Hz tick sampled too early)
    --echo --settle-ms 3000       take that long to apply a directive; ticks in
                                  between carry the old number, and later
                                  directives wait behind it
    --echo --never-settle         take a directive and never carry it out: the
                                  state and `applied` stay where they were
    --echo --playing-after-clear  empty the queue on `clear` but keep "playing"

It also says what it is and how loud it is (PROTOCOL §3.1, §5.3): --kind K goes
into `hello.device.kind`, and its volume (--volume N to start with, 100 otherwise)
is reported in `deviceState` after `welcome` and whenever it changes. A
`do:setVolume` is applied, and so reported, while this receiver holds the session.
With --idle-volume it also takes the cap "idleVolume" and applies one at any time,
which is what lets a controller turn it down before sending it music (`act:volume`
with a `target`, PROTOCOL §5.1).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Optional

import websockets


class FakeReceiver:
    """The receiver as an object, so a test can run one in its own loop."""

    def __init__(self, url: str, token: str, name: str, device_id: str, *,
                 echo: bool = False, stale_first: bool = False, settle_ms: int = 0,
                 never_settle: bool = False, playing_after_clear: bool = False,
                 kind: Optional[str] = None, volume: int = 100,
                 idle_volume: bool = False, quiet: bool = False) -> None:
        self.url, self.token, self.name, self.device_id = url, token, name, device_id
        self.echo = echo
        self.stale_first = stale_first
        self.settle_ms = settle_ms
        self.never_settle = never_settle
        self.playing_after_clear = playing_after_clear
        self.kind = kind
        self.volume = volume
        self.idle_volume = idle_volume
        self.quiet = quiet
        self.pos_ms = 0
        self.index = 0
        self.playing = False
        self.have_queue = False
        # The echo: the `seq` of the last directive carried out on THIS connection.
        self.applied = 0
        # Whether the hub's session names this device, from `welcome` and `session`.
        self.active = False
        self.ws = None

    def say(self, *a) -> None:
        if not self.quiet:
            print(f"[{self.name}]", *a)

    async def report(self, **extra) -> None:
        # Read from the "player" at the moment of sending, never from a cache.
        frame = {"t": "report", "positionMs": self.pos_ms, "index": self.index,
                 "isPlaying": self.playing, **extra}
        if self.echo:
            frame["applied"] = self.applied
        await self.ws.send(json.dumps(frame))

    async def set_volume(self, level: int) -> None:
        """Change the level here, as a hardware key would, and say so. A report,
        not a request: the hub stores it and answers nothing."""
        level = max(0, min(100, int(level)))
        if level == self.volume:
            return
        self.volume = level
        await self.report_volume()

    async def report_volume(self) -> None:
        await self.ws.send(json.dumps({"t": "deviceState", "volume": self.volume}))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            # --playing-after-clear keeps ticking over an empty queue: that is the
            # defect (a player told to clear that never stops saying "playing").
            if self.playing and (self.have_queue or self.playing_after_clear):
                if self.have_queue:
                    self.pos_ms += 1000
                await self.report()

    def _apply(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "load":
            self.have_queue = True
            self.index = msg.get("index", 0)
            self.pos_ms = msg.get("positionMs", 0)
            self.playing = msg.get("play", True)
            n = len(msg.get("tracks", []))
            self.say(f"LOAD {n} tracks @ idx {self.index}, {self.pos_ms}ms, play={self.playing}")
        elif cmd == "play":
            self.playing = True; self.say("play")
        elif cmd == "pause":
            self.playing = False; self.say("pause")
        elif cmd == "jump":
            self.index = msg.get("index", 0); self.pos_ms = 0
            self.say(f"jump -> idx {self.index}")
        elif cmd == "seek":
            self.pos_ms = msg.get("positionMs", 0); self.say(f"seek -> {self.pos_ms}ms")
        elif cmd == "queueChanged":
            self.index = msg.get("index", self.index)
            self.say(f"queue changed ({len(msg.get('tracks', []))})")
        elif cmd == "clear":
            self.have_queue = False
            self.index = 0
            self.pos_ms = 0
            if not self.playing_after_clear:
                self.playing = False
            self.say("clear")

    async def _on_do(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "setVolume":
            # Never the start of playback, and no `seq` to echo: it only moves the
            # level, whether or not anything is playing here.
            if not (self.active or self.idle_volume):
                self.say(f"volume {msg.get('level')} ignored: not the playing device")
                return
            self.say(f"volume {msg.get('level')}")
            await self.set_volume(msg.get("level", self.volume))
            return
        if cmd == "release":
            self.playing = False
            # Final position report, THEN released (order matters for resume). Both
            # read from the player now; no echoing report follows a release.
            await self.report()
            await self.ws.send(json.dumps({"t": "released",
                                           "transferId": msg.get("transferId"),
                                           "index": self.index,
                                           "positionMs": self.pos_ms}))
            self.say(f"RELEASED @ idx {self.index}, {self.pos_ms}ms")
            return
        if cmd not in ("load", "play", "pause", "jump", "seek", "queueChanged", "clear"):
            return
        seq = msg.get("seq")
        if self.stale_first:
            await self.report()          # the state this directive is about to end
        if self.never_settle:
            self.say(f"{cmd} seq={seq} taken, never carried out")
            return
        if self.settle_ms:
            await asyncio.sleep(self.settle_ms / 1000)
        self._apply(msg)
        # The effect is in the player's state: only now may `applied` move, and it
        # only moves forward. Then a report at once, so the hub is never left
        # holding a dropped stale one with nothing behind it.
        if isinstance(seq, int) and seq > self.applied:
            self.applied = seq
        await self.report()

    async def run(self) -> None:
        async with websockets.connect(self.url, max_size=4 * 1024 * 1024) as ws:
            self.ws = ws
            caps = (["receiver", "controller"] + (["cmdEcho"] if self.echo else [])
                    + (["idleVolume"] if self.idle_volume else []))
            device = {"id": self.device_id, "name": self.name, "platform": "fake",
                      "caps": caps}
            if self.kind is not None:
                device["kind"] = self.kind
            await ws.send(json.dumps({"t": "hello", "token": self.token, "device": device}))
            tick_task = asyncio.create_task(self._ticker())
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("t")
                    if t == "welcome":
                        # The one reset point of the echo, on both sides.
                        self.applied = 0
                        self.say(f"connected as {msg['deviceId'][:8]}"
                                 f" (hub features: {msg.get('features', [])})")
                        self.active = (msg.get("session") or {}).get(
                            "activeDeviceId") == msg["deviceId"]
                        # The level on connect, so the row is true from the start.
                        await self.report_volume()
                    elif t == "session":
                        self.active = msg.get("activeDeviceId") == self.device_id
                    elif t == "do":
                        await self._on_do(msg)
            finally:
                tick_task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "ws://localhost:4790"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", "change-me-to-a-long-random-secret"))
    ap.add_argument("--name", default="Fake-Receiver")
    ap.add_argument("--id", default=None, help="stable device id (defaults to one derived from name)")
    ap.add_argument("--echo", action="store_true",
                    help='advertise "cmdEcho" and echo `seq` in `report.applied`')
    ap.add_argument("--stale-first", action="store_true",
                    help="report the old state once before applying each directive")
    ap.add_argument("--settle-ms", type=int, default=0, metavar="MS",
                    help="take MS milliseconds to apply each directive")
    ap.add_argument("--never-settle", action="store_true",
                    help="take directives and never carry them out")
    ap.add_argument("--playing-after-clear", action="store_true",
                    help='keep reporting "playing" after a clear')
    ap.add_argument("--kind", default=None, metavar="K",
                    help="what this device is, for the picker's icon: computer, "
                         "phone, tablet, speaker, tv, group")
    ap.add_argument("--volume", type=int, default=100, metavar="N",
                    help="the level it starts at and reports on connect (0-100)")
    ap.add_argument("--idle-volume", action="store_true",
                    help='advertise "idleVolume" and apply do:setVolume also while '
                         "not the playing device")
    args = ap.parse_args()
    device_id = args.id or ("fake-" + args.name.lower().replace(" ", "-"))
    asyncio.run(FakeReceiver(
        args.url, args.token, args.name, device_id, echo=args.echo,
        stale_first=args.stale_first, settle_ms=args.settle_ms,
        never_settle=args.never_settle,
        playing_after_clear=args.playing_after_clear,
        kind=args.kind, volume=max(0, min(100, args.volume)),
        idle_volume=args.idle_volume).run())


if __name__ == "__main__":
    main()
