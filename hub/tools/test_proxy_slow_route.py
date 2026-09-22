#!/usr/bin/env python3
"""
A proxied request slower than the WebSocket *handshake* deadline must still answer.

The HTTP proxies answer from inside `process_request`, which runs during the
opening handshake — so the handshake deadline is also the proxy's real ceiling.
`websockets.serve` defaults `open_timeout` to **10 seconds**, which silently
capped every proxied route there and made `PROXY_SLOW_TIMEOUT = 45` unreachable.

The failure mode is the nasty kind: the connection is aborted before any
response is written, so the client sees a dropped socket rather than a status,
and both clients render that as "lb-bot is busy or unreachable" — a message that
sends you looking at lb-bot, which is healthy and about to answer. Measured on
the live stack: `/lb/album/sources` took 45s directly from lb-bot and answered
nothing at all through the hub.

This drives a real hub subprocess against a stub upstream that deliberately
sleeps past the old 10s default, and proves a status comes back. It is slow by
construction — that is the point of it.

Exits non-zero on failure.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TOKEN = "test-token"
# Comfortably past the 10s default this test exists to defend against, and
# comfortably under PROXY_SLOW_TIMEOUT so the *upstream* timeout is not what is
# being measured.
UPSTREAM_DELAY = 14.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(UPSTREAM_DELAY)
        raw = json.dumps({"sources": [], "artist": "A", "album": "B"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def main() -> int:
    upstream = http.server.HTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ,
           "HUB_TOKEN": TOKEN,
           "HUB_PORT": str(port),
           "HUB_HEALTH_PORT": str(_free_port()),
           "HUB_HOST": "127.0.0.1",
           "HUB_MIRROR_PLAYQUEUE": "false",
           "HUB_STATE": state,
           "LBBOT_URL": f"http://127.0.0.1:{upstream.server_address[1]}"}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures: list[str] = []
    try:
        time.sleep(1.5)  # let the hub bind

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/lb/album/sources?rgid=r1",
            headers={"Authorization": f"Bearer {TOKEN}"})
        started = time.monotonic()
        try:
            # Generous client ceiling: we are measuring the SERVER's behaviour,
            # not urllib's patience.
            with urllib.request.urlopen(req, timeout=UPSTREAM_DELAY + 30) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        except Exception as e:  # noqa: BLE001 — the regression lands here
            elapsed = time.monotonic() - started
            failures.append(
                f"no HTTP response at all after {elapsed:.1f}s ({type(e).__name__}: {e}). "
                "The handshake deadline aborted the connection before the proxy "
                "could answer — that is the 10s `open_timeout` default, and it "
                "makes PROXY_SLOW_TIMEOUT unreachable")
            status, body = None, b""

        elapsed = time.monotonic() - started
        if status is not None:
            if status != 200:
                failures.append(
                    f"slow route answered {status}, expected 200 — body {body[:200]!r}")
            elif elapsed < UPSTREAM_DELAY - 1:
                failures.append(
                    f"answered in {elapsed:.1f}s, faster than the stub's own "
                    f"{UPSTREAM_DELAY}s — the request cannot have reached upstream")
            elif json.loads(body).get("album") != "B":
                failures.append("the upstream body did not survive the proxy")
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
        upstream.shutdown()
        try:
            os.unlink(state)
        except OSError:
            pass

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - proxy slow route: a request past the handshake deadline still "
          "answers, and answers from upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
