#!/usr/bin/env python3
"""
Regression test for the inbound lb-bot notify: `POST /lb/notify`.

Two things are checked, both about how fast a landed album reaches the clients:

1. **The cache is cleared before the broadcast.** Every client answers a `library`
   frame by re-reading the artist's discography. Before this, that re-read was served
   the hub's own 60 s cached copy — the album still "missing" — so the frame
   announcing a landing was followed by a minute of the old answer.
   A read already in flight when the notify arrives must not re-cache what it fetched.

2. **The frame carries the new album's handles** (`ndAlbumIds`, `ndArtistId`, `row`),
   rebuilt field by field and bounded, so a client can fetch the one album
   instead of re-syncing its whole library.

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
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def readexactly(self, n: int) -> bytes:
        out, self._data = self._data[:n], self._data[n:]
        return out


class Protocol:
    def __init__(self, body: bytes) -> None:
        self.reader = Reader(body)


class FakeHub:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def _broadcast(self, obj: dict) -> None:
        self.frames.append(obj)


async def notify(payload: dict) -> tuple:
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {hub.TOKEN}", "Content-Length": str(len(body))}
    return await hub._handle_lb_notify(Protocol(body), "/lb/notify", headers, "POST")


def key_for(route: tuple[str, str], path: str) -> str:
    return json.dumps([route, path, [], None], sort_keys=True, default=str)


async def run() -> list[str]:
    failures: list[str] = []
    fake = FakeHub()
    hub.HUB_INSTANCE = fake

    disco = ("GET", "/lb/artist/discography")
    releases = ("GET", "/lb/album/releases")
    hub.LB._cache.clear()
    hub.LB._cache_put(key_for(disco, "/api/artist/discography?nd_id=a1"), 200, b"{}", "application/json")
    hub.LB._cache_put(key_for(releases, "/api/album/releases?rgid=r"), 200, b"{}", "application/json",
                      hub.PROXY_CACHE_TTL_LONG)
    gen_before = hub.LB._cache_gen

    status, _headers, _body = await notify({
        "event": "albumIndexed", "release_mbid": "rel-1", "rgid": "rg-1",
        "artist": "A", "album": "B", "nd_artist_id": "ar-9",
        "nd_album_ids": ["al-1", 7, "al-2"] + [f"x{i}" for i in range(40)],
        "row": {"rgid": "rg-1", "title": "B", "status": "present", "present": 12,
                "total": True, "navidrome_album_ids": ["al-1"], "evil": {"nested": 1},
                "match_score": 0.9},
        "extra": "dropped",
    })
    if status != 200:
        failures.append(f"notify should answer 200, got {status}")

    if hub.LB._cache_get(key_for(disco, "/api/artist/discography?nd_id=a1")) is not None:
        failures.append("discography cache survived the notify — clients re-read the stale answer")
    if hub.LB._cache_get(key_for(releases, "/api/album/releases?rgid=r")) is None:
        failures.append("long-TTL MusicBrainz route was purged — a fill changes nothing there")
    if hub.LB._cache_gen == gen_before:
        failures.append("cache generation not bumped — an in-flight read would re-cache stale data")

    if len(fake.frames) != 1:
        failures.append(f"expected one broadcast, got {len(fake.frames)}")
        return failures
    frame = fake.frames[0]
    expect = {"t": "library", "event": "albumIndexed", "releaseMbid": "rel-1",
              "rgid": "rg-1", "ndArtistId": "ar-9"}
    for k, v in expect.items():
        if frame.get(k) != v:
            failures.append(f"frame[{k!r}] = {frame.get(k)!r}, expected {v!r}")
    ids = frame.get("ndAlbumIds") or []
    if ids[:2] != ["al-1", "al-2"] or len(ids) > hub._LB_NOTIFY_IDS_MAX:
        failures.append(f"ndAlbumIds not filtered/bounded: {ids[:4]}… ({len(ids)})")
    if "extra" in frame:
        failures.append("unlisted payload key relayed to clients")
    row = frame.get("row") or {}
    if row.get("present") != 12 or "total" in row or "evil" in row:
        failures.append(f"row not rebuilt field by field: {row}")
    if row.get("navidrome_album_ids") != ["al-1"]:
        failures.append(f"row lost navidrome_album_ids: {row}")

    # An older lb-bot sends only the original fields; that must still work.
    fake.frames.clear()
    await notify({"release_mbid": "rel-2", "rgid": "rg-2"})
    if not fake.frames or fake.frames[0].get("event") != "albumPlaced" or "row" in fake.frames[0]:
        failures.append(f"legacy notify changed shape: {fake.frames}")

    # A read that started before the notify must not write its answer back.
    route_spec = hub.LB_ROUTES[disco]
    calls = []

    def slow_upstream(method, url, payload, token, timeout, label):
        # The notify arrives while this read is upstream. Mid-flight, on the
        # worker thread, is the only placement that exercises the race.
        calls.append(url)
        hub.LB.invalidate(hub.LB_LIBRARY_ROUTES)
        return 200, b'{"stale": true}', "application/json"

    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    hub._proxy_upstream_blocking = slow_upstream
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    try:
        hub.LB._cache.clear()
        params = [("nd_id", "a2")]

        await hub.LB.call(disco, route_spec, params, None)
        if not calls:
            failures.append("race test never reached upstream")
        elif hub.LB._cache:
            failures.append("a read in flight across an invalidation re-cached its stale answer")
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream

    return failures


def main() -> int:
    failures = asyncio.run(run())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb notify: cache purged before broadcast/in-flight not re-cached/frame bounded/legacy shape")
    return 0


if __name__ == "__main__":
    sys.exit(main())
