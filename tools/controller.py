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
    transfer <deviceIndex>       hand playback to that device, resume in place
    quit
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
                    print(f"  [{i}]{star} {d['name']:16} {on:7} {d['caps']}")
            if t == "welcome":
                self._print_session(msg["session"])
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
                await ws.send(json.dumps({"t": "act", "action": "setQueue",
                                          "tracks": DEMO_TRACKS, "index": 0, "play": True}))
            elif cmd in ("play", "pause", "next", "prev"):
                action = {"prev": "previous"}.get(cmd, cmd)
                await ws.send(json.dumps({"t": "act", "action": action}))
            elif cmd == "seek" and args:
                await ws.send(json.dumps({"t": "act", "action": "seek", "positionMs": int(args[0])}))
            elif cmd == "transfer" and args:
                idx = int(args[0])
                if 0 <= idx < len(self.devices):
                    await ws.send(json.dumps({"t": "act", "action": "transfer",
                                              "target": self.devices[idx]["id"], "play": True}))
                else:
                    print("bad device index; run `devices`")
            else:
                print("commands: devices | queue | play | pause | next | prev | seek <ms> | "
                      "transfer <idx> | quit")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "ws://localhost:4790"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", "change-me-to-a-long-random-secret"))
    args = ap.parse_args()
    asyncio.run(Ctl().run(args.url, args.token))


if __name__ == "__main__":
    main()
