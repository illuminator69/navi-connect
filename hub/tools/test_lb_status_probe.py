#!/usr/bin/env python3
"""
The liveness probe must not remember a failure, and a client must be able to
tell "lb-bot is slow" from "lb-bot is down".

`/lb/status` rewrites any upstream status into a 200 verdict, and the proxy
cached that verdict for 60 s: after lb-bot came back, every client read
"unreachable" for a minute, and after it went away, "reachable". Only an
upstream success may be cached. And every upstream failure used to be the same
`502 unreachable`; a timeout is now `504 busy` and a refused connection
`502 down`, so a client can say which.

No hub subprocess: the upstream call is stubbed, or pointed at a socket that
accepts and never answers. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402


async def run() -> list[str]:
    failures: list[str] = []
    probe = ("GET", "/lb/status")
    spec = hub.LB_ROUTES[probe]
    answers = [(502, b'{"error":"down"}', "application/json"),
               (200, b'{"summary":1}', "application/json")]
    calls: list[str] = []

    def upstream(method, url, payload, token, timeout, label):
        calls.append(url)
        return answers.pop(0)

    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    hub._proxy_upstream_blocking = upstream
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    try:
        hub.LB._cache.clear()
        s1, b1, _c = await hub.LB.call(probe, spec, [], None)
        s2, b2, _c = await hub.LB.call(probe, spec, [], None)
        s3, b3, _c = await hub.LB.call(probe, spec, [], None)
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
    if (s1, s2, s3) != (200, 200, 200):
        failures.append(f"probe must always be 200, got {(s1, s2, s3)}")
    if json.loads(b1).get("upstreamReachable") is not False:
        failures.append("first probe should report unreachable")
    if json.loads(b2).get("upstreamReachable") is not True:
        failures.append("the unreachable verdict was cached — lb-bot came back and nobody heard")
    if len(calls) != 2:
        failures.append(f"expected 2 upstream calls (miss, miss, then a cached success), got {len(calls)}")
    if json.loads(b3).get("upstreamReachable") is not True:
        failures.append("a reachable verdict should be served from cache")

    # busy vs down
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
    status, body, _c = hub._proxy_upstream_blocking(
        "GET", f"http://127.0.0.1:{dead_port}/api/x", None, "", 2, "lbbot")
    if status != 502 or not json.loads(body).get("down"):
        failures.append(f"a refused connection should be 502 down, got {status} {body[:80]!r}")

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    accepted: list[socket.socket] = []

    def hold():
        try:
            conn, _addr = silent.accept()
            accepted.append(conn)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=hold, daemon=True).start()
    status, body, _c = hub._proxy_upstream_blocking(
        "GET", f"http://127.0.0.1:{silent.getsockname()[1]}/api/x", None, "", 0.5, "lbbot")
    for c in accepted:
        c.close()
    silent.close()
    if status != 504 or not json.loads(body).get("busy"):
        failures.append(f"a timeout should be 504 busy, got {status} {body[:80]!r}")

    # a successful write invalidates its own read
    wl_get = ("GET", "/lb/wishlist")
    key = json.dumps([wl_get, "/api/wishlist", [], None], sort_keys=True, default=str)
    hub.LB._cache_put(key, 200, b'{"wishlist":[]}', "application/json")
    hub._proxy_upstream_blocking = lambda *a, **k: (200, b'{"ok":true}', "application/json")
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    try:
        await hub.LB.call(("POST", "/lb/wishlist"), hub.LB_ROUTES[("POST", "/lb/wishlist")],
                          [], {"rgid": "r1"})
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
    if hub.LB._cache_get(key) is not None:
        failures.append("adding to the wishlist left the cached wishlist in place")
    return failures


async def run_failure_propagation() -> list[str]:
    """B-005: `/lb/status` used to answer `upstreamReachable: true` for up to
    PROXY_CACHE_TTL (60 s) after lb-bot died, because nothing told its cache
    entry lb-bot had gone away in the meantime — only the probe route's OWN
    next call (or the 60 s probe_loop tick) ever moved the verdict. A refused
    connection (`down`) on any OTHER lb-bot route must invalidate that cache
    entry and flip the verdict immediately; a timeout (`busy`) only invalidates
    the cache entry — a slow lb-bot is not a dead one (ruling R18).
    """
    failures: list[str] = []
    probe = ("GET", "/lb/status")
    probe_spec = hub.LB_ROUTES[probe]
    fresh_get = ("GET", "/lb/fresh-releases")
    fresh_spec = hub.LB_ROUTES[fresh_get]
    key = json.dumps([probe, probe_spec["path"], [], None], sort_keys=True, default=str)

    original = hub._proxy_upstream_blocking
    original_upstream = type(hub.LB).upstream
    type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
    hub.LB._cache.clear()
    try:
        # --- a refused connection on an unrelated route ------------------------ #
        hub.LB._set_available(False)
        hub._proxy_upstream_blocking = lambda *a, **k: (200, b'{"summary":1}', "application/json")
        _s, body, _c = await hub.LB.call(probe, probe_spec, [], None)
        if json.loads(body).get("upstreamReachable") is not True:
            failures.append("setup: priming /lb/status didn't report reachable")
        if hub.LB.available is not True:
            failures.append("setup: priming /lb/status didn't set the verdict True")

        hub._proxy_upstream_blocking = lambda *a, **k: (
            502, b'{"error":"lbbot unreachable","down":true}', "application/json")
        await hub.LB.call(fresh_get, fresh_spec, [], None)

        if hub.LB.available is not False:
            failures.append("a refused connection on another route didn't flip the verdict")
        if hub.LB._cache_get(key) is not None:
            failures.append("the stale /lb/status cache entry survived a failure on another route")

        hub._proxy_upstream_blocking = lambda *a, **k: (
            502, b'{"error":"lbbot unreachable","down":true}', "application/json")
        _s, body, _c = await hub.LB.call(probe, probe_spec, [], None)
        if json.loads(body).get("upstreamReachable") is not False:
            failures.append("the next /lb/status call still answered reachable after invalidation")

        # --- a timeout on an unrelated route is BUSY, not down (ruling R18) ---- #
        # A 504 `busy` is a healthy lb-bot that is still working (a rate-limited
        # MusicBrainz hop, a 150 s source search). Flipping the hub-wide verdict
        # on it blanked every client's lb-bot layer until the next probe tick.
        # It only drops /lb/status's cached answer, so the next call re-asks.
        hub.LB._cache.clear()
        hub.LB._set_available(False)
        hub._proxy_upstream_blocking = lambda *a, **k: (200, b'{"summary":1}', "application/json")
        await hub.LB.call(probe, probe_spec, [], None)
        hub._proxy_upstream_blocking = lambda *a, **k: (
            504, b'{"error":"lbbot timed out","busy":true}', "application/json")
        await hub.LB.call(fresh_get, fresh_spec, [], None)
        if hub.LB.available is not True:
            failures.append("a timeout (busy) on another route flipped the verdict to unavailable")
        if hub.LB._cache_get(key) is not None:
            failures.append("the /lb/status cache entry survived a timeout on another route")
        busy_calls: list[str] = []

        def healthy(method, url, *a, **k):
            busy_calls.append(url)
            return (200, b'{"summary":1}', "application/json")

        hub._proxy_upstream_blocking = healthy
        _s, body, _c = await hub.LB.call(probe, probe_spec, [], None)
        if len(busy_calls) != 1:
            failures.append("the /lb/status call after a busy route didn't re-probe upstream")
        if json.loads(body).get("upstreamReachable") is not True:
            failures.append("/lb/status after a busy route didn't answer upstreamReachable: true")

        # --- a 502 tooLarge is NOT a reachability failure ----------------------- #
        hub.LB._cache.clear()
        hub.LB._set_available(False)
        hub._proxy_upstream_blocking = lambda *a, **k: (200, b'{"summary":1}', "application/json")
        await hub.LB.call(probe, probe_spec, [], None)
        hub._proxy_upstream_blocking = lambda *a, **k: (
            502, b'{"error":"lbbot response exceeded the proxy limit","tooLarge":true}',
            "application/json")
        await hub.LB.call(fresh_get, fresh_spec, [], None)
        if hub.LB.available is not True:
            failures.append("an oversized (tooLarge) response wrongly flipped the verdict")
        if hub.LB._cache_get(key) is None:
            failures.append("an oversized (tooLarge) response wrongly dropped /lb/status's cache")
    finally:
        hub._proxy_upstream_blocking = original
        type(hub.LB).upstream = original_upstream
        hub.LB._cache.clear()
    return failures


def main() -> int:
    failures = asyncio.run(run())
    failures += asyncio.run(run_failure_propagation())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb status probe: failure-not-cached/success-cached/refused-is-502-down/"
          "timeout-is-504-busy/write-invalidates-its-read/"
          "other-route-down-flips-verdict/other-route-busy-only-drops-status-cache/"
          "tooLarge-is-not-a-failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
