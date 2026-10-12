#!/usr/bin/env python3
"""
Q-017(c): `/lb/gap` must not ship absolute download paths or a peer's raw
filenames to a device.

lb-bot's gap view (`GET /api/gaps/{group_id}`) carries `tracks[].placeFolder`
(an absolute `/downloads/<folder>` path on the lb-bot HOST) and
`tracks[].placeFile` (a peer's filename) alongside `sources[].files` (which
the route already strips — see the `strip` comment on `("GET", "/lb/gap")` in
LB_ROUTES). No Feishin/Navic/PROTOCOL code reads `placeFolder`/`placeFile`
(lb-bot's own SPA reads them directly from lb-bot, not via the hub), so they
should never reach a client through the hub proxy either.

Q-043: the same holds for `tracks[].manualPick`, lb-bot's record of the file
picked by hand for a track — its `filename` is a peer's raw slskd path. Neither
client reads any key of it, so the whole object is dropped.

No hub subprocess: the upstream call is stubbed with a synthetic `/lb/gap`
body, same technique as test_lb_status_probe.py. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402


async def run() -> list[str]:
    failures: list[str] = []
    route = ("GET", "/lb/gap")
    spec = hub.LB_ROUTES[route]

    upstream_body = json.dumps({
        "groupId": "g1",
        "sources": [
            {"peer": "alice", "score": 5, "files": ["01 - Song.flac", "02 - Song.flac"],
             "filesTruncated": False},
        ],
        "tracks": [
            {"id": "t1", "title": "Track One", "placeFolder": "/downloads/Artist - Album",
             "placeFile": "01 - Track One.flac"},
            {"id": "t2", "title": "Track Two", "placeFolder": "/downloads/Artist - Album",
             "placeFile": "02 - Track Two.flac"},
        ],
    }).encode()

    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    hub._proxy_upstream_blocking = lambda *a, **k: (200, upstream_body, "application/json")
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    hub.LB._cache.clear()
    try:
        status, body, _ctype = await hub.LB.call(route, spec, [("group_id", "g1")], None)
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
        hub.LB._cache.clear()

    if status != 200:
        failures.append(f"expected 200, got {status}")
        return failures

    payload = json.loads(body)
    tracks = payload.get("tracks") or []
    if len(tracks) != 2:
        failures.append(f"expected 2 tracks in the proxied body, got {len(tracks)}")
    for t in tracks:
        if "placeFolder" in t:
            failures.append(f"track {t.get('id')!r} still carries placeFolder: {t.get('placeFolder')!r}")
        if "placeFile" in t:
            failures.append(f"track {t.get('id')!r} still carries placeFile: {t.get('placeFile')!r}")
        if "id" not in t or "title" not in t:
            failures.append(f"stripping placeFolder/placeFile also dropped other track fields: {t}")

    sources = payload.get("sources") or []
    for s in sources:
        if "files" in s or "filesTruncated" in s:
            failures.append(f"source {s.get('peer')!r} still carries a file listing: {s}")
        if "peer" not in s or "score" not in s:
            failures.append(f"stripping sources.files also dropped other source fields: {s}")

    return failures


POST_ROUTES = [
    ("POST", "/lb/gap/fetch"),
    ("POST", "/lb/gap/auto"),
    ("POST", "/lb/gap/cancel"),
    ("POST", "/lb/gap/rescan"),
    ("POST", "/lb/album/allow-mp3"),
]


def _post_body(extra: dict) -> bytes:
    gap = {
        "groupId": "g1",
        "sources": [{"peer": "alice", "score": 5, "files": ["01 - Song.flac"],
                     "filesTruncated": False}],
        "tracks": [{"id": "t1", "title": "Track One",
                    "placeFolder": "/downloads/Artist - Album",
                    "placeFile": "01 - Track One.flac"}],
    }
    return json.dumps({"ok": True, "gap": gap, **extra}).encode()


async def run_posts() -> list[str]:
    """Q-018: lb-bot nests the same gap view under `gap` on the POST replies."""
    failures: list[str] = []
    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    try:
        for route in POST_ROUTES:
            for label, extra in (("success", {}), ("alreadyActive", {"alreadyActive": True})):
                upstream_body = _post_body(extra)
                hub._proxy_upstream_blocking = lambda *a, _b=upstream_body, **k: (200, _b, "application/json")
                hub.LB._cache.clear()
                status, body, _c = await hub.LB.call(
                    route, hub.LB_ROUTES[route], [], {"group_id": "g1", "sourceId": "s", "allow": True})
                tag = f"{route[1]} ({label})"
                if status != 200:
                    failures.append(f"{tag}: expected 200, got {status}")
                    continue
                payload = json.loads(body)
                gap = payload.get("gap") or {}
                if payload.get("ok") is not True:
                    failures.append(f"{tag}: ok dropped")
                if label == "alreadyActive" and payload.get("alreadyActive") is not True:
                    failures.append(f"{tag}: alreadyActive dropped")
                for t in gap.get("tracks") or []:
                    for k in ("placeFolder", "placeFile"):
                        if k in t:
                            failures.append(f"{tag}: gap.tracks still carries {k}")
                    if "id" not in t or "title" not in t:
                        failures.append(f"{tag}: other track fields dropped: {t}")
                for s_ in gap.get("sources") or []:
                    if "files" in s_ or "filesTruncated" in s_:
                        failures.append(f"{tag}: gap.sources still carries a file listing")
                    if "peer" not in s_ or "score" not in s_:
                        failures.append(f"{tag}: other source fields dropped: {s_}")
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
        hub.LB._cache.clear()

    # Dotted-path edge cases: odd shapes pass through untouched, never raise.
    for odd in ({"ok": True}, {"gap": None}, {"gap": "x"}, {"gap": {"tracks": "nope"}},
                {"gap": {"tracks": [1, None, {"placeFolder": "/d", "id": "t"}]}}):
        raw = json.dumps(odd).encode()
        out = json.loads(hub.LbProxy._strip({"strip": (("gap.tracks", "placeFolder"),)}, 200, raw))
        expect = json.loads(json.dumps(odd))
        if "gap" in odd and isinstance(odd["gap"], dict) and isinstance(odd["gap"].get("tracks"), list):
            expect["gap"]["tracks"][2].pop("placeFolder")
        if out != expect:
            failures.append(f"dotted-key edge case {odd!r}: got {out!r}")
    return failures


def _gap_without_pick() -> dict:
    """A gap view carrying nothing the projection removes."""
    return {
        "groupId": "g1", "status": "picking",
        "sources": [{"peer": "alice", "score": 5}],
        "tracks": [{"id": "t1", "position": 1, "title": "Track One", "state": "downloaded"},
                   {"id": "t2", "position": 2, "title": "Track Two", "state": "missing"},
                   {"id": "t3", "position": 3, "title": "Track Three", "state": "present"}],
    }


def _gap_with_pick() -> dict:
    """The same gap with a file picked by hand for one track: one row carries
    the pick, one a null, one no key at all."""
    gap = _gap_without_pick()
    gap["tracks"][0]["manualPick"] = {
        "filename": "@@peer\\Music\\Artist - Album\\01 - Track One.flac"}
    gap["tracks"][1]["manualPick"] = None
    return gap


async def run_manual_pick() -> list[str]:
    """Q-043: `tracks[].manualPick` goes, on the poll and on every POST that echoes
    the gap; a gap that never carried one comes out as it went in."""
    failures: list[str] = []
    clean = _gap_without_pick()
    get_route = ("GET", "/lb/gap")
    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    try:
        for label, gap in (("with a manualPick", _gap_with_pick()),
                           ("without a manualPick", _gap_without_pick())):
            upstream_body = json.dumps(gap).encode()
            hub._proxy_upstream_blocking = lambda *a, _b=upstream_body, **k: (200, _b, "application/json")
            hub.LB._cache.clear()
            status, body, _c = await hub.LB.call(
                get_route, hub.LB_ROUTES[get_route], [("group_id", "g1")], None)
            if status != 200:
                failures.append(f"/lb/gap ({label}): expected 200, got {status}")
            elif json.loads(body) != clean:
                failures.append(f"/lb/gap ({label}): expected the gap minus manualPick and "
                                f"nothing else, got {json.loads(body)!r}")

            upstream_body = json.dumps({"ok": True, "gap": gap}).encode()
            hub._proxy_upstream_blocking = lambda *a, _b=upstream_body, **k: (200, _b, "application/json")
            for route in POST_ROUTES:
                hub.LB._cache.clear()
                status, body, _c = await hub.LB.call(
                    route, hub.LB_ROUTES[route], [], {"group_id": "g1", "sourceId": "s", "allow": True})
                if status != 200:
                    failures.append(f"{route[1]} ({label}): expected 200, got {status}")
                elif json.loads(body) != {"ok": True, "gap": clean}:
                    failures.append(f"{route[1]} ({label}): expected the gap minus manualPick "
                                    f"and nothing else, got {json.loads(body)!r}")
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
        hub.LB._cache.clear()

    # The real rule set over shapes it cannot apply to: no `tracks`, a `tracks`
    # that is not a list, rows that are not objects. Those pass through, and must
    # not cost the object row beside them its strip (`_strip` swallows an error
    # by handing the whole body back).
    for odd in ({"groupId": "g1"}, {"tracks": None}, {"tracks": "nope"},
                {"tracks": [1, None, "manualPick", ["manualPick"],
                            {"id": "t", "manualPick": {"filename": "x"}}]}):
        raw = json.dumps(odd).encode()
        out = json.loads(hub.LbProxy._strip({"strip": hub.GAP_STRIP_RULES}, 200, raw))
        expect = json.loads(raw)
        if isinstance(expect.get("tracks"), list):
            expect["tracks"][-1].pop("manualPick")
        if out != expect:
            failures.append(f"gap rules over {odd!r}: got {out!r}")
    return failures


def main() -> int:
    failures = asyncio.run(run()) + asyncio.run(run_posts()) + asyncio.run(run_manual_pick())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb gap strip: tracks.placeFolder/placeFile/manualPick stripped, "
          "sources.files stripped, other fields survive; POST replies' gap.* stripped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
