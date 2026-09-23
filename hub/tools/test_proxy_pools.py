#!/usr/bin/env python3
"""
A status poll must never queue behind a source search, and identical reads in
flight must cost one upstream call.

The proxy used to have one first-come pool of four slots and no acquire timeout:
four `/lb/album/sources` fan-outs (up to 150 s each) held every slot, and every
`/lb/album/status` poll queued behind them until the handshake deadline dropped
the socket with no answer. This drives a real hub subprocess against a stub
upstream and proves:

1. with four slow `album/sources` calls in flight, `album/status` still answers
   in well under a second (the fast pool);
2. five concurrent identical `GET /lb/fills` reach the stub exactly once;
3. a request that cannot get a fast slot in time is answered `503 busy` rather
   than left hanging.

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
from concurrent.futures import ThreadPoolExecutor

TOKEN = "test-token"
SLOW = 6.0
QUEUE_TIMEOUT = 1.5

hits: dict[str, int] = {}
hits_lock = threading.Lock()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        with hits_lock:
            hits[path] = hits.get(path, 0) + 1
        if path in ("/api/album/sources", "/api/gaps/slow"):
            time.sleep(SLOW)
        if path == "/api/fills":
            time.sleep(0.4)  # long enough for five callers to pile up behind one
        raw = json.dumps({"path": path, "state": "downloading"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def get(port: int, path: str, timeout: float = 30):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.monotonic() - started
    except Exception as e:  # noqa: BLE001
        return None, repr(e).encode(), time.monotonic() - started


def main() -> int:
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_HEALTH_PORT": str(_free_port()), "HUB_HOST": "127.0.0.1",
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state,
           "PROXY_QUEUE_TIMEOUT_FAST": str(QUEUE_TIMEOUT),
           "LBBOT_URL": f"http://127.0.0.1:{upstream.server_address[1]}"}
    hub = subprocess.Popen([sys.executable, "hub.py"], env=env,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
    failures: list[str] = []
    pool = ThreadPoolExecutor(max_workers=12)
    try:
        time.sleep(1.5)

        # 1. four searches hold the default pool; a status poll answers anyway
        searches = [pool.submit(get, port, f"/lb/album/sources?rgid=r{i}") for i in range(4)]
        time.sleep(0.5)
        status, body, elapsed = get(port, "/lb/album/status?release_mbid=m1")
        if status != 200:
            failures.append(f"status poll behind four searches answered {status}: {body[:120]!r}")
        elif elapsed > 2.0:
            failures.append(f"status poll took {elapsed:.1f}s behind four searches — it queued")
        for f in searches:
            s, b, _e = f.result()
            if s != 200:
                failures.append(f"a search answered {s}: {b[:120]!r}")

        # 2. identical in-flight reads coalesce into one upstream call
        with hits_lock:
            hits.pop("/api/fills", None)
        fills = [pool.submit(get, port, "/lb/fills?release_mbids=a,b") for _ in range(5)]
        results = [f.result() for f in fills]
        if any(s != 200 for s, _b, _e in results):
            failures.append(f"coalesced reads did not all answer 200: {[s for s, _b, _e in results]}")
        with hits_lock:
            n = hits.get("/api/fills", 0)
        if n != 1:
            failures.append(f"five identical concurrent reads reached upstream {n} times, expected 1")

        # 3. the fast pool exhausted past its queue timeout answers 503 busy
        slow_gaps = [pool.submit(get, port, f"/lb/gap?group_id=slow&sourcePage={i}") for i in range(2)]
        time.sleep(0.5)
        status, body, elapsed = get(port, "/lb/album/status?release_mbid=m2")
        if status != 503:
            failures.append(f"a poll with no fast slot answered {status} after {elapsed:.1f}s, expected 503")
        else:
            try:
                if not json.loads(body).get("busy"):
                    failures.append(f"503 body carries no busy flag: {body[:120]!r}")
            except Exception:  # noqa: BLE001
                failures.append(f"503 body is not JSON: {body[:120]!r}")
            if elapsed > QUEUE_TIMEOUT + 1.5:
                failures.append(f"the busy answer took {elapsed:.1f}s, expected ~{QUEUE_TIMEOUT}s")
        for f in slow_gaps:
            f.result()
    finally:
        pool.shutdown(wait=False)
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
    print("PASS - proxy pools: poll-answers-behind-searches/identical-reads-coalesce/"
          "no-slot-is-503-busy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
