#!/usr/bin/env python3
"""
Regression test for the proxy's response-size ceiling.

`_proxy_upstream_blocking` used to end with `r.read(PROXY_MAX_RESPONSE)`, and
`read(n)` **truncates silently** — the status stays 200 and the body is a
half-finished JSON document. Every client then failed to parse it and reported
"couldn't reach the service", with nothing anywhere naming the real cause.

That was invisible on every proxied route until lb-bot's site-wide
fresh-releases feed grew past 4 MB and took the Fresh tab down in both clients
at once. A corrupt success is worse than a clean error, so an oversized body is
now a 502 that says so.

Serves real HTTP from a local server, with the cap shrunk so the test doesn't
have to move megabytes. Exits non-zero on failure.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

CAP = 1024


class Handler(http.server.BaseHTTPRequestHandler):
    """`/small` fits inside the cap; `/big` is deliberately one byte over it."""

    def do_GET(self):  # noqa: N802
        if self.path == "/small":
            body = json.dumps({"ok": True, "pad": "x" * 32}).encode()
        else:
            body = b"[" + b"x" * (CAP + 1) + b"]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    original = hub.PROXY_MAX_RESPONSE
    hub.PROXY_MAX_RESPONSE = CAP
    failures = []
    try:
        status, body, _ctype = hub._proxy_upstream_blocking(
            "GET", f"http://127.0.0.1:{port}/small", None, "", 5.0, "test")
        if status != 200:
            failures.append(f"under-cap body should pass through, got {status}")
        else:
            try:
                if json.loads(body).get("ok") is not True:
                    failures.append("under-cap body was altered")
            except ValueError as e:
                failures.append(f"under-cap body was not intact JSON: {e}")

        status, body, _ctype = hub._proxy_upstream_blocking(
            "GET", f"http://127.0.0.1:{port}/big", None, "", 5.0, "test")
        if status != 502:
            failures.append(
                f"oversized body must be 502, got {status} — a truncated 200 is "
                "the silent-corruption bug this test exists for")
        else:
            payload = json.loads(body)
            if payload.get("tooLarge") is not True:
                failures.append("502 must carry tooLarge so a client can say why")
            if str(CAP) not in payload.get("error", ""):
                failures.append("the error should name the limit it exceeded")
        # And it must not hand back the oversized bytes at all.
        if len(body) > CAP:
            failures.append("the refusal itself must be small")
    finally:
        hub.PROXY_MAX_RESPONSE = original
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - proxy limits: under-cap passthrough/oversized-is-502/refusal-names-limit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
