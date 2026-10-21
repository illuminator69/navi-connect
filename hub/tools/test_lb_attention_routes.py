#!/usr/bin/env python3
"""
P6 (§15.6): `GET /lb/attention` and `GET /lb/activity`, the phone's Needs
attention and Source history.

Serves a stub lb-bot and drives the real `LbProxy.handle`, proving:

  - both reach their upstream path with the token and answer 401 without it;
    neither forwards a parameter (the route lists none);
  - a body carrying a folder's `name`/`path`/`identifyError`/`coverUrl` or a
    transfer's `filename`/`path`/`title` comes out without them, and a walk of
    every key of a worst-case answer finds none of those names anywhere;
  - the answer is cached (a second read inside the TTL costs nothing upstream)
    for 20 s, and two concurrent reads cost one upstream call;
  - both ride the fast pool: with every default slot held by a slow
    `album/sources` search, they still answer at once;
  - an lb-bot older than the routes (bare 404) reaches the client as that same
    bare 404, no `code` added, and is not cached; a refused upstream is 502 `down`;
  - a terminal fill push and `/lb/notify` drop both cached answers; a progress
    push does not;
  - both are advertised, and the route table still has no `/api/downloads` and
    no write route onto the placements or transfers.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

HITS: list[tuple[str, str, dict]] = []
HITS_LOCK = threading.Lock()
STATUS = [200]      # what /api/attention and /api/activity answer
BODY_404 = [b'{"error": "not found"}']
SLOW_SOURCES = [0.0]
SLOW_READ = [0.0]

# Never anywhere in either answer. A folder's `coverUrl` (lb-bot's own cover route)
# is checked on the folders alone: an album row's Cover Art Archive URL is allowed.
FORBIDDEN = {"name", "path", "identifyError", "filename", "title"}

ATTENTION = {
    "version": 1, "serverTime": 1761000000.0,
    "counts": {"failedGaps": 3, "failedRequests": 1, "folders": 16,
               "foldersUnidentified": 12, "picks": 14},
    "albums": [{"kind": "gap", "key": "g1", "groupId": "g1", "releaseMbid": "",
                "rgid": "rg1", "albumId": "", "artist": "A", "album": "B",
                "coverUrl": "https://coverartarchive.org/x.jpg", "state": "failed",
                "missingCount": 3, "total": 11, "updatedAt": 1761000000.0}],
    # an lb-bot that got its allowlist wrong: the hub must still drop these
    "folders": [{"id": "abcdef012345", "fileCount": 11, "formats": ["flac"],
                 "matchLabel": "", "confidence": 0, "canConfirm": False,
                 "groupId": "", "releaseMbid": "", "identified": False,
                 "name": "Some.Folder-2001", "path": "/downloads/Some.Folder-2001",
                 "identifyError": "no match in /downloads/x", "coverUrl": "/api/x"}],
    "truncated": {"albums": 0, "folders": 0},
}
ACTIVITY = {
    "version": 1, "serverTime": 1761000000.0,
    "requests": [{"releaseMbid": "rel1", "rgid": "rg1", "groupId": "", "albumId": "",
                  "artist": "A", "album": "B", "state": "downloading", "percent": 40,
                  "done": 4, "total": 10, "failureKind": "", "reason": "",
                  "retryable": False, "gapFill": False, "updatedAt": 1761000000.0}],
    "transfers": [{"id": "t1", "kind": "album", "album": "B", "artist": "A",
                   "groupId": "", "state": "downloading", "pct": 40, "peer": "p",
                   "filename": "@@peer\\Music\\A\\B\\01.flac",
                   "path": "/downloads/x/01.flac", "title": "A - B (01.flac)"}],
}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        with HITS_LOCK:
            HITS.append(("GET", path, dict(urllib.parse.parse_qsl(query))))
        if path == "/api/album/sources":
            time.sleep(SLOW_SOURCES[0])
            self._send(200, b"{}")
        elif path in ("/api/attention", "/api/activity"):
            time.sleep(SLOW_READ[0])
            if STATUS[0] == 404:
                self._send(404, BODY_404[0])
            elif STATUS[0] != 200:
                self._send(STATUS[0], b'{"error": "x"}')
            else:
                body = ATTENTION if path == "/api/attention" else ACTIVITY
                self._send(200, json.dumps(body).encode())
        else:
            self._send(404, b"{}")

    def log_message(self, *_args):
        pass


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


def walk_keys(node, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            walk_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_keys(v, out)
    return out


def hits(path: str) -> int:
    with HITS_LOCK:
        return sum(1 for h in HITS if h[1] == path)


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    original_url = hub.LBBOT_URL
    hub.LBBOT_URL = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {hub.TOKEN}"}
    failures: list[str] = []

    try:
        proxy = hub.LbProxy()

        def get(path: str, headers=None):
            return asyncio.run(proxy.handle(None, path, dict(auth if headers is None else headers), "GET"))

        for route, upstream in (("/lb/attention", "/api/attention"),
                                ("/lb/activity", "/api/activity")):
            spec = hub.LB_ROUTES.get(("GET", route))
            if spec is None:
                failures.append(f"{route} is not in LB_ROUTES")
                continue

            # --- auth ----------------------------------------------------------
            HITS.clear(); proxy._cache.clear()
            status, _h, _b = get(route, headers={})
            if status != 401 or HITS:
                failures.append(f"{route} without the token should be 401 and reach nothing, "
                                f"got {status}, {HITS}")

            # --- path, no params forwarded ---------------------------------------
            HITS.clear(); proxy._cache.clear()
            status, _h, body = get(f"{route}?limit=9999&path=/etc&secret=leak")
            if status != 200:
                failures.append(f"{route} should answer 200, got {status}")
                continue
            if HITS != [("GET", upstream, {})]:
                failures.append(f"{route} should reach {upstream} with no params: {HITS}")

            # --- the strip: no forbidden key anywhere ------------------------------
            data = json.loads(body)
            leaked = walk_keys(data, set()) & FORBIDDEN
            if leaked:
                failures.append(f"{route} answer still carries {sorted(leaked)}")
            text = body.decode()
            for secret in ("/downloads/", "Some.Folder-2001", "01.flac"):
                if secret in text:
                    failures.append(f"{route} answer still contains {secret!r}")
            if route == "/lb/attention":
                f = (data.get("folders") or [{}])[0]
                if f.get("fileCount") != 11 or f.get("id") != "abcdef012345":
                    failures.append(f"attention folder lost an allowed field: {f}")
                if data.get("counts", {}).get("folders") != 16:
                    failures.append(f"attention counts did not survive: {data.get('counts')}")
                if any("coverUrl" in row for row in data.get("folders") or []):
                    failures.append("a folder row still carries coverUrl")
                if (data.get("albums") or [{}])[0].get("coverUrl") is None:
                    failures.append("an album row's coverUrl is allowed and must survive")
            else:
                t = (data.get("transfers") or [{}])[0]
                if t.get("peer") != "p" or t.get("pct") != 40:
                    failures.append(f"activity transfer lost an allowed field: {t}")
                if (data.get("requests") or [{}])[0].get("album") != "B":
                    failures.append("a request row's album is allowed and must survive")

            # --- cached 20 s --------------------------------------------------------
            if spec.get("ttl") != 20.0 or not spec.get("cache"):
                failures.append(f"{route} should be cached for 20 s: {spec.get('cache')}, {spec.get('ttl')}")
            HITS.clear()
            get(route)
            if hits(upstream) != 0:
                failures.append(f"a second {route} read inside the TTL reached upstream")

            # --- two concurrent reads, one upstream call ----------------------------
            HITS.clear(); proxy._cache.clear()
            SLOW_READ[0] = 0.3

            async def two(r=route):
                return await asyncio.gather(
                    proxy.handle(None, r, dict(auth), "GET"),
                    proxy.handle(None, r, dict(auth), "GET"))
            try:
                results = asyncio.run(two())
            finally:
                SLOW_READ[0] = 0.0
            if hits(upstream) != 1 or [r[0] for r in results] != [200, 200]:
                failures.append(f"two concurrent {route} reads should cost one upstream call, "
                                f"got {hits(upstream)} ({[r[0] for r in results]})")

            # --- fast pool -----------------------------------------------------------
            if spec.get("pool") != "fast":
                failures.append(f"{route} is not on the fast pool")

            # --- an older lb-bot: bare 404 passes through, uncached -----------------
            STATUS[0] = 404
            try:
                proxy._cache.clear(); HITS.clear()
                status, _h, body = get(route)
                status2, _h2, _b2 = get(route)
            finally:
                STATUS[0] = 200
            if status != 404 or body != BODY_404[0]:
                failures.append(f"{route}: an lb-bot 404 should pass through untouched, "
                                f"got {status} {body!r}")
            elif "code" in json.loads(body):
                failures.append(f"{route}: the 404 gained a code")
            if status2 != 404 or hits(upstream) != 2:
                failures.append(f"{route}: a 404 must not be cached ({hits(upstream)} hits)")

            # --- an lb-bot 503 passes through, uncached -------------------------------
            STATUS[0] = 503
            try:
                proxy._cache.clear()
                status, _h, _b = get(route)
            finally:
                STATUS[0] = 200
            if status != 503:
                failures.append(f"{route}: an lb-bot 503 should pass through, got {status}")

        # --- fast pool in practice: every default slot held by a slow search ------
        async def held():
            SLOW_SOURCES[0] = 3.0
            proxy._cache.clear()
            slow = [asyncio.create_task(proxy.handle(
                None, f"/lb/album/sources?rgid=r{i}", dict(auth), "GET"))
                for i in range(hub.PROXY_MAX_INFLIGHT)]
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            a = await proxy.handle(None, "/lb/attention", dict(auth), "GET")
            b = await proxy.handle(None, "/lb/activity", dict(auth), "GET")
            took = time.monotonic() - t0
            await asyncio.gather(*slow)
            return a[0], b[0], took
        try:
            sa, sb, took = asyncio.run(held())
        finally:
            SLOW_SOURCES[0] = 0.0
        if (sa, sb) != (200, 200) or took > 1.5:
            failures.append(f"with every default slot held, attention/activity took {took:.2f}s "
                            f"({sa}, {sb}): not on the fast pool")

        # --- a refused upstream is 502 down ----------------------------------------
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        hub.LBBOT_URL = f"http://127.0.0.1:{dead}"
        down = hub.LbProxy()
        status, _h, body = asyncio.run(down.handle(None, "/lb/attention", dict(auth), "GET"))
        if status != 502 or not json.loads(body).get("down"):
            failures.append(f"a refused lb-bot should be 502 down, got {status} {body!r}")
        hub.LBBOT_URL = f"http://127.0.0.1:{port}"

        # --- invalidation by pushes --------------------------------------------------
        fake = FakeHub()
        hub.HUB_INSTANCE = fake

        def seed() -> None:
            hub.LB._cache.clear()
            for route, upstream in (("/lb/attention", "/api/attention"),
                                    ("/lb/activity", "/api/activity")):
                key = json.dumps([("GET", route), upstream, [], None], sort_keys=True, default=str)
                hub.LB._cache_put(key, 200, b"{}", "application/json")

        async def push(path: str, payload: dict):
            raw = json.dumps(payload).encode()
            headers = {**auth, "Content-Length": str(len(raw))}
            handler = hub._handle_lb_fill if path == "/lb/fill" else hub._handle_lb_notify
            return await handler(Protocol(raw), path, headers, "POST")

        seed()
        asyncio.run(push("/lb/fill", {"kind": "album", "key": "rg1", "rgid": "rg1",
                                      "state": "downloading", "percent": 40}))
        if len(hub.LB._cache) != 2:
            failures.append("a progress push must leave the attention/activity cache alone")
        for payload in ({"kind": "album", "key": "rg1", "state": "failed"},
                        {"kind": "album", "key": "rg1", "state": "verified"},
                        {"kind": "album", "key": "rg1", "state": "cancelled"},
                        {"kind": "gap", "key": "g1", "status": "picking"},
                        {"kind": "gap", "key": "g1", "status": "failed"}):
            seed()
            asyncio.run(push("/lb/fill", payload))
            if hub.LB._cache:
                failures.append(f"a settling push {payload} should drop both cached answers")
        seed()
        asyncio.run(push("/lb/notify", {"event": "albumPlaced", "rgid": "rg1"}))
        if hub.LB._cache:
            failures.append("/lb/notify should drop both cached answers")

        # --- advertised; nothing unsafe in the table ---------------------------------
        adv = proxy.advertised_routes()
        for want in ("GET /lb/attention", "GET /lb/activity"):
            if want not in adv:
                failures.append(f"{want!r} is not advertised")
        for (method, path), spec in hub.LB_ROUTES.items():
            up = spec.get("path", "")
            if up.startswith("/api/downloads"):
                failures.append(f"{method} {path} reaches /api/downloads")
            if method != "GET" and (up.startswith("/api/placements")
                                    or up.startswith("/api/transfers")):
                failures.append(f"{method} {path} is a write onto {up}")
    finally:
        hub.LBBOT_URL = original_url
        hub.HUB_INSTANCE = None
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - lb attention/activity: auth/no-params/strip-walk/cached-20s/coalesced"
          "/fast-pool/404-passes-uncached/502-down/settling-push-and-notify-flush"
          "/advertised/no-downloads-no-writes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
