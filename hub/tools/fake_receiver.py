#!/usr/bin/env python3
"""
A simulated navi-connect receiver — no audio, just protocol.

It connects to the hub, advertises itself as a receiver, obeys `do` commands,
and ticks a fake playback position (1 Hz) that it reports back. Run two of
these with different --name to demonstrate transfer-with-resume end to end.

    python fake_receiver.py --name Living-Room
    python fake_receiver.py --name Bedroom
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import websockets


async def run(url: str, token: str, name: str, device_id: str) -> None:
    pos_ms = 0
    index = 0
    playing = False
    have_queue = False

    async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "t": "hello", "token": token,
            "device": {"id": device_id, "name": name, "platform": "fake",
                       "caps": ["receiver", "controller"]},
        }))

        async def report() -> None:
            # Immediate state report — a real receiver confirms every state
            # change right away rather than waiting for the next 1 Hz tick.
            await ws.send(json.dumps({"t": "report", "positionMs": pos_ms,
                                      "index": index, "isPlaying": playing}))

        async def ticker() -> None:
            nonlocal pos_ms
            while True:
                await asyncio.sleep(1)
                if playing and have_queue:
                    pos_ms += 1000
                    await report()

        tick_task = asyncio.create_task(ticker())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    print(f"[{name}] connected as {msg['deviceId'][:8]}")
                elif t == "do":
                    cmd = msg.get("cmd")
                    if cmd == "load":
                        have_queue = True
                        index = msg.get("index", 0)
                        pos_ms = msg.get("positionMs", 0)
                        playing = msg.get("play", True)
                        n = len(msg.get("tracks", []))
                        print(f"[{name}] LOAD {n} tracks @ idx {index}, {pos_ms}ms, play={playing}")
                        await report()
                    elif cmd == "play":
                        playing = True; print(f"[{name}] play")
                        await report()
                    elif cmd == "pause":
                        playing = False; print(f"[{name}] pause")
                        await report()
                    elif cmd == "jump":
                        index = msg.get("index", 0); pos_ms = 0
                        print(f"[{name}] jump -> idx {index}")
                        await report()
                    elif cmd == "seek":
                        pos_ms = msg.get("positionMs", 0); print(f"[{name}] seek -> {pos_ms}ms")
                        await report()
                    elif cmd == "setVolume":
                        print(f"[{name}] volume {msg.get('level')}")
                    elif cmd == "queueChanged":
                        print(f"[{name}] queue changed ({len(msg.get('tracks', []))})")
                    elif cmd == "release":
                        playing = False
                        # Final position report, THEN released (order matters for resume).
                        await ws.send(json.dumps({"t": "report", "positionMs": pos_ms,
                                                  "index": index, "isPlaying": False}))
                        await ws.send(json.dumps({"t": "released"}))
                        print(f"[{name}] RELEASED @ idx {index}, {pos_ms}ms")
        finally:
            tick_task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "ws://localhost:4790"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", "change-me-to-a-long-random-secret"))
    ap.add_argument("--name", default="Fake-Receiver")
    ap.add_argument("--id", default=None, help="stable device id (defaults to one derived from name)")
    args = ap.parse_args()
    device_id = args.id or ("fake-" + args.name.lower().replace(" ", "-"))
    asyncio.run(run(args.url, args.token, args.name, device_id))


if __name__ == "__main__":
    main()
