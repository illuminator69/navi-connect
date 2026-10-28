#!/usr/bin/env python3
"""
Worbus P1, H1: the hub calls lb-bot with `Authorization: Bearer <HUB_TOKEN>`.

lb-bot's web API sits behind a login now (its S5), and the hub is its trusted
service caller: the same secret lb-bot already sends the hub as
`LB_BOT_HUB_TOKEN` on its `/lb/index`, `/lb/fill` and `/lb/notify` pushes. Before
this, `LbProxy.upstream_token` was the base class's `""` and the hub sent no
`Authorization` at all, so every `/lb/*` call to a locked lb-bot would be a 401
while `/lb/status` still read it as up.

A real hub subprocess against a stub lb-bot that records the header it gets:

- `probe_and_routes_send_bearer`: the stub refuses anything without the bearer
  (401 `signin_required`, as lb-bot does). The hub's own background probe, a GET
  route and a POST route all carry `Bearer <HUB_TOKEN>`, and all three answer.
- `old_lbbot_ignores_it`: the stub requires nothing (an lb-bot from before the
  login). The same calls answer exactly what the stub says, as they did before.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
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

import websockets

TOKEN = "test-token-h1"
HUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

SEEN: list[tuple[str, str, str]] = []      # (method, path, Authorization)
SEEN_LOCK = threading.Lock()
MODE = {"strict": True}                     # strict = a locked lb-bot (S5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLbBot(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _answer(self, method: str) -> None:
        path = self.path.partition("?")[0]
        auth = self.headers.get("Authorization") or ""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
        with SEEN_LOCK:
            SEEN.append((method, path, auth))
        if MODE["strict"] and auth != f"Bearer {TOKEN}":
            self._send(401, {"code": "signin_required"})
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "epoch": "1-a", "headSeq": 3})
        elif path == "/api/wishlist" and method == "GET":
            self._send(200, {"wishlist": [{"rgid": "r1"}], "mode": MODE["strict"]})
        elif path == "/api/wishlist" and method == "POST":
            self._send(200, {"ok": True, "mode": MODE["strict"]})
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        self._answer("GET")

    def do_POST(self):  # noqa: N802
        self._answer("POST")

    def log_message(self, *_args):
        pass


def request(port: int, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def seen(method: str, path: str) -> list[str]:
    with SEEN_LOCK:
        return [a for m, p, a in SEEN if m == method and p == path]


async def welcome_lb(port: int) -> dict:
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"t": "hello", "token": TOKEN,
                                  "device": {"id": "h1", "name": "h1", "platform": "test",
                                             "caps": ["controller"]}}))
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    return first.get("lb") or {}


def case_probe_and_routes_send_bearer(port: int) -> list[str]:
    failures: list[str] = []
    want = f"Bearer {TOKEN}"
    probes = seen("GET", "/api/health")
    if not probes:
        failures.append("probe_and_routes_send_bearer: the hub's own probe never reached lb-bot")
    elif any(a != want for a in probes):
        failures.append(f"probe_and_routes_send_bearer: the background probe sent "
                        f"Authorization {probes!r}, expected {want!r}")
    lb = asyncio.run(welcome_lb(port))
    if lb.get("available") is not True:
        failures.append(f"probe_and_routes_send_bearer: a locked lb-bot reads as "
                        f"available={lb.get('available')!r} — the probe was refused")
    s, b = request(port, "/lb/wishlist")
    if s != 200 or b.get("wishlist") != [{"rgid": "r1"}]:
        failures.append(f"probe_and_routes_send_bearer: GET /lb/wishlist = {s} {b!r}")
    if seen("GET", "/api/wishlist") != [want]:
        failures.append(f"probe_and_routes_send_bearer: GET route sent "
                        f"{seen('GET', '/api/wishlist')!r}, expected [{want!r}]")
    s, b = request(port, "/lb/wishlist", "POST", {"rgid": "r2", "artist": "A", "album": "B"})
    if s != 200 or b.get("ok") is not True:
        failures.append(f"probe_and_routes_send_bearer: POST /lb/wishlist = {s} {b!r}")
    if seen("POST", "/api/wishlist") != [want]:
        failures.append(f"probe_and_routes_send_bearer: POST route sent "
                        f"{seen('POST', '/api/wishlist')!r}, expected [{want!r}]")
    s, b = request(port, "/lb/status")
    if s != 200 or b.get("upstreamReachable") is not True:
        failures.append(f"probe_and_routes_send_bearer: /lb/status against a locked "
                        f"lb-bot = {s} upstreamReachable={b.get('upstreamReachable')!r}")
    return failures


def case_old_lbbot_ignores_it(port: int) -> list[str]:
    failures: list[str] = []
    MODE["strict"] = False
    with SEEN_LOCK:
        SEEN.clear()
    # The POST first: its success drops the cached strict-mode GET, so the
    # read after it is the stub's own answer in this mode.
    s2, b2 = request(port, "/lb/wishlist", "POST", {"rgid": "r3"})
    s, b = request(port, "/lb/wishlist")
    if (s2, b2) != (200, {"ok": True, "mode": False}):
        failures.append(f"old_lbbot_ignores_it: POST /lb/wishlist = {s2} {b2!r}, "
                        "expected the stub's own answer")
    if (s, b) != (200, {"wishlist": [{"rgid": "r1"}], "mode": False}):
        failures.append(f"old_lbbot_ignores_it: GET /lb/wishlist = {s} {b!r}, "
                        "expected the stub's own answer")
    s3, b3 = request(port, "/lb/status")
    if s3 != 200 or b3.get("upstreamReachable") is not True:
        failures.append(f"old_lbbot_ignores_it: /lb/status = {s3} "
                        f"upstreamReachable={b3.get('upstreamReachable')!r}")
    lb = asyncio.run(welcome_lb(port))
    if lb.get("available") is not True:
        failures.append(f"old_lbbot_ignores_it: an lb-bot that ignores the header reads "
                        f"available={lb.get('available')!r}")
    return failures


def main() -> int:
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLbBot)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    lb_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    port = _free_port()
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_HEALTH_PORT": str(_free_port()), "HUB_HOST": "127.0.0.1",
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "LBBOT_URL": lb_url}
    proc = subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures: list[str] = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not seen("GET", "/api/health"):
            time.sleep(0.1)
        time.sleep(0.3)
        failures += case_probe_and_routes_send_bearer(port)
        failures += case_old_lbbot_ignores_it(port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()
        for p in (state, state + ".bak"):
            try:
                os.unlink(p)
            except OSError:
                pass
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb upstream auth: probe_and_routes_send_bearer/old_lbbot_ignores_it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
