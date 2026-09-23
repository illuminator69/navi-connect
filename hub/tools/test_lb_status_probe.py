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


def main() -> int:
    failures = asyncio.run(run())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb status probe: failure-not-cached/success-cached/refused-is-502-down/"
          "timeout-is-504-busy/write-invalidates-its-read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
