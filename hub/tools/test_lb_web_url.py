#!/usr/bin/env python3
"""
Worbus P4, lane H (H1): lb-bot's page link (`webUrl`) and the failed-imports count.

Inside the Worbus Server container the hub reaches lb-bot at http://127.0.0.1:8899,
which opens nothing on a phone. So a loopback upstream is never offered as the link:
with LBBOT_WEB_URL unset the hub sends an empty `webUrl` and each client builds the
link itself from the server address it is using (design §6.4).

  loopback_upstream_not_offered   LBBOT_URL on 127.0.0.1 / localhost / [::1], no
                                  LBBOT_WEB_URL -> `web_url() == ""`, and "" in
                                  `/lb/status` (the probe answer) and `welcome.lb`;
                                  a non-loopback upstream still falls back as before
  web_url_env_wins                LBBOT_WEB_URL set -> that value, whatever the upstream
  attention_passes_imports        a stand-in lb-bot answering `/api/attention` with
                                  `counts.imports: 3` -> a client reads
                                  `counts.imports == 3` through `/lb/attention`

Real hub subprocesses (test_hub_info's HubProc) against the fake Navidrome and a stub
lb-bot on loopback. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _k in [k for k in os.environ if k.startswith("WORBUS_")]:
    os.environ.pop(_k)

from test_hub_info import TOKEN, USERS, HubProc  # noqa: E402
from fake_navidrome import FakeNavidrome  # noqa: E402

ATTENTION = {
    "version": 1, "serverTime": 1761000000.0,
    "counts": {"failedGaps": 1, "failedRequests": 0, "folders": 2,
               "foldersUnidentified": 1, "picks": 0, "imports": 3},
    "albums": [], "folders": [], "truncated": {"albums": 0, "folders": 0},
}


class StubLb(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/health":
            body = {"ok": True, "downloads": "off"}
        elif path == "/api/attention":
            body = ATTENTION
        else:
            body = {"ok": True}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a):
        pass


def start_stub() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubLb)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def bearer() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


async def links_of(nd: FakeNavidrome, **env: str) -> tuple[str | None, str | None]:
    """(welcome.lb.webUrl, /lb/status webUrl) from a hub started with `env`."""
    h = HubProc(nd.url, **env)
    try:
        await h.up()
        w = await h.welcome(token=TOKEN)
        status, _hd, raw = h.raw("GET", "/lb/status", headers=bearer())
        status_web = json.loads(raw).get("webUrl") if status == 200 else f"<{status}>"
        return (w.get("lb") or {}).get("webUrl"), status_web
    finally:
        h.cleanup()


def unit_web_url() -> list[str]:
    """`LbProxy.web_url` decided in process, every host form, no hub started."""
    f: list[str] = []
    os.environ["HUB_TOKEN"] = TOKEN
    sys.path.insert(0, os.path.dirname(HERE))
    import hub
    cases = [
        # (LBBOT_URL, LBBOT_WEB_URL, expected)
        ("http://127.0.0.1:8899", "", ""),
        ("http://127.0.0.1:8899/", "", ""),
        ("http://127.5.6.7:8899", "", ""),
        ("http://localhost:8899", "", ""),
        ("http://LOCALHOST:8899", "", ""),
        ("http://[::1]:8899", "", ""),
        ("https://localhost", "", ""),
        ("http://lb-bot:8899", "", "http://lb-bot:8899"),
        ("http://192.168.1.20:8899", "", "http://192.168.1.20:8899"),
        ("http://localhost.example.org:8899", "", "http://localhost.example.org:8899"),
        ("http://127.0.0.1:8899", "https://lb.example.org", "https://lb.example.org"),
        ("http://lb-bot:8899", "http://10.0.0.5:8899", "http://10.0.0.5:8899"),
        # a loopback LBBOT_WEB_URL is the person's own choice (an ssh tunnel): kept
        ("http://lb-bot:8899", "http://127.0.0.1:9999", "http://127.0.0.1:9999"),
        ("", "https://lb.example.org", ""),   # proxy off: no link, as before
    ]
    for upstream, web, want in cases:
        hub.LBBOT_URL = upstream.rstrip("/")
        hub.LBBOT_WEB_URL = web.rstrip("/")
        got = hub.LbProxy().web_url()
        if got != want:
            f.append(f"LBBOT_URL={upstream!r} LBBOT_WEB_URL={web!r}: web_url()={got!r}, "
                     f"expected {want!r}")
    return f


async def t_loopback_upstream_not_offered(nd: FakeNavidrome, lb_port: int) -> list[str]:
    f: list[str] = []
    for host in ("127.0.0.1", "localhost", "[::1]"):
        welcome_web, status_web = await links_of(nd, LBBOT_URL=f"http://{host}:{lb_port}")
        if welcome_web != "":
            f.append(f"upstream {host}: welcome.lb.webUrl = {welcome_web!r}, expected \"\"")
        if status_web != "":
            f.append(f"upstream {host}: /lb/status webUrl = {status_web!r}, expected \"\"")
    # A non-loopback upstream (the two-container shape, lb-bot by container name)
    # still falls back as today.
    welcome_web, status_web = await links_of(nd, LBBOT_URL="http://lb.invalid:8899")
    if welcome_web != "http://lb.invalid:8899" or status_web != "http://lb.invalid:8899":
        f.append(f"non-loopback upstream: welcome {welcome_web!r}, /lb/status {status_web!r}, "
                 "expected the upstream http://lb.invalid:8899")
    return f


async def t_web_url_env_wins(nd: FakeNavidrome, lb_port: int) -> list[str]:
    f: list[str] = []
    for upstream in (f"http://127.0.0.1:{lb_port}", "http://lb.invalid:8899"):
        welcome_web, status_web = await links_of(
            nd, LBBOT_URL=upstream, LBBOT_WEB_URL="https://lb.example.org/")
        if welcome_web != "https://lb.example.org" or status_web != "https://lb.example.org":
            f.append(f"upstream {upstream}: welcome {welcome_web!r}, /lb/status "
                     f"{status_web!r}, expected https://lb.example.org")
    return f


async def t_attention_passes_imports(nd: FakeNavidrome, lb_port: int) -> list[str]:
    f: list[str] = []
    h = HubProc(nd.url, LBBOT_URL=f"http://127.0.0.1:{lb_port}")
    try:
        await h.up()
        status, _hd, raw = h.raw("GET", "/lb/attention", headers=bearer())
        if status != 200:
            return [f"/lb/attention answered {status}: {raw[:200]!r}"]
        counts = json.loads(raw).get("counts") or {}
        if counts.get("imports") != 3:
            f.append(f"counts.imports = {counts.get('imports')!r}, expected 3 "
                     f"(counts {counts!r})")
        if counts.get("failedGaps") != 1:
            f.append(f"counts.failedGaps = {counts.get('failedGaps')!r}: the other keys moved")
    finally:
        h.cleanup()
    return f


async def main_async() -> tuple[list[str], list[str]]:
    nd = FakeNavidrome(USERS).start()
    stub = start_stub()
    lb_port = stub.server_address[1]
    failures: list[str] = []
    names: list[str] = []

    def record(name: str, got: list[str]) -> None:
        names.append(name)
        failures.extend(f"{name}: {x}" for x in got)

    try:
        for name, fn in (("loopback_upstream_not_offered", t_loopback_upstream_not_offered),
                         ("web_url_env_wins", t_web_url_env_wins),
                         ("attention_passes_imports", t_attention_passes_imports)):
            try:
                got = await fn(nd, lb_port)
            except Exception:  # noqa: BLE001
                got = [f"raised:\n{traceback.format_exc()}"]
            record(name, got)
        try:
            got = unit_web_url()
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        record("web_url_host_forms", got)
    finally:
        stub.shutdown()
        nd.stop()
    return failures, names


def main() -> int:
    failures, names = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - lb web url: " + "/".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
