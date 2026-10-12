#!/usr/bin/env python3
"""
I-006: the listening-analytics routes — `GET /lb/listening/{status,summary,
unowned,recent}` and `POST /lb/listening/sync` — plus the Fresh rider.

Serves a stub lb-bot and drives the real `LbProxy.handle`, proving:

  - each route reaches the right upstream path;
  - `period`, `tz`, `kind`, `before`, `limit` each survive and an unlisted
    param is dropped (a forgotten param is silently lost, so every one is
    asserted by name); a `tz` with a `/` (`Europe/Berlin`) arrives intact;
  - none of the four reads is cached (the mirror fills in the background, so a
    hub copy could serve a mid-import body), summary and unowned also do not
    coalesce (two CONCURRENT identical reads each reach upstream; joining an
    in-flight read is the same stale answer, shared), and two zones each arrive
    intact; a 503 from an older/unready lb-bot passes through with its body;
  - `sync` forwards no body and has nothing to invalidate;
  - `status` rides the fast pool;
  - all five are advertised;
  - `/lb/fresh-releases` rows reach the client untouched, so the `artistPlays`
    field lb-bot adds is not lost in transit.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

HITS: list[tuple[str, str, dict]] = []   # (method, path, query or body)
SLOW = [0.0]    # seconds the summary / unowned stubs hold their answer
POST_STATUS = [200]  # what the sync POST stub answers
STATUS = [200]  # what the listening GET stubs answer (an old lb-bot: 503/404)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        q = dict(urllib.parse.parse_qsl(query))
        HITS.append(("GET", path, q))
        if path.startswith("/api/listening/") and STATUS[0] != 200:
            self._send(STATUS[0], {"error": "unavailable", "state": "x"})
        elif path.startswith("/api/listening/") or path == "/api/fresh-releases":
            if path in ("/api/listening/summary", "/api/listening/unowned"):
                time.sleep(SLOW[0])
            if path == "/api/fresh-releases":
                self._send(200, {"releases": [
                    {"rgid": "r1", "artistPlays": 42, "artist": "A"},
                    {"rgid": "r2", "artistPlays": 0, "artist": "B"}]})
            else:
                self._send(200, {"ok": True})
        else:
            self._send(404, {})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        HITS.append(("POST", self.path, json.loads(raw or b"{}")))
        if POST_STATUS[0] != 200:
            self._send(POST_STATUS[0], {"error": "method not allowed"})
            return
        self._send(200, {"ok": True, "state": "importing"})

    def log_message(self, *_args):
        pass


class FakeConn:
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

    def upstream_for(path: str):
        HITS.clear()
        status, _h, body = get(path)
        return status, body, (HITS[0] if HITS else None)

    try:
        # --- param allowlists: every listed param survives, the rest is dropped
        cases = [
            ("/lb/listening/status", "", {}),
            ("/lb/listening/summary", "period=30d&tz=Europe/Berlin",
             {"period": "30d", "tz": "Europe/Berlin"}),
            ("/lb/listening/unowned", "period=90d&kind=artist&limit=25",
             {"period": "90d", "kind": "artist", "limit": "25"}),
            ("/lb/listening/recent", "before=1700000000&limit=50",
             {"before": "1700000000", "limit": "50"}),
        ]
        for route, query, want in cases:
            HITS.clear()
            proxy._cache.clear()
            sep = "&" if query else ""
            status, _h, _b = get(f"{route}?{query}{sep}secret=leak&days=7")
            if status != 200:
                failures.append(f"{route} should answer 200, got {status}")
                continue
            if not HITS or HITS[0][1] != route.replace("/lb/", "/api/", 1):
                failures.append(f"{route} reached the wrong upstream path: {HITS}")
                continue
            if HITS[0][2] != want:
                failures.append(f"{route}: upstream query should be exactly {want}, got {HITS[0][2]}")

        # tz with a slash arrives intact whether sent raw or percent-encoded.
        for sent in ("tz=Europe/Berlin", "tz=America%2FNew_York"):
            HITS.clear(); proxy._cache.clear()
            get(f"/lb/listening/summary?period=7d&{sent}")
            want = urllib.parse.unquote(sent.split("=")[1])
            if not HITS or HITS[0][2].get("tz") != want:
                failures.append(f"tz {sent!r} did not arrive as {want!r}: {HITS}")

        # --- none of the four reads is cached: a repeat reaches upstream -------
        # The mirror fills in the background, so a hub copy could hand a client the
        # body cached mid-import. Counted by upstream hits.
        for route, query in (
                ("/lb/listening/summary", "period=7d&tz=UTC"),
                ("/lb/listening/unowned", "period=7d&kind=album&limit=30"),
                ("/lb/listening/status", ""),
                ("/lb/listening/recent", "limit=50")):
            proxy._cache.clear()
            HITS.clear()
            get(f"{route}?{query}")
            get(f"{route}?{query}")
            if len(HITS) != 2:
                failures.append(f"two identical {route} reads should both reach "
                                f"upstream, got {len(HITS)} hits")

        # --- two zones each arrive intact ---------------------------------------
        HITS.clear()
        get("/lb/listening/summary?period=7d&tz=Europe/Berlin")
        get("/lb/listening/summary?period=7d&tz=Asia/Tokyo")
        if [h[2].get("tz") for h in HITS] != ["Europe/Berlin", "Asia/Tokyo"]:
            failures.append(f"each tz should arrive intact upstream: {HITS}")

        # --- summary / unowned never join an in-flight read --------------------
        for route in (("GET", "/lb/listening/summary"), ("GET", "/lb/listening/unowned")):
            if hub.LB_ROUTES[route].get("coalesce") is not False:
                failures.append(f"{route} must carry coalesce: False")
        for route, q in (("/lb/listening/summary", "period=7d&tz=UTC"),
                         ("/lb/listening/unowned", "period=7d")):
            SLOW[0] = 0.4
            HITS.clear()

            async def two():
                return await asyncio.gather(
                    proxy.handle(None, f"{route}?{q}", dict(auth), "GET"),
                    proxy.handle(None, f"{route}?{q}", dict(auth), "GET"))
            try:
                asyncio.run(two())
            finally:
                SLOW[0] = 0.0
            if len(HITS) != 2:
                failures.append(f"two concurrent {route} reads should each reach upstream, "
                                f"got {len(HITS)} hits")

        # --- an unready/older lb-bot: a 503 passes through with its body --------
        STATUS[0] = 503
        try:
            for route in ("summary", "unowned", "status", "recent"):
                status, _h, body = get(f"/lb/listening/{route}")
                if status != 503 or json.loads(body).get("error") != "unavailable":
                    failures.append(f"listening/{route}: upstream 503 came back as {status} {body!r}")
        finally:
            STATUS[0] = 200

        # --- sync: forwards no body --------------------------------------------
        HITS.clear()
        status, _h, body = post("/lb/listening/sync", {"force": True, "secret": "leak"})
        if status != 200 or json.loads(body).get("state") != "importing":
            failures.append(f"sync did not return lb-bot's body: {status} {body!r}")
        if HITS != [("POST", "/api/listening/sync", {})]:
            failures.append(f"sync should reach /api/listening/sync with an empty body: {HITS}")
        # an older lb-bot answers the POST 405: it passes through
        POST_STATUS[0] = 405
        try:
            status, _h, body = post("/lb/listening/sync", {})
        finally:
            POST_STATUS[0] = 200
        if status != 405 or json.loads(body).get("error") != "method not allowed":
            failures.append(f"sync: upstream 405 came back as {status} {body!r}")
        if hub.LB_ROUTES[("POST", "/lb/listening/sync")].get("invalidates"):
            failures.append("sync has nothing cached to invalidate any more")
        for key in (("GET", "/lb/listening/summary"), ("GET", "/lb/listening/unowned")):
            if key in hub.LB_LIBRARY_ROUTES:
                failures.append(f"{key} is uncached; it must not sit in LB_LIBRARY_ROUTES")

        # --- status rides the fast pool ----------------------------------------
        if hub.LB_ROUTES.get(("GET", "/lb/listening/status"), {}).get("pool") != "fast":
            failures.append("/lb/listening/status is not on the fast pool")
        for key in (("GET", "/lb/listening/summary"), ("GET", "/lb/listening/unowned"),
                    ("GET", "/lb/listening/recent"), ("POST", "/lb/listening/sync")):
            if hub.LB_ROUTES.get(key, {}).get("pool", "default") != "default":
                failures.append(f"{key} should use the default pool")

        # --- advertised --------------------------------------------------------
        adv = proxy.advertised_routes()
        for want in ("GET /lb/listening/status", "GET /lb/listening/summary",
                     "GET /lb/listening/unowned", "GET /lb/listening/recent",
                     "POST /lb/listening/sync"):
            if want not in adv:
                failures.append(f"{want!r} is not advertised")

        # --- Fresh rider: artistPlays survives the proxy -----------------------
        proxy._cache.clear()
        _s, _h, body = get("/lb/fresh-releases?days=7")
        rows = json.loads(body).get("releases") or []
        if [r.get("artistPlays") for r in rows] != [42, 0]:
            failures.append(f"artistPlays did not survive /lb/fresh-releases: {rows}")
    finally:
        hub.LBBOT_URL = original_url
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - lb listening routes: param-allowlist/tz-with-slash/all-four-reads-uncached"
          "/no-coalesce/503-passes/sync-sends-no-body/status-fast-pool/advertised"
          "/fresh-artistPlays-survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
