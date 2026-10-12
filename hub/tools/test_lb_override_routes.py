#!/usr/bin/env python3
"""
I-019: the artist-MBID override routes — `GET /lb/overrides`,
`POST /lb/artist/override`, `POST /lb/artist/override/remove`.

Serves a stub lb-bot and drives the real `LbProxy.handle` (POSTs included, with
a stand-in connection carrying the body), which proves what a look at the
route table cannot:

  - each route reaches the right upstream path;
  - `nd_id`, `mbid`, `name` survive the body allowlist and an unlisted key is
    dropped (the allowlist is the security control — and the failure mode of a
    forgotten key is silent);
  - the GET is cached, and a successful POST evicts BOTH the cached override
    map and the cached discography, while a refused POST (409) evicts neither;
  - all three are advertised, which is what clients gate the surface on;
  - lb-bot's 400 / 404 / 409 / 502 bodies reach the client with their status.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

HITS: list[tuple[str, str, dict | None]] = []   # (method, path, json body)
# What the next override POST answers: (status, body). Mutated per case.
NEXT_POST: list[tuple[int, dict]] = [(200, {"ok": True})]
GET_STATUS = [200]   # what GET /api/overrides answers (an older lb-bot: 404)
REVISION = [1]   # bumped so a cached answer and a fresh one are distinguishable


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
        path = self.path.split("?")[0]
        if path == "/api/overrides" and GET_STATUS[0] != 200:
            self._send(GET_STATUS[0], {"error": "not found"})
        elif path == "/api/overrides":
            self._send(200, {"artists": [{
                "ndArtistId": "nd1", "mbid": "m1", "name": "X", "tagMbid": "",
                "setAt": 1.0 + REVISION[0]}]})
        elif path == "/api/artist/discography":
            self._send(200, {"rev": REVISION[0]})
        else:
            self._send(404, {})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HITS.append(("POST", self.path, body))
        if self.path in ("/api/artist/override", "/api/artist/override/remove"):
            status, out = NEXT_POST[0]
            self._send(status, out)
        else:
            self._send(404, {})

    def log_message(self, *_args):
        pass


class FakeConn:
    """Just enough of the websockets protocol for `_read_body`."""
    def __init__(self, raw: bytes):
        self.reader = asyncio.StreamReader()
        self.reader.feed_data(raw)
        self.body_consumed = False


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

    def posts() -> list[tuple[str, str, dict | None]]:
        return [h for h in HITS if h[0] == "POST"]

    try:
        # --- GET /lb/overrides: reachable, right path, cached ---------------
        HITS.clear()
        status, _h, body = get("/lb/overrides?secret=leak")
        if status != 200 or json.loads(body).get("artists", [{}])[0].get("ndArtistId") != "nd1":
            failures.append(f"/lb/overrides did not pass the upstream body through: {status}")
        if not HITS or HITS[0][1] != "/api/overrides":
            failures.append(f"/lb/overrides reached the wrong upstream path or query: {HITS}")
        before = len(HITS)
        get("/lb/overrides")
        if len(HITS) != before:
            failures.append("a repeat GET /lb/overrides went upstream — it should be cached")

        # --- POST set: path + body allowlist --------------------------------
        HITS.clear()
        NEXT_POST[0] = (200, {"ok": True, "mbid": "m2", "name": "Y", "task_id": "t1"})
        status, _h, body = post("/lb/artist/override", {
            "nd_id": "nd1", "mbid": "m2", "name": "Y", "setAt": 5, "secret": "leak"})
        if status != 200 or json.loads(body).get("task_id") != "t1":
            failures.append(f"set override did not return lb-bot's 200 body: {status} {body!r}")
        sent = posts()
        if not sent or sent[0][1] != "/api/artist/override":
            failures.append(f"set override reached the wrong upstream path: {sent}")
        elif sent[0][2] != {"nd_id": "nd1", "mbid": "m2", "name": "Y"}:
            failures.append(
                f"set override body should be exactly nd_id/mbid/name, got {sent[0][2]}")

        # --- POST remove: path + body allowlist -----------------------------
        HITS.clear()
        NEXT_POST[0] = (200, {"ok": True, "task_id": ""})
        status, _h, _b = post("/lb/artist/override/remove",
                              {"nd_id": "nd1", "mbid": "m2", "name": "Y"})
        sent = posts()
        if status != 200:
            failures.append(f"remove override should answer 200, got {status}")
        if not sent or sent[0][1] != "/api/artist/override/remove":
            failures.append(f"remove override reached the wrong upstream path: {sent}")
        elif sent[0][2] != {"nd_id": "nd1"}:
            failures.append(f"remove override body should be exactly nd_id, got {sent[0][2]}")

        # --- a successful POST evicts the override map AND the discography ---
        for route, body_in in (("/lb/artist/override", {"nd_id": "nd1", "mbid": "m2", "name": "Y"}),
                               ("/lb/artist/override/remove", {"nd_id": "nd1"})):
            NEXT_POST[0] = (200, {"ok": True})
            REVISION[0] += 1
            get("/lb/overrides")
            get("/lb/artist/discography?id=nd1")   # warm both caches
            HITS.clear()
            get("/lb/overrides")
            get("/lb/artist/discography?id=nd1")
            if HITS:
                failures.append(f"{route}: the two reads were not cached before the POST: {HITS}")
            post(route, body_in)
            HITS.clear()
            REVISION[0] += 1
            get("/lb/overrides")
            _s, _h, disc = get("/lb/artist/discography?id=nd1")
            gets = [h[1].split("?")[0] for h in HITS if h[0] == "GET"]
            if "/api/overrides" not in gets:
                failures.append(f"{route} did not invalidate the cached GET /lb/overrides")
            if "/api/artist/discography" not in gets:
                failures.append(f"{route} did not invalidate the cached discography")
            if json.loads(disc).get("rev") != REVISION[0]:
                failures.append(f"{route}: discography read after the POST was stale")

        # --- a refused POST must NOT evict (nothing changed) ----------------
        NEXT_POST[0] = (409, {"error": "busy", "code": "busy"})
        get("/lb/overrides")
        HITS.clear()
        post("/lb/artist/override", {"nd_id": "nd1", "mbid": "m2", "name": "Y"})
        HITS.clear()
        get("/lb/overrides")
        if HITS:
            failures.append("a 409 refusal evicted the override cache — nothing was stored")

        # --- error bodies pass through with their status --------------------
        cases = [
            (400, {"error": "mbid is not a UUID"}, "/lb/artist/override"),
            (404, {"error": "no such artist", "code": "unknown_mbid"}, "/lb/artist/override"),
            (404, {"error": "none", "code": "no_override"}, "/lb/artist/override/remove"),
            (409, {"error": "busy", "code": "busy"}, "/lb/artist/override"),
            (502, {"error": "MusicBrainz unavailable", "retryable": True}, "/lb/artist/override"),
        ]
        for st, payload, route in cases:
            NEXT_POST[0] = (st, payload)
            status, _h, body = post(route, {"nd_id": "nd1", "mbid": "m2", "name": "Y"})
            if status != st or json.loads(body) != payload:
                failures.append(f"{route}: upstream {st} {payload} came back as {status} {body!r}")

        # --- an index push flushes the override map (its `toCheck` follows scans)
        # Driven through the real handler, against the module's own proxy, which
        # is the one the handler invalidates.
        async def index_push():
            raw = json.dumps({"seq": 7, "epoch": "1-test"}).encode()
            return await hub._handle_lb_index(
                FakeConn(raw), "/lb/index", {**auth, "Content-Length": str(len(raw))}, "POST")

        def hub_get(path: str):
            return asyncio.run(hub.LB.handle(None, path, dict(auth), "GET"))

        hub.LB._cache.clear()
        hub_get("/lb/overrides")
        HITS.clear()
        hub_get("/lb/overrides")
        if HITS:
            failures.append("index case: /lb/overrides was not cached before the push")
        res = asyncio.run(index_push())
        if res is None or res[0] != 200:
            failures.append(f"the index push was not accepted: {res and res[0]}")
        HITS.clear()
        hub_get("/lb/overrides")
        if not HITS:
            failures.append("an index push did not flush the cached /lb/overrides")
        if ("GET", "/lb/overrides") not in hub.LB_INDEX_INVALIDATES:
            failures.append("/lb/overrides is not in LB_INDEX_INVALIDATES")

        # --- an older lb-bot: 405 on the POSTs passes through, evicts nothing --
        hub.LB._cache.clear(); proxy._cache.clear()
        get("/lb/overrides")
        for route in ("/lb/artist/override", "/lb/artist/override/remove"):
            NEXT_POST[0] = (405, {"error": "method not allowed"})
            status, _h, body = post(route, {"nd_id": "nd1", "mbid": "m2", "name": "Y"})
            if status != 405 or json.loads(body) != {"error": "method not allowed"}:
                failures.append(f"{route}: upstream 405 came back as {status} {body!r}")
            HITS.clear()
            get("/lb/overrides")
            if HITS:
                failures.append(f"{route}: a 405 evicted the override cache")

        # --- a 404 on GET /lb/overrides (older lb-bot) is not cached ----------
        proxy._cache.clear()
        GET_STATUS[0] = 404
        try:
            status, _h, _b = get("/lb/overrides")
            HITS.clear()
            get("/lb/overrides")
            if status != 404:
                failures.append(f"/lb/overrides upstream 404 came back as {status}")
            if not HITS:
                failures.append("a 404 from /api/overrides was cached")
        finally:
            GET_STATUS[0] = 200

        # --- advertised, with the slow timeout on the writes ----------------
        adv = proxy.advertised_routes()
        for want in ("GET /lb/overrides", "POST /lb/artist/override",
                     "POST /lb/artist/override/remove"):
            if want not in adv:
                failures.append(f"{want!r} is not advertised")
        for key in (("POST", "/lb/artist/override"), ("POST", "/lb/artist/override/remove")):
            spec = hub.LB_ROUTES.get(key, {})
            if spec.get("timeout") != hub.PROXY_SLOW_TIMEOUT:
                failures.append(f"{key} is not on PROXY_SLOW_TIMEOUT")
            if spec.get("cache"):
                failures.append(f"{key} must not be cached")
    finally:
        hub.LBBOT_URL = original_url
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - lb override routes: reachable/body-allowlist/GET-cached/POST-evicts"
          "-map-and-discography/409-evicts-nothing/error-bodies-pass-through/advertised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
