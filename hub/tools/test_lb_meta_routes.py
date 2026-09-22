#!/usr/bin/env python3
"""
End-to-end test for the three lb-bot routes wired up with the editorial
metadata work: `/lb/meta/artist`, `/lb/meta/album` and `/lb/artist/lookup`.

Two of them (`/lb/album/similar` aside) are new, and the fourth —
`/lb/album/similar` — had been shipped and whitelisted for months with **zero
consumers**, so nothing had ever exercised it over the wire either. It is
covered here for the same reason.

Serves a stub lb-bot and drives the real `LbProxy.handle`, which is what proves
the things a unit test of the route table cannot:

  - the route is reachable at all, and reaches the right upstream path;
  - only whitelisted params are forwarded (an unlisted one is dropped, not
    passed through — the whitelist is the security control);
  - the shared result cache answers the second identical request without a
    second upstream hit;
  - an oversized answer is a clean 502 `tooLarge`, never a truncated 200.

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

HITS: list[str] = []
CAP = 4096


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        path = self.path.split("?")[0]
        if path == "/api/meta/artist":
            body = {"mbid": "m1", "summary": "A band.", "paragraphs": ["A band."],
                    "found": True, "links": [], "relations": {}, "credits": [],
                    "source": {"name": "Wikipedia", "license": "CC BY-SA 4.0",
                               "url": "https://en.wikipedia.org/wiki/X"}}
        elif path == "/api/meta/album":
            # Deliberately past the cap: a long Wikipedia article is exactly the
            # case lb-bot caps server-side, and the hub must refuse cleanly if
            # one ever gets through.
            body = {"rgid": "r1", "paragraphs": ["z" * (CAP + 1)]}
        elif path == "/api/artist/lookup":
            body = {"candidates": [{"mbid": "m1", "name": "X"}]}
        elif path == "/api/album/similar":
            body = {"albums": [{"rgid": "r1", "title": "T", "artist": "A"}],
                    "because": "Radiohead", "sources": ["ListenBrainz"]}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    original_url = hub.LBBOT_URL
    original_cap = hub.PROXY_MAX_RESPONSE
    hub.LBBOT_URL = f"http://127.0.0.1:{port}"
    hub.PROXY_MAX_RESPONSE = CAP
    proxy = hub.LbProxy()
    headers = {"Authorization": f"Bearer {hub.TOKEN}"}
    failures: list[str] = []

    def call(path: str):
        return asyncio.run(proxy.handle(None, path, headers, "GET"))

    try:
        # --- artist meta: reachable, forwards `mbid`, drops anything else ----
        HITS.clear()
        status, _hdrs, body = call("/lb/meta/artist?mbid=m1&secret=leak")
        if status != 200:
            failures.append(f"/lb/meta/artist should answer 200, got {status}")
        elif json.loads(body).get("summary") != "A band.":
            failures.append("/lb/meta/artist did not pass the upstream body through")
        if not HITS or not HITS[0].startswith("/api/meta/artist"):
            failures.append(f"/lb/meta/artist reached the wrong upstream path: {HITS}")
        if HITS and "secret" in HITS[0]:
            failures.append(
                "an un-whitelisted param was forwarded — the param list IS the "
                "control, and a passthrough defeats it")
        if HITS and "mbid=m1" not in HITS[0]:
            failures.append(f"`mbid` was not forwarded: {HITS[0]}")

        # --- and the cache answers the repeat without a second hit ----------
        before = len(HITS)
        call("/lb/meta/artist?mbid=m1&secret=leak")
        if len(HITS) != before:
            failures.append(
                "the second identical read went upstream — this route is cached "
                "for PROXY_CACHE_TTL_LONG precisely because the answer is an "
                "encyclopaedia article")

        # --- `name` is whitelisted too (Navidrome doesn't always carry an MBID)
        HITS.clear()
        call("/lb/meta/artist?name=Radiohead")
        if not HITS or "name=Radiohead" not in HITS[0]:
            failures.append(f"`name` was not forwarded: {HITS}")

        # --- album meta: oversized upstream body is a clean 502 -------------
        status, _hdrs, body = call("/lb/meta/album?rgid=r1")
        if status != 502:
            failures.append(
                f"an oversized /lb/meta/album must be 502, got {status} — a "
                "truncated 200 is the silent-corruption trap")
        elif json.loads(body).get("tooLarge") is not True:
            failures.append("the 502 must carry tooLarge so a client can say why")

        # --- artist lookup: the route D1 depends on ------------------------
        HITS.clear()
        status, _hdrs, body = call("/lb/artist/lookup?q=radio")
        if status != 200:
            failures.append(f"/lb/artist/lookup should answer 200, got {status}")
        elif not json.loads(body).get("candidates"):
            failures.append("/lb/artist/lookup returned no candidates")
        if not HITS or "q=radio" not in HITS[0]:
            failures.append(f"`q` was not forwarded: {HITS}")

        # --- similar albums: shipped long ago, never exercised over the wire -
        HITS.clear()
        status, _hdrs, body = call(
            "/lb/album/similar?artist_name=Radiohead&rgid=r1&limit=6")
        if status != 200:
            failures.append(f"/lb/album/similar should answer 200, got {status}")
        else:
            payload = json.loads(body)
            if payload.get("because") != "Radiohead":
                failures.append(
                    "`because` did not survive the proxy — it is the attribution "
                    "every row is rendered with, not an optional field")
    finally:
        hub.LBBOT_URL = original_url
        hub.PROXY_MAX_RESPONSE = original_cap
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - lb meta routes: reachable/param-whitelist/cached/oversized-is-502")
    return 0


if __name__ == "__main__":
    sys.exit(main())
