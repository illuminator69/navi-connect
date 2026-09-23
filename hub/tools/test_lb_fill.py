#!/usr/bin/env python3
"""
Regression test for the inbound fill push: `POST /lb/fill`.

A fill's progress, failure or cancel reaches the other clients through this
route, as a `t: "fill"` frame. Three things are checked:

1. **It is not a `library` event.** Both clients answer a `library` frame by
   refetching their discographies and the hub flushes `LB_LIBRARY_ROUTES` on it;
   a progress tick must do neither. The one cache a fill push touches is the
   ranked source list, and only when the fill ended.
2. **The frame is rebuilt field by field and bounded**, with the typed fields
   (ints, floats, bools) coerced and unlisted keys dropped.
3. **A push with no usable kind or key is refused**, and a body read that times
   out is a 400 rather than a `{}` forwarded upstream.

No sockets: the protocol, headers and hub are stubbed. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402


class Reader:
    def __init__(self, data: bytes, hang: bool = False) -> None:
        self._data, self._hang = data, hang

    async def readexactly(self, n: int) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        out, self._data = self._data[:n], self._data[n:]
        return out


class Protocol:
    def __init__(self, body: bytes, hang: bool = False) -> None:
        self.reader = Reader(body, hang)


class FakeHub:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def _broadcast(self, obj: dict) -> None:
        self.frames.append(obj)


async def push(payload: dict, token: str | None = None) -> tuple:
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token or hub.TOKEN}", "Content-Length": str(len(body))}
    return await hub._handle_lb_fill(Protocol(body), "/lb/fill", headers, "POST")


def key_for(route: tuple[str, str], path: str) -> str:
    return json.dumps([route, path, [], None], sort_keys=True, default=str)


async def run() -> list[str]:
    failures: list[str] = []
    fake = FakeHub()
    hub.HUB_INSTANCE = fake

    disco = ("GET", "/lb/artist/discography")
    sources = ("GET", "/lb/album/sources")
    hub.LB._cache.clear()
    hub.LB._cache_put(key_for(disco, "/api/artist/discography?nd_id=a1"), 200, b"{}", "application/json")
    hub.LB._cache_put(key_for(sources, "/api/album/sources?rgid=r"), 200, b"{}", "application/json")
    gen_before = hub.LB._cache_gen

    # 1. progress: nothing invalidated, frame typed and bounded
    status, _h, _b = await push({
        "kind": "album", "key": "rg-1", "releaseMbid": "rel-1", "rgid": "rg-1",
        "state": "downloading", "done": 3, "failed": 0.0, "total": 12, "percent": "31",
        "bytesDone": 98304000, "speedBps": 1450000, "retryable": 0, "cancellable": 1,
        "updatedAt": 1790000000.2, "serverTime": 1790000003, "ndAlbumIds": ["a", 7, "b"],
        "files": [{"title": "never relayed"}], "evil": {"nested": 1},
    })
    if status != 200:
        failures.append(f"progress push should answer 200, got {status}")
    if hub.LB._cache_get(key_for(disco, "/api/artist/discography?nd_id=a1")) is None:
        failures.append("a progress push flushed the discography cache — that is a library notify's job")
    if hub.LB._cache_get(key_for(sources, "/api/album/sources?rgid=r")) is None:
        failures.append("a progress push flushed the source list — only a terminal fill does")
    if hub.LB._cache_gen != gen_before:
        failures.append("a progress push bumped the cache generation")
    if len(fake.frames) != 1:
        failures.append(f"expected one broadcast, got {len(fake.frames)}")
        return failures
    frame = fake.frames[0]
    for k, v in {"t": "fill", "kind": "album", "key": "rg-1", "state": "downloading",
                 "done": 3, "failed": 0, "total": 12, "bytesDone": 98304000,
                 "retryable": False, "cancellable": True, "updatedAt": 1790000000.2,
                 "ndAlbumIds": ["a", "b"]}.items():
        if frame.get(k) != v:
            failures.append(f"frame[{k!r}] = {frame.get(k)!r}, expected {v!r}")
    if "percent" in frame:
        failures.append("a string percent was relayed as if it were a number")
    for k in ("files", "evil"):
        if k in frame:
            failures.append(f"unlisted key {k!r} relayed to clients")

    # 2. terminal: the ranked source list is dropped, nothing else
    fake.frames.clear()
    await push({"kind": "album", "key": "rg-1", "state": "cancelled"})
    if hub.LB._cache_get(key_for(sources, "/api/album/sources?rgid=r")) is not None:
        failures.append("a cancelled fill left the ranked source list cached")
    if hub.LB._cache_get(key_for(disco, "/api/artist/discography?nd_id=a1")) is None:
        failures.append("a terminal fill flushed the discography cache")
    if not fake.frames or fake.frames[0].get("state") != "cancelled":
        failures.append(f"terminal frame not broadcast: {fake.frames}")

    # gap and wishlist kinds pass; anything else is refused
    fake.frames.clear()
    await push({"kind": "gap", "key": "g1", "status": "downloading", "taskStatus": "running"})
    await push({"kind": "wishlist", "key": "rg-9", "state": "landed"})
    if [f.get("kind") for f in fake.frames] != ["gap", "wishlist"]:
        failures.append(f"gap/wishlist kinds not relayed: {fake.frames}")
    status, _h, _b = await push({"kind": "library", "key": "x"})
    if status != 400:
        failures.append(f"unknown kind should be 400, got {status}")
    status, _h, _b = await push({"kind": "album"})
    if status != 400:
        failures.append(f"missing key should be 400, got {status}")
    status, _h, _b = await push({"kind": "album", "key": "x"}, token="wrong")
    if status != 401:
        failures.append(f"bad token should be 401, got {status}")

    # 3. a body that never arrives is an error, never `{}`
    headers = {"Authorization": f"Bearer {hub.TOKEN}", "Content-Length": "5"}
    protocol = Protocol(b"", hang=True)
    original = hub.asyncio.wait_for

    async def impatient(coro, timeout):
        return await original(coro, timeout=0.05)

    hub.asyncio.wait_for = impatient
    try:
        raw = await hub._read_body(protocol, headers)
    finally:
        hub.asyncio.wait_for = original
    if raw is not None:
        failures.append(f"a timed-out body read returned {raw!r} instead of None")

    return failures


def main() -> int:
    failures = asyncio.run(run())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb fill: no library flush on progress/sources dropped on terminal/frame typed and bounded/bad pushes refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
