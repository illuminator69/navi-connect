#!/usr/bin/env python3
"""
Worbus P4, lane H (H3): `server/healthcheck.py`, against two stub HTTP servers.

  both_answer_healthy        hub health port and lb-bot /api/health both 200 -> exit 0
  lbbot_down_unhealthy       lb-bot's port closed -> exit 1, the line names lb-bot
  hub_down_unhealthy         the hub's health port closed -> exit 1, names the hub
  lbbot_503_unhealthy        lb-bot answers 503 -> exit 1
  slow_answer_times_out      an answer slower than 2 s -> exit 1 within about 2 s
  health_port_follows_hub    HUB_HEALTH_PORT unset -> HUB_PORT + 1

Exits non-zero on failure.
"""
from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "healthcheck.py"
BEHAVIOUR = {"status": 200, "delay": 0.0}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.server.server_port == LB[0] and self.path == "/api/health":
            time.sleep(BEHAVIOUR["delay"])
            status = BEHAVIOUR["status"]
        elif self.server.server_port == HUB[0] and self.path == "/":
            status = 200
        else:
            status = 404
        body = b'{"ok": true}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


HUB: list[int] = [0]
LB: list[int] = [0]


def serve() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(hub_health: int | None, lb: int, hub_port: int | None = None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUB_PORT", "HUB_HEALTH_PORT", "LB_BOT_WEB_PORT")}
    env["LB_BOT_WEB_PORT"] = str(lb)
    if hub_health is not None:
        env["HUB_HEALTH_PORT"] = str(hub_health)
    if hub_port is not None:
        env["HUB_PORT"] = str(hub_port)
    started = time.monotonic()
    p = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True,
                       text=True, timeout=20)
    return p.returncode, p.stdout.strip(), time.monotonic() - started


def main() -> int:
    hub, lb = serve(), serve()
    HUB[0], LB[0] = hub.server_port, lb.server_port
    f: list[str] = []
    names = []
    try:
        names.append("both_answer_healthy")
        rc, out, _ = check(HUB[0], LB[0])
        if rc != 0:
            f.append(f"both up: rc {rc} ({out})")
        names.append("lbbot_down_unhealthy")
        rc, out, _ = check(HUB[0], closed_port())
        if rc != 1 or "lb-bot" not in out:
            f.append(f"lb-bot down: rc {rc} ({out})")
        names.append("hub_down_unhealthy")
        rc, out, _ = check(closed_port(), LB[0])
        if rc != 1 or "hub" not in out:
            f.append(f"hub down: rc {rc} ({out})")
        names.append("lbbot_503_unhealthy")
        BEHAVIOUR["status"] = 503
        rc, out, _ = check(HUB[0], LB[0])
        if rc != 1:
            f.append(f"lb-bot 503: rc {rc} ({out})")
        BEHAVIOUR["status"] = 200
        names.append("slow_answer_times_out")
        BEHAVIOUR["delay"] = 4.0
        rc, out, took = check(HUB[0], LB[0])
        if rc != 1 or took > 3.8:
            f.append(f"slow lb-bot: rc {rc} after {took:.1f} s ({out})")
        BEHAVIOUR["delay"] = 0.0
        names.append("health_port_follows_hub")
        rc, out, _ = check(None, LB[0], hub_port=HUB[0] - 1)
        if rc != 0:
            f.append(f"HUB_PORT+1: rc {rc} ({out})")
    finally:
        hub.shutdown()
        lb.shutdown()
    for x in f:
        print(f"FAIL - {x}")
    if f:
        return 1
    print("PASS - worbus healthcheck: " + "/".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
