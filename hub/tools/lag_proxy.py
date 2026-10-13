#!/usr/bin/env python3
"""
A WebSocket relay that adds latency — the slow link a localhost hub never has.

    python lag_proxy.py 4791 ws://127.0.0.1:4790 --delay-ms 300

listens on 127.0.0.1:4791 and relays every connection to the hub, holding each
frame for 300 ms in each direction (so a command and its answer are 600 ms
apart). Point a client at the proxy instead of the hub. On a local hub the
answer to an act returns in about a millisecond, which is why a control that
waits for it looks instant there and laggy on a phone.

What it keeps: frame order in each direction, text/binary type, the request path
(`/connect`), and the close code and reason — a close is delayed like a frame and
then passed on, so `4003 superseded` still means that to the client behind it.

What it does not do: the hub's HTTP routes (`/lb/*`, `/sonic/*`, `/preview/*`).
A client pointed here gets its WebSocket and nothing else.

<listen> is a port or host:port (the host defaults to 127.0.0.1).
"""
from __future__ import annotations

import argparse
import asyncio
import time

import websockets

_CLOSE = object()  # queue marker: the source side closed


async def _pump(src, dst, delay: float) -> None:
    """Relay src -> dst, each frame `delay` seconds after it arrived, in order."""
    queue: asyncio.Queue = asyncio.Queue()

    async def reader() -> None:
        try:
            async for frame in src:
                queue.put_nowait((time.monotonic() + delay, frame))
        except websockets.ConnectionClosed:
            pass
        # 1005/1006 are "no code received" and may not be sent on the wire.
        code = src.close_code if src.close_code not in (None, 1005, 1006) else 1000
        queue.put_nowait((time.monotonic() + delay, (_CLOSE, code, src.close_reason or "")))

    read_task = asyncio.create_task(reader())
    try:
        while True:
            due, frame = await queue.get()
            wait = due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if isinstance(frame, tuple) and frame and frame[0] is _CLOSE:
                await dst.close(frame[1], frame[2])
                return
            await dst.send(frame)
    except websockets.ConnectionClosed:
        pass
    finally:
        read_task.cancel()


async def serve(listen_host: str, listen_port: int, upstream: str, delay_ms: int,
                ready: asyncio.Event | None = None) -> None:
    delay = delay_ms / 1000
    base = upstream.rstrip("/")

    async def handler(client) -> None:
        path = (getattr(client, "path", None)
                or getattr(getattr(client, "request", None), "path", None) or "/")
        try:
            # No keepalive pings of its own in either direction: the hub's and the
            # client's pings are control frames that end at this relay, so each
            # side would otherwise measure the proxy rather than be left alone.
            server = await websockets.connect(base + path, max_size=4 * 1024 * 1024,
                                              ping_interval=None)
        except Exception as e:  # noqa: BLE001
            print(f"lag-proxy: upstream {base + path} refused: {e}", flush=True)
            await client.close(1011, "upstream unavailable")
            return
        up = asyncio.create_task(_pump(client, server, delay))
        down = asyncio.create_task(_pump(server, client, delay))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            # One direction ended by passing a close on; let the other drain what
            # is still inside its delay before the sockets go.
            await asyncio.wait({up, down}, timeout=delay + 1.0)
        finally:
            for task in (up, down):
                task.cancel()
            await server.close()

    async with websockets.serve(handler, listen_host, listen_port,
                                max_size=4 * 1024 * 1024, ping_interval=None):
        print(f"lag-proxy: ws://{listen_host}:{listen_port} -> {base}, "
              f"{delay_ms} ms each way", flush=True)
        if ready is not None:
            ready.set()
        await asyncio.Future()  # until cancelled


def main() -> None:
    ap = argparse.ArgumentParser(description="WebSocket relay with a fixed delay each way")
    ap.add_argument("listen", help="port, or host:port (host defaults to 127.0.0.1)")
    ap.add_argument("upstream", help="the hub, e.g. ws://127.0.0.1:4790")
    ap.add_argument("--delay-ms", type=int, default=300,
                    help="delay added to every frame in each direction (default 300)")
    args = ap.parse_args()
    host, _, port = args.listen.rpartition(":")
    try:
        asyncio.run(serve(host or "127.0.0.1", int(port), args.upstream, args.delay_ms))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
