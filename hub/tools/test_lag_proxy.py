#!/usr/bin/env python3
"""
Tests for tools/lag_proxy.py, the relay that puts a slow link in front of a hub.

It is a walk tool, but a relay that reorders frames or swallows a close code would
turn every measurement taken through it into a measurement of the relay. So:

  1. a frame is held for the delay in EACH direction (hello -> welcome takes at
     least twice the delay);
  2. frames keep their order, both ways, when they arrive faster than the delay;
  3. a close is passed on with its code: `4001` for a bad token, `4003` when
     another socket takes the device id.

Spins up a real hub subprocess and the proxy, each on a free port. Exits non-zero
on failure.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets

import lag_proxy
from test_cmd_echo import Hub, Peer, tracks, until
from test_edits import TOKEN, _free_port

DELAY_MS = 150


async def main():
    failures = []
    async with Hub() as hub:
        port = _free_port()
        ready = asyncio.Event()
        proxy = asyncio.create_task(
            lag_proxy.serve("127.0.0.1", port, hub.url, DELAY_MS, ready))
        await asyncio.wait_for(ready.wait(), 5)
        via = f"ws://127.0.0.1:{port}/connect"
        try:
            # ----- 1. the delay, each way ---------------------------------- #
            r = Peer("Recv", "recv")
            await r.connect(via, settle=0)
            await until(lambda: r.welcome is not None, timeout=3.0)
            if r.welcome is None:
                failures.append("no `welcome` through the proxy within 3 s")
            else:
                took = (r.welcome_at - r.hello_at) * 1000
                if not 2 * DELAY_MS - 20 <= took <= 2 * DELAY_MS + 400:
                    failures.append(f"hello -> welcome took {took:.0f} ms through a "
                                    f"{DELAY_MS} ms proxy, expected about {2 * DELAY_MS}")

            # ----- 2. order, both ways -------------------------------------- #
            c = Peer("Ctl", "ctl", ["controller"])      # straight to the hub
            await c.connect(hub.url)
            await r.act(action="setQueue", tracks=tracks(3), index=0, play=True)
            await until(lambda: c.active == "recv", timeout=2.0)
            sent_at = time.monotonic()
            for i in range(1, 11):                     # down: hub -> proxy -> r
                await c.act(action="seek", positionMs=i * 1000)
            await until(lambda: len([d for d in r.dos if d.get("cmd") == "seek"]) >= 10,
                        timeout=3.0)
            down = [d.get("positionMs") for d in r.dos if d.get("cmd") == "seek"]
            if down != [i * 1000 for i in range(1, 11)]:
                failures.append(f"directives reordered or lost on the way down: {down}")
            elif (time.monotonic() - sent_at) * 1000 < DELAY_MS - 20:
                failures.append("ten directives crossed the proxy faster than its delay")
            c.sessions.clear()
            for i in range(1, 11):                     # up: r -> proxy -> hub
                await r.act(action="seek", positionMs=100000 + i)
            await until(lambda: len(c.sessions) >= 10, timeout=3.0)
            up = [s.get("positionMs") for s in c.sessions]
            if up != [100000 + i for i in range(1, 11)]:
                failures.append(f"acts reordered or lost on the way up: {up}")

            # ----- 3. close codes ------------------------------------------- #
            r2 = Peer("Recv", "recv")                  # same id, straight to the hub
            await r2.connect(hub.url)
            try:
                await asyncio.wait_for(r.ws.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
            if r.ws.close_code != 4003:
                failures.append(f"superseded behind the proxy: close code "
                                f"{r.ws.close_code}, expected 4003")

            bad = await websockets.connect(via)
            await bad.send(json.dumps({"t": "hello", "token": "wrong",
                                       "device": {"id": "x", "name": "X", "caps": []}}))
            try:
                first = json.loads(await asyncio.wait_for(bad.recv(), 3))
            except Exception as e:  # noqa: BLE001
                first = {"error": repr(e)}
            if first.get("code") != "auth":
                failures.append(f"bad token through the proxy: expected the hub's "
                                f"auth error frame first, got {first}")
            try:
                await asyncio.wait_for(bad.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
            if bad.close_code != 4001:
                failures.append(f"bad token through the proxy: close code "
                                f"{bad.close_code}, expected 4001")
            for p in (r2, c):
                await p.close()
        finally:
            proxy.cancel()

    if failures:
        print("FAIL (lag proxy):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - lag proxy: delay-each-way/order-kept-both-ways/close-codes-passed-on")


if __name__ == "__main__":
    asyncio.run(main())
