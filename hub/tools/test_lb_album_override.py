#!/usr/bin/env python3
"""
I-022 / I-023: the album-override and artist-merge routes (ten of them).

Serves a stub lb-bot and drives the real `LbProxy.handle`, proving what a look
at the route table cannot:

  - every listed query param and body key reaches the stub, an unlisted one
    does not (the allowlist silently drops, so a forgotten key is invisible);
  - `force: true` arrives as a JSON boolean, not a string;
  - lb-bot's 409 (`code`, `matched`, `total`) and 404 (`code`) bodies come back
    unchanged with their status;
  - the two polled GETs are never cached, `album/covers` is;
  - each mutating POST flushes a cached `GET /lb/overrides` and the cached
    discography, while `preview` flushes nothing;
  - the status polls ride the fast pool, the MusicBrainz-bound routes the slow
    timeout, and the two gating routes are advertised.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
import urllib.parse

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

HITS: list[tuple[str, str, dict | None]] = []   # (method, path+query, json body)
NEXT_POST: list[tuple[int, dict]] = [(200, {"ok": True})]
# What the two polled GETs answer (status, body); every other GET answers 200.
POLL_ANSWER: list[tuple[int, dict]] = [(200, {})]
POLL_PATHS = ("/api/album/override", "/api/artist/merge")
POLL_PATHS_SOURCES: list[str] = []   # extra GET path answered from POLL_ANSWER


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        HITS.append(("GET", self.path, None))
        if self.path.split("?")[0] in POLL_PATHS + tuple(POLL_PATHS_SOURCES):
            status, out = POLL_ANSWER[0]
            self._send(status, {"n": len(HITS), **out})
            return
        self._send(200, {"n": len(HITS), "artists": []})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HITS.append(("POST", self.path, body))
        status, out = NEXT_POST[0]
        self._send(status, out)

    def log_message(self, *_args):
        pass


class FakeConn:
    def __init__(self, raw: bytes):
        self.reader = asyncio.StreamReader()
        self.reader.feed_data(raw)
        self.body_consumed = False


# (hub route, lb-bot path, allowed keys)
GETS = [
    ("/lb/album/override", "/api/album/override", ("album_id", "id")),
    ("/lb/album/covers", "/api/album/covers", ("rgid", "release_mbid")),
    ("/lb/artist/merge/preview", "/api/artist/merge/preview", ("nd_id", "from_nd_id")),
    ("/lb/artist/merge", "/api/artist/merge", ("id",)),
]
POSTS = [
    ("/lb/album/override/preview", "/api/album/override/preview", ("album_id", "release_mbid")),
    ("/lb/album/override", "/api/album/override",
     ("album_id", "release_mbid", "cover_url", "force")),
    ("/lb/album/cover", "/api/album/cover", ("album_id", "cover_url")),
    ("/lb/album/override/remove", "/api/album/override/remove", ("id",)),
    ("/lb/artist/merge", "/api/artist/merge", ("nd_id", "from_nd_id")),
    ("/lb/artist/merge/undo", "/api/artist/merge/undo", ("id",)),
]
MUTATING = [r for r, _p, _k in POSTS if r != "/lb/album/override/preview"]


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    original_url = hub.LBBOT_URL
    hub.LBBOT_URL = f"http://127.0.0.1:{port}"
    proxy = hub.LbProxy()
    auth = {"Authorization": f"Bearer {hub.TOKEN}"}
    failures: list[str] = []

    def get(path: str):
        return asyncio.run(proxy.handle(None, path, dict(auth), "GET"))

    def post(path: str, body: dict):
        raw = json.dumps(body).encode()
        headers = {**auth, "Content-Length": str(len(raw))}

        async def go():
            return await proxy.handle(FakeConn(raw), path, headers, "POST")
        return asyncio.run(go())

    try:
        # --- GET allowlists: every listed param arrives, an unlisted one not
        for route, upstream, keys in GETS:
            HITS.clear()
            proxy._cache.clear()
            q = urllib.parse.urlencode({**{k: f"v-{k}" for k in keys}, "secret": "leak"})
            status, _h, _b = get(f"{route}?{q}")
            if status != 200 or len(HITS) != 1:
                failures.append(f"GET {route}: status {status}, hits {HITS}")
                continue
            path, _, query = HITS[0][1].partition("?")
            got = dict(urllib.parse.parse_qsl(query))
            if path != upstream:
                failures.append(f"GET {route} reached {path}, want {upstream}")
            if got != {k: f"v-{k}" for k in keys}:
                failures.append(f"GET {route}: query should be exactly {keys}, got {got}")

        # --- POST allowlists, with `force` as a boolean -----------------------
        for route, upstream, keys in POSTS:
            HITS.clear()
            NEXT_POST[0] = (200, {"ok": True})
            sent_body = {k: (True if k == "force" else f"v-{k}") for k in keys}
            status, _h, _b = post(route, {**sent_body, "secret": "leak", "path": "/x"})
            hits = [h for h in HITS if h[0] == "POST"]
            if status != 200 or len(hits) != 1:
                failures.append(f"POST {route}: status {status}, hits {HITS}")
                continue
            if hits[0][1] != upstream:
                failures.append(f"POST {route} reached {hits[0][1]}, want {upstream}")
            if hits[0][2] != sent_body:
                failures.append(f"POST {route}: body should be exactly {sent_body}, got {hits[0][2]}")
            if "force" in keys and hits[0][2].get("force") is not True:
                failures.append(f"POST {route}: force did not arrive as boolean true: {hits[0][2]}")

        # --- error bodies pass through unchanged ------------------------------
        cases = [
            ("/lb/album/override", 409, {"error": "poor match", "code": "poor_match",
                                         "matched": 3, "total": 10}),
            ("/lb/album/override", 409, {"error": "busy", "code": "busy"}),
            ("/lb/album/override/preview", 404, {"error": "no release", "code": "unknown_release"}),
            ("/lb/album/override/remove", 404, {"error": "gone", "code": "no_override"}),
            ("/lb/artist/merge", 409, {"error": "no", "code": "different_artists"}),
            ("/lb/album/cover", 400, {"error": "bad cover_url"}),
            ("/lb/artist/merge/undo", 502, {"error": "down", "retryable": True}),
        ]
        for route, st, payload in cases:
            NEXT_POST[0] = (st, payload)
            status, _h, body = post(route, {"album_id": "a", "release_mbid": "r", "id": "i",
                                            "nd_id": "n", "from_nd_id": "f"})
            if status != st or json.loads(body) != payload:
                failures.append(f"{route}: upstream {st} {payload} came back {status} {body!r}")

        # --- polled GETs are never cached; covers is -------------------------
        for route in ("/lb/album/override?id=o1", "/lb/artist/merge?id=m1"):
            proxy._cache.clear()
            HITS.clear()
            _s, _h, first = get(route)
            _s, _h, second = get(route)
            if len(HITS) != 2:
                failures.append(f"{route}: two calls made {len(HITS)} upstream hits, want 2")
            if json.loads(first).get("n") == json.loads(second).get("n"):
                failures.append(f"{route}: the second answer is the first one replayed")
        proxy._cache.clear()
        HITS.clear()
        get("/lb/album/covers?rgid=g1")
        get("/lb/album/covers?rgid=g1")
        if len(HITS) != 1:
            failures.append(f"album/covers: two calls made {len(HITS)} upstream hits, want 1")

        # --- mutating POSTs flush overrides + discography; preview does not ---
        body_in = {"album_id": "a", "release_mbid": "r", "id": "i", "nd_id": "n",
                   "from_nd_id": "f", "cover_url": "u"}
        for route in MUTATING + ["/lb/album/override/preview"]:
            NEXT_POST[0] = (200, {"ok": True})
            proxy._cache.clear()
            get("/lb/overrides")
            get("/lb/artist/discography?id=nd1")
            HITS.clear()
            get("/lb/overrides")
            get("/lb/artist/discography?id=nd1")
            if HITS:
                failures.append(f"{route}: reads were not cached before the POST")
            post(route, body_in)
            HITS.clear()
            get("/lb/overrides")
            get("/lb/artist/discography?id=nd1")
            gets = [h[1].split("?")[0] for h in HITS if h[0] == "GET"]
            flushed = "/api/overrides" in gets and "/api/artist/discography" in gets
            if route in MUTATING and not flushed:
                failures.append(f"{route} did not flush /lb/overrides and the discography ({gets})")
            if route not in MUTATING and gets:
                failures.append(f"{route} (preview) flushed a cache: {gets}")

        # --- a refused mutating POST flushes nothing --------------------------
        proxy._cache.clear()
        NEXT_POST[0] = (409, {"code": "busy"})
        get("/lb/overrides")
        HITS.clear()
        get("/lb/overrides")
        if HITS:
            failures.append("409 case: /lb/overrides was not cached before the POST")
        for route in MUTATING:
            post(route, body_in)
        HITS.clear()
        get("/lb/overrides")
        if HITS:
            failures.append("a 409 refusal flushed the override cache")

        # --- the polled routes flush /lb/overrides on 200 and 404, not on 5xx --
        for route in ("/lb/album/override?id=o1", "/lb/artist/merge?id=m1"):
            for st, payload, flushes in ((200, {}, True),
                                         (404, {"code": "no_override"}, True),
                                         (502, {"retryable": True}, False)):
                POLL_ANSWER[0] = (st, payload)
                proxy._cache.clear()
                get("/lb/overrides")
                HITS.clear()
                get("/lb/overrides")
                if HITS:
                    failures.append(f"{route} {st}: overrides not cached before the poll")
                get(route)
                HITS.clear()
                get("/lb/overrides")
                if flushes and not HITS:
                    failures.append(f"{route} answering {st} did not flush /lb/overrides")
                if not flushes and HITS:
                    failures.append(f"{route} answering {st} flushed /lb/overrides")
        POLL_ANSWER[0] = (200, {})

        # --- /lb/notify flushes /lb/overrides too (module proxy, real handler) -
        hub.LB._cache.clear()
        asyncio.run(hub.LB.handle(None, "/lb/overrides", dict(auth), "GET"))
        HITS.clear()
        asyncio.run(hub.LB.handle(None, "/lb/overrides", dict(auth), "GET"))
        if HITS:
            failures.append("notify case: /lb/overrides was not cached before the notify")
        nraw = json.dumps({"event": "albumPlaced"}).encode()

        async def notify():
            return await hub._handle_lb_notify(
                FakeConn(nraw), "/lb/notify", {**auth, "Content-Length": str(len(nraw))}, "POST")
        asyncio.run(notify())
        HITS.clear()
        asyncio.run(hub.LB.handle(None, "/lb/overrides", dict(auth), "GET"))
        if not HITS:
            failures.append("/lb/notify did not flush the cached /lb/overrides")

        # --- B-084: `allFiles` survives the download allowlist; responses unfiltered
        HITS.clear()
        echo = {"ok": True, "existing": False, "allFiles": True, "leftOut": 6}
        NEXT_POST[0] = (200, echo)
        status, _h, out = post("/lb/album/download", {
            "rgid": "g", "release_mbid": "r", "sourceUsername": "peer",
            "sourceFolder": "f", "allFiles": True, "notAKey": "x"})
        sent = [h for h in HITS if h[0] == "POST"]
        if status != 200 or len(sent) != 1:
            failures.append(f"album/download: status {status}, hits {HITS}")
        else:
            if sent[0][2].get("allFiles") is not True:
                failures.append(f"album/download: allFiles did not reach lb-bot as true: {sent[0][2]}")
            if "notAKey" in sent[0][2]:
                failures.append("album/download: an unlisted neighbour key was forwarded")
            if sent[0][2].get("sourceUsername") != "peer" or sent[0][2].get("sourceFolder") != "f":
                failures.append(f"album/download: existing keys changed: {sent[0][2]}")
        if json.loads(out) != echo:
            failures.append(f"album/download: the allFiles echo was altered: {out!r}")
        spec = hub.LB_ROUTES[("POST", "/lb/album/download")]
        if spec.get("timeout") != hub.PROXY_SLOW_TIMEOUT or spec.get("cache"):
            failures.append("album/download: timeout or cache changed")
        rows = {"sources": [{"fileCount": 11, "extraFiles": 6}]}
        POLL_ANSWER[0] = (200, rows)
        POLL_PATHS_SOURCES.append("/api/album/sources")
        proxy._cache.clear()
        status, _h, out = get("/lb/album/sources?rgid=g")
        if json.loads(out).get("sources") != rows["sources"]:
            failures.append(f"album/sources: extraFiles did not pass through: {out!r}")
        POLL_PATHS_SOURCES.clear()
        POLL_ANSWER[0] = (200, {})

        # --- pools, by behaviour: which semaphore a request takes ------------
        used: list[str] = []

        class Spy(asyncio.Semaphore):
            def __init__(self, name, n):
                super().__init__(n)
                self.name = name

            async def acquire(self):
                used.append(self.name)
                return await super().acquire()
        proxy._pools = {name: Spy(name, 4) for name in proxy._pools}
        for route, want in (("/lb/album/override?id=o1", "fast"),
                            ("/lb/album/override?album_id=a1", "default"),
                            ("/lb/artist/merge?id=m1", "fast")):
            used.clear()
            proxy._cache.clear()
            get(route)
            if used != [want]:
                failures.append(f"{route} took pool {used}, want [{want!r}]")
        used.clear()
        POLL_ANSWER[0] = (200, {})

        # --- pools, timeouts, adverts ----------------------------------------
        for key in (("GET", "/lb/album/override"), ("GET", "/lb/artist/merge")):
            if hub.LB_ROUTES[key].get("cache"):
                failures.append(f"{key} must not be cached")
        for key in (("GET", "/lb/album/covers"), ("POST", "/lb/album/override/preview"),
                    ("POST", "/lb/album/override"), ("POST", "/lb/album/cover"),
                    ("GET", "/lb/artist/merge/preview")):
            if hub.LB_ROUTES[key].get("timeout") != hub.PROXY_SLOW_TIMEOUT:
                failures.append(f"{key} is not on PROXY_SLOW_TIMEOUT")
        for key in (("POST", "/lb/artist/merge"), ("POST", "/lb/album/override/remove"),
                    ("POST", "/lb/artist/merge/undo")):
            if hub.LB_ROUTES[key].get("timeout") != hub.PROXY_SLOW_TIMEOUT:
                failures.append(f"{key} is not on PROXY_SLOW_TIMEOUT")
        adv = proxy.advertised_routes()
        for r, _p, _k in GETS:
            if f"GET {r}" not in adv:
                failures.append(f"GET {r} is not advertised")
        for r, _p, _k in POSTS:
            if f"POST {r}" not in adv:
                failures.append(f"POST {r} is not advertised")
    finally:
        hub.LBBOT_URL = original_url
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - lb album override/merge routes: ten routes/allowlists/force-bool/"
          "error-bodies/polls-uncached/covers-cached/invalidation/pools/advertised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
