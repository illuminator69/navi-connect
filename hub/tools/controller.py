#!/usr/bin/env python3
"""
Interactive navi-connect controller — drives the hub from a terminal.

Connects as a controller-only device, prints session/device updates, and reads
commands from stdin. Use it to exercise transfer-with-resume against two
fake_receiver.py instances.

Commands:
    devices                      list known devices (with index)
    queue                        load a 5-track demo queue and start playing
    play | pause | next | prev
    seek <ms>
    volume <0-100>               set the active device's volume
    volume <0-100> --target <deviceIndex>
                                 set that device's volume though it is not playing
                                 (it needs the cap "idleVolume")
    sleep <minutes>              pause the session in that many minutes; 0 = off
    transfer <deviceIndex>       hand playback to that device, resume in place
    quit

Every act carries a `cid`, so the hub answers each with an `ack` (PROTOCOL §5.4),
printed as `-- ack c3 ok rev118` or `!! ack c4 refused: target_offline ...`. Against
a hub without "actAck" in `welcome.features` no acks come, and refusals arrive as
`error` frames as before.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import websockets

DEMO_TRACKS = [
    {"id": f"demo-{i}", "title": f"Track {i}", "artist": "Test Artist",
     "album": "Demo", "durationMs": 180000}
    for i in range(1, 6)
]


class Ctl:
    def __init__(self) -> None:
        self.devices: list[dict] = []
        self._acts = 0

    async def act(self, ws, action: str, **fields) -> None:
        """Send one act with a fresh `cid`, the correlation id its `ack` echoes."""
        self._acts += 1
        await ws.send(json.dumps({"t": "act", "action": action,
                                  "cid": f"c{self._acts}", **fields}))

    async def run(self, url: str, token: str) -> None:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "t": "hello", "token": token,
                "device": {"id": "cli-controller", "name": "CLI", "platform": "cli",
                           "caps": ["controller"]},
            }))
            recv = asyncio.create_task(self._recv(ws))
            await self._stdin_loop(ws)
            recv.cancel()

    async def _recv(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("t")
            if t in ("welcome", "devices"):
                self.devices = msg["devices"]
                print("\n-- devices --")
                for i, d in enumerate(self.devices):
                    star = "*" if d["isActive"] else " "
                    on = "online" if d["online"] else "offline"
                    # `kind` is what the device says it is; an older hub sends none.
                    print(f"  [{i}]{star} {d['name']:16} {d.get('kind') or '-':8} {on:7} "
                          f"vol {d.get('volume', '?'):>3} {d['caps']}")
            if t == "welcome":
                print(f"-- hub features: {msg.get('features', [])}")
                self._print_session(msg["session"])
            elif t == "ack":
                # Written after every broadcast the act caused, so the session
                # printed just above already holds the hub's answer.
                if msg.get("ok"):
                    note = f" ({msg['code']})" if msg.get("code") else ""
                    print(f"\n-- ack {msg.get('cid')} ok rev{msg.get('rev')}{note}")
                else:
                    print(f"\n!! ack {msg.get('cid')} refused: {msg.get('code')} "
                          f"{msg.get('message') or ''}")
            elif t == "session":
                self._print_session(msg)
            elif t == "progress":
                sys.stdout.write(f"\r   > idx {msg['index']} {msg['positionMs']/1000:6.1f}s "
                                 f"playing={msg['isPlaying']}   ")
                sys.stdout.flush()
            elif t == "error":
                print(f"\n!! error: {msg.get('code')} {msg.get('message')}")

    def _print_session(self, s: dict) -> None:
        print(f"-- session rev{s['rev']} active={s['activeDeviceId']} "
              f"idx={s['index']} pos={s['positionMs']}ms playing={s['isPlaying']} "
              f"queue={len(s['queue'])} repeat={s['repeat']} shuffle={s['shuffle']}")
        timer = s.get("sleepTimer")
        if timer:
            # What is left when the frame was built; the hub sends nothing while it
            # counts down, so this line appears on set, on replace and with whatever
            # else changes the session.
            left = timer["remainingMs"] // 1000
            by = next((d["name"] for d in self.devices if d["id"] == timer.get("setBy")),
                      timer.get("setBy"))
            print(f"-- sleepTimer {left // 60}:{left % 60:02d} left of "
                  f"{timer['minutes']} min, set by {by}")

    async def _stdin_loop(self, ws) -> None:
        loop = asyncio.get_event_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            if cmd == "quit":
                return
            elif cmd == "devices":
                await ws.send(json.dumps({"t": "ping"}))  # noop; list prints on any device update
            elif cmd == "queue":
                await self.act(ws, "setQueue", tracks=DEMO_TRACKS, index=0, play=True)
            elif cmd in ("play", "pause", "next", "prev"):
                await self.act(ws, {"prev": "previous"}.get(cmd, cmd))
            elif cmd == "seek" and args:
                await self.act(ws, "seek", positionMs=int(args[0]))
            elif cmd == "volume" and args:
                fields = {"level": int(args[0])}
                if "--target" in args:
                    idx = self._device_index(args[args.index("--target") + 1:])
                    if idx is None:
                        continue
                    fields["target"] = self.devices[idx]["id"]
                await self.act(ws, "volume", **fields)
            elif cmd == "sleep" and args:
                await self.act(ws, "sleepTimer", minutes=int(args[0]))
            elif cmd == "transfer" and args:
                idx = self._device_index(args)
                if idx is not None:
                    await self.act(ws, "transfer", target=self.devices[idx]["id"], play=True)
            else:
                print("commands: devices | queue | play | pause | next | prev | seek <ms> | "
                      "volume <0-100> [--target <idx>] | sleep <minutes> | "
                      "transfer <idx> | quit")

    def _device_index(self, args: list) -> int | None:
        """The device index a command names, or None (and a hint) if it names none."""
        if args and args[0].isdigit() and int(args[0]) < len(self.devices):
            return int(args[0])
        print("bad device index; run `devices`")
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "ws://localhost:4790"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", "change-me-to-a-long-random-secret"))
    args = ap.parse_args()
    asyncio.run(Ctl().run(args.url, args.token))


if __name__ == "__main__":
    main()
