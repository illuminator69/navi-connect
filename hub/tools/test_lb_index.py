#!/usr/bin/env python3
"""
The index mirror's hub half: two sync routes, the `/lb/index` push, `lbIndex`
and `lb` in `welcome`, and the hub's own lb-bot probe.

Both clients keep a local copy of lb-bot's library index and pull changes by
sequence number. The hub's part is small, and every piece of it is a place the
mirror can go quietly stale:

1. **The sync routes are neither cached nor coalesced.** A cached page, or a
   caller joining a request that started before a write, answers with a head
   older than the `index` frame that prompted the pull — the client then sits
   one push behind until the next trigger. Their params are whitelisted like
   every other route's.
2. **They run on their own one-slot `sync` pool.** Not the default pool, where a
   pull would queue behind 150 s source searches; not `fast`, where a pull from
   zero would starve the status polls. Proven in both directions, and a second
   pull that cannot get the slot is `503 busy`, like the fast pool.
3. **`POST /lb/index` is validated, fanned out as `t: "index"` and is not a
   library event.** It drops the discography cache and nothing in
   `LB_LIBRARY_ROUTES` besides — a bulk build pushes every 2 s, and flushing the
   library routes that often is precisely what the separate frame exists to
   avoid.
4. **`welcome` carries `lb` and `lbIndex`, and `lbIndex` survives a restart.**
   `lb.routes` is the same list `/lb/status` advertises.
5. **The probe.** The hub probes lb-bot's `/api/health` itself; an lb-bot older
   than that route answers 404 and the probe falls back to `/api/summary`
   (ruling R5), so a hub deployed first does not call lb-bot down. An
   authenticated inbound push marks lb-bot available at once.
6. **A verdict that CHANGES is broadcast as `t: "lb"`** (ruling R18), exactly
   once per flip and never for an unchanged verdict; the probe's own health call
   has a short deadline, and a first-welcome wait that expired is not paid again.
   With lb-bot unconfigured, `welcome.lb.routes` and `/lb/status` agree (both
   empty).
7. **lb-bot's downloads mode** (Worbus P1). `welcome.lb`, the `lb` frame and
   `/lb/status` carry `downloads` exactly when lb-bot's last 200 health answer
   said "on" or "off"; a change of it alone is broadcast once; a health answer
   without it (an older lb-bot) leaves the key out, and any other value is never
   relayed. An unreachable lb-bot keeps the mode it last reported.
8. **lb-bot's index-build progress** (Worbus P1). A push's `progress`
   (`{unindexed, total, current}`) is kept in the hint, so it reaches
   `welcome.lbIndex`, the `index` frame and state.json (and survives a restart);
   a push that only moves `progress` is still stored and broadcast. A malformed
   `progress` is dropped and the push accepted; a push without one is exactly
   what it was before (and leaves the hint without one).

Real hub subprocesses against a stub lb-bot, plus in-process checks of the
verdict machinery with the upstream call stubbed. Exits non-zero on failure.
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
from concurrent.futures import ThreadPoolExecutor

import websockets

TOKEN = "test-token"
os.environ.setdefault("HUB_TOKEN", TOKEN)  # before any in-process `import hub`
SLOW = 4.0
QUEUE_TIMEOUT = 1.5
HUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, HUB_DIR)

hits: dict[str, int] = {}
queries: dict[str, list[str]] = {}
hits_lock = threading.Lock()
mode = {"health": 200, "downloads": None}   # what the stub's /api/health answers
counter = {"n": 0}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        with hits_lock:
            hits[path] = hits.get(path, 0) + 1
            queries.setdefault(path, []).append(query)
            counter["n"] += 1
            n = counter["n"]
        if path == "/api/health" and mode["health"] != 200:
            self._send(mode["health"], {"error": "not found"})
            return
        if path in ("/api/album/sources", "/api/gaps/slow") or "since=slow" in query:
            time.sleep(SLOW)
        elif path == "/api/index/changes":
            time.sleep(0.3)  # long enough for concurrent callers to overlap
        body = {"path": path, "n": n, "ok": True, "epoch": "1-abc", "headSeq": 7}
        if path == "/api/health" and mode["downloads"] is not None:
            body["downloads"] = mode["downloads"]
        self._send(200, body)

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def request(port: int, path: str, method: str = "GET", body=None,
            token: str = TOKEN, timeout: float = 30, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.monotonic() - started
    except Exception as e:  # noqa: BLE001
        return None, repr(e).encode(), time.monotonic() - started


def spawn(port: int, state: str, lbbot_url: str, **extra: str):
    env = {**_inherited_env(), "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_HEALTH_PORT": str(_free_port()), "HUB_HOST": "127.0.0.1",
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state,
           "PROXY_QUEUE_TIMEOUT_FAST": str(QUEUE_TIMEOUT), "LBBOT_URL": lbbot_url,
           **extra}
    return subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR)


def stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def connect(port: int, device_id: str):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.send(json.dumps({"t": "hello", "token": TOKEN,
                              "device": {"id": device_id, "name": device_id,
                                         "platform": "test", "caps": ["controller"]}}))
    first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    return ws, first


async def welcome_of(port: int, device_id: str) -> dict:
    ws, first = await connect(port, device_id)
    await ws.close()
    return first


async def drain(ws, seconds: float) -> list[dict]:
    frames: list[dict] = []
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return frames
        try:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=left)))
        except asyncio.TimeoutError:
            return frames


def static_checks() -> list[str]:
    """Route-table facts that need no running hub."""
    import hub
    failures: list[str] = []
    changes, keys = ("GET", "/lb/index/changes"), ("GET", "/lb/index/keys")
    for route, upstream, params in ((changes, "/api/index/changes", ("since", "epoch")),
                                    (keys, "/api/index/keys", ())):
        spec = hub.LB_ROUTES.get(route)
        if spec is None:
            failures.append(f"{route} is not whitelisted")
            continue
        want = {"method": "GET", "path": upstream, "cache": False,
                "pool": "sync", "coalesce": False}
        for k, v in want.items():
            if spec.get(k) != v:
                failures.append(f"{route}[{k!r}] = {spec.get(k)!r}, expected {v!r}")
        if tuple(spec.get("params") or ()) != params:
            failures.append(f"{route} params = {spec.get('params')!r}, expected {params!r}")
    # I-006: these two opt out too (tools/test_lb_listening_routes.py asserts why).
    LISTENING_NO_COALESCE = (("GET", "/lb/listening/summary"), ("GET", "/lb/listening/unowned"))
    # `coalesce` is a new key and absent must mean today's behaviour everywhere else.
    for table in (hub.LB_ROUTES, hub.SONIC_ROUTES, hub.PREVIEW_ROUTES):
        for route, spec in table.items():
            if route in (changes, keys, *LISTENING_NO_COALESCE):
                continue
            if "coalesce" in spec:
                failures.append(f"{route} grew a coalesce key; only the sync routes and "
                                "the listening summary/unowned reads opt out")
    for route in (changes, keys, ("POST", "/lb/index")):
        if route in hub.LB_LIBRARY_ROUTES:
            failures.append(f"{route} is in LB_LIBRARY_ROUTES — the index is not a library event")
    if getattr(hub, "PROXY_SYNC_INFLIGHT", None) != 1:
        failures.append(f"PROXY_SYNC_INFLIGHT = {getattr(hub, 'PROXY_SYNC_INFLIGHT', None)!r}, expected 1")
    if hub.LB_ROUTES[("GET", "/lb/status")].get("path") != "/api/health":
        failures.append("/lb/status still probes /api/summary rather than /api/health")
    # A hand-edited state.json goes through the same validation as a push.
    for stored, want in (
            ({"seq": 1, "epoch": "e", "progress": {"unindexed": 2, "total": 3, "current": "x"}},
             {"seq": 1, "epoch": "e", "progress": {"unindexed": 2, "total": 3, "current": "x"}}),
            ({"seq": 1, "epoch": "e", "progress": {"unindexed": "2", "total": 3}},
             {"seq": 1, "epoch": "e"}),
            ({"seq": 1, "epoch": "e", "progress": None}, {"seq": 1, "epoch": "e"})):
        if hub._lb_index_hint(stored) != want:
            failures.append(f"state.json's lbIndex {stored!r} loads as "
                            f"{hub._lb_index_hint(stored)!r}, expected {want!r}")
    return failures


async def phase_live(port: int, state: str, lb_url: str) -> list[str]:
    """Routes, pools, the push and `welcome`, against a healthy stub."""
    failures: list[str] = []
    pool = ThreadPoolExecutor(max_workers=16)
    loop = asyncio.get_running_loop()

    def bg(*a, **k):
        return loop.run_in_executor(pool, lambda: request(*a, **k))

    try:
        # ----- welcome before any push: `lb`, and no `lbIndex` ------------- #
        with hits_lock:
            health_hits = hits.get("/api/health", 0)
        if health_hits < 1:
            failures.append("the hub never probed /api/health on its own")
        first = await welcome_of(port, "w1")
        lb = first.get("lb")
        if not isinstance(lb, dict):
            failures.append(f"welcome carries no `lb`: {sorted(first)}")
            lb = {}
        if lb.get("available") is not True:
            failures.append(f"welcome.lb.available = {lb.get('available')!r} with lb-bot healthy")
        if "lbIndex" in first:
            failures.append(f"welcome carried lbIndex before any push: {first['lbIndex']!r}")
        s, b, _e = request(port, "/lb/status")
        status_routes = json.loads(b).get("routes") if s == 200 else None
        if lb.get("routes") != status_routes:
            failures.append(f"welcome.lb.routes differs from /lb/status routes: "
                            f"{lb.get('routes')!r} vs {status_routes!r}")
        if "GET /lb/index/changes" not in (lb.get("routes") or []):
            failures.append("the sync route is not advertised in welcome.lb.routes")
        # Where a client sends the user for lb-bot's own workspace: LBBOT_WEB_URL
        # when set, else the address the hub itself proxies to — and the same
        # value from /lb/status as from the welcome.
        status_web = json.loads(b).get("webUrl") if s == 200 else None
        if lb.get("webUrl") != lb_url or status_web != lb_url:
            failures.append(f"webUrl should fall back to LBBOT_URL {lb_url!r}: "
                            f"welcome {lb.get('webUrl')!r}, /lb/status {status_web!r}")
        if s == 200 and json.loads(b).get("upstreamReachable") is not True:
            failures.append(f"/lb/status reports lb-bot unreachable: {b[:120]!r}")
        # downloads_relayed_on_welcome_and_frame (the welcome half; the frame
        # half is in unit_checks) and lb_status_carries_downloads.
        if lb.get("downloads") != "off":
            failures.append(f"welcome.lb.downloads = {lb.get('downloads')!r} with lb-bot's "
                            "health saying \"off\"")
        if s == 200 and json.loads(b).get("downloads") != "off":
            failures.append(f"/lb/status downloads = {json.loads(b).get('downloads')!r}, "
                            "expected \"off\" (Navic's LbStatusProbe reads this route)")

        # ----- params whitelisted; uncached; uncoalesced ------------------- #
        with hits_lock:
            hits.pop("/api/index/changes", None)
            queries.pop("/api/index/changes", None)
        same = "/lb/index/changes?since=5&epoch=1-abc&evil=1"
        results = await asyncio.gather(*(bg(port, same) for _ in range(3)))
        if any(r[0] != 200 for r in results):
            failures.append(f"concurrent sync reads did not all answer 200: {[r[0] for r in results]}")
        with hits_lock:
            n = hits.get("/api/index/changes", 0)
            seen = list(queries.get("/api/index/changes", []))
        if n != 3:
            failures.append(f"three identical concurrent sync reads reached lb-bot {n} times, "
                            "expected 3 — a joined read can predate the write that was pushed")
        if len({json.loads(r[1]).get("n") for r in results if r[0] == 200}) != 3:
            failures.append("identical sync reads shared one answer")
        if seen and any(sorted(q.split("&")) != ["epoch=1-abc", "since=5"] for q in seen):
            failures.append(f"sync params not whitelisted as since/epoch: {seen!r}")
        s1, b1, _e = request(port, "/lb/index/keys?x=1")
        s2, b2, _e = request(port, "/lb/index/keys")
        if (s1, s2) != (200, 200) or json.loads(b1).get("n") == json.loads(b2).get("n"):
            failures.append(f"/lb/index/keys was served from cache: {b1[:80]!r} {b2[:80]!r}")
        with hits_lock:
            kq = list(queries.get("/api/index/keys", []))
        if any(kq):
            failures.append(f"/lb/index/keys forwarded params it does not list: {kq!r}")

        # ----- the sync pool is independent of both others ---------------- #
        held = [bg(port, f"/lb/album/sources?rgid=r{i}") for i in range(4)]
        held += [bg(port, f"/lb/gap?group_id=slow&sourcePage={i}") for i in range(2)]
        await asyncio.sleep(0.6)
        s, b, elapsed = await bg(port, "/lb/index/keys")
        if s != 200 or elapsed > 1.0:
            failures.append(f"a sync read behind full default+fast pools answered {s} "
                            f"after {elapsed:.1f}s — it queued")
        await asyncio.gather(*held)

        holder = bg(port, "/lb/index/changes?since=slow")
        await asyncio.sleep(0.5)
        polled = bg(port, "/lb/album/status?release_mbid=m1")
        disco = bg(port, "/lb/artist/discography?nd_id=pool-check")
        second = bg(port, "/lb/index/changes?since=1")
        (sp, _bp, ep), (sd, _bd, ed), (s2, b2, e2) = await asyncio.gather(polled, disco, second)
        if sp != 200 or ep > 1.0:
            failures.append(f"a status poll behind a running sync answered {sp} after {ep:.1f}s")
        if sd != 200 or ed > 1.0:
            failures.append(f"a default-pool read behind a running sync answered {sd} after {ed:.1f}s")
        if s2 != 503:
            failures.append(f"a second sync with the slot held answered {s2} after {e2:.1f}s, "
                            "expected 503")
        else:
            if not json.loads(b2).get("busy"):
                failures.append(f"sync 503 carries no busy flag: {b2[:120]!r}")
            if e2 > QUEUE_TIMEOUT + 1.5:
                failures.append(f"the sync busy answer took {e2:.1f}s, expected ~{QUEUE_TIMEOUT}s")
        await holder

        # ----- inbound POST /lb/index: refusals --------------------------- #
        bad = [({"seq": -1, "epoch": "1-a"}, "negative seq"),
               ({"seq": True, "epoch": "1-a"}, "boolean seq"),
               ({"seq": "5", "epoch": "1-a"}, "string seq"),
               ({"seq": 5.5, "epoch": "1-a"}, "fractional seq"),
               ({"seq": 5, "epoch": ""}, "empty epoch"),
               ({"seq": 5, "epoch": "x" * 65}, "65-char epoch"),
               ({"seq": 5, "epoch": 7}, "numeric epoch"),
               ({"seq": 5}, "missing epoch"),
               ({"epoch": "1-a"}, "missing seq"),
               ([5, "1-a"], "a list body")]
        for body, what in bad:
            s, _b, _e = request(port, "/lb/index", "POST", body)
            if s != 400:
                failures.append(f"a push with {what} answered {s}, expected 400")
        s, _b, _e = request(port, "/lb/index", "POST", raw=b"{nope")
        if s != 400:
            failures.append(f"a non-JSON push answered {s}, expected 400")
        s, _b, _e = request(port, "/lb/index", "POST", {"seq": 1, "epoch": "1-a"}, token="wrong")
        if s != 401:
            failures.append(f"a push with a bad token answered {s}, expected 401")
        s, _b, _e = request(port, "/lb/index")
        if s != 405:
            failures.append(f"GET /lb/index answered {s}, expected 405")

        # ----- the push: frame, cache scope, persistence ------------------ #
        request(port, "/lb/artist/discography?nd_id=a1")
        request(port, "/lb/album/lookup?q=abc")
        with hits_lock:
            disco_before = hits.get("/api/artist/discography", 0)
            lookup_before = hits.get("/api/album/lookup", 0)
        request(port, "/lb/artist/discography?nd_id=a1")
        with hits_lock:
            if hits.get("/api/artist/discography", 0) != disco_before:
                failures.append("the discography read was not cached to begin with — "
                                "the invalidation check below would prove nothing")

        ws, _first = await connect(port, "watcher")
        await drain(ws, 0.3)
        s, b, _e = await bg(port, "/lb/index", "POST",
                            {"seq": 42, "epoch": "1-deadbeef", "evil": {"x": 1}})
        if s != 200:
            failures.append(f"a valid push answered {s}: {b[:120]!r}")
        frames = await drain(ws, 0.8)
        await ws.close()
        index = [f for f in frames if f.get("t") == "index"]
        if index != [{"t": "index", "seq": 42, "epoch": "1-deadbeef"}]:
            failures.append(f"expected exactly one index frame with seq/epoch, got {index!r}")
        if any(f.get("t") == "library" for f in frames):
            failures.append("a push produced a `library` frame — clients would refetch everything")
        if any(f.get("t") == "lb" for f in frames):
            failures.append("a push with lb-bot already available broadcast an `lb` frame — "
                            "only a CHANGED verdict may")

        request(port, "/lb/artist/discography?nd_id=a1")
        request(port, "/lb/album/lookup?q=abc")
        with hits_lock:
            if hits.get("/api/artist/discography", 0) != disco_before + 1:
                failures.append("the push left the discography cache in place")
            if hits.get("/api/album/lookup", 0) != lookup_before:
                failures.append("the push flushed /lb/album/lookup — it must touch nothing in "
                                "LB_LIBRARY_ROUTES but the discography")

        after = await welcome_of(port, "w2")
        if after.get("lbIndex") != {"seq": 42, "epoch": "1-deadbeef"}:
            failures.append(f"welcome.lbIndex after a push = {after.get('lbIndex')!r}")
        with open(state, encoding="utf-8") as f:
            persisted = json.load(f).get("lbIndex")
        if persisted != {"seq": 42, "epoch": "1-deadbeef"}:
            failures.append(f"the first push was not persisted at once: {persisted!r}")

        # ----- Worbus P1: index-build progress ----------------------------- #
        # (The push above, with no `progress`, is `push_without_progress_unchanged`:
        # its frame, welcome and state.json copies are exactly {seq, epoch}.)
        async def push_and_frames(body_in: dict) -> tuple[int, list[dict]]:
            w, _f = await connect(port, "progress-watch")
            await drain(w, 0.3)
            st, _bb, _ee = await bg(port, "/lb/index", "POST", body_in)
            got = await drain(w, 0.6)
            await w.close()
            return st, [f for f in got if f.get("t") == "index"]

        # progress_kept_in_hint_welcome_and_frame: the head did not move, only
        # the progress did; unknown keys go and `current` is cut at 200.
        long_name = "A" + "b" * 300
        # progress_only_push_invalidates_nothing (review finding 3): the head
        # did not move, so no cached read is stale; the frame still goes out.
        request(port, "/lb/artist/discography?nd_id=a1")
        request(port, "/lb/overrides")
        with hits_lock:
            disco_before = hits.get("/api/artist/discography", 0)
            ovr_before = hits.get("/api/overrides", 0)
        st, idx = await push_and_frames({"seq": 42, "epoch": "1-deadbeef",
                                         "progress": {"unindexed": 12, "total": 40,
                                                      "current": long_name, "path": "/m"}})
        want_prog = {"unindexed": 12, "total": 40, "current": long_name[:200]}
        if st != 200 or idx != [{"t": "index", "seq": 42, "epoch": "1-deadbeef",
                                 "progress": want_prog}]:
            failures.append(f"a progress-only push answered {st} with index frames {idx!r}, "
                            f"expected one carrying {want_prog!r}")
        got = (await welcome_of(port, "w-prog")).get("lbIndex")
        if got != {"seq": 42, "epoch": "1-deadbeef", "progress": want_prog}:
            failures.append(f"welcome.lbIndex after a progress push = {got!r}")
        request(port, "/lb/artist/discography?nd_id=a1")
        request(port, "/lb/overrides")
        with hits_lock:
            if (hits.get("/api/artist/discography", 0), hits.get("/api/overrides", 0)) != (
                    disco_before, ovr_before):
                failures.append("a progress-only push (same seq and epoch) dropped the "
                                "discography or override cache")

        # malformed_progress_dropped_push_accepted: each is a 200, the head is
        # taken, and the hint (welcome, frame) carries no progress.
        malformed = [("a string", "12 of 40"), ("a list", [12, 40]),
                     ("negative unindexed", {"unindexed": -1, "total": 40, "current": ""}),
                     ("bool unindexed", {"unindexed": True, "total": 40, "current": ""}),
                     ("string total", {"unindexed": 1, "total": "40", "current": ""}),
                     ("float total", {"unindexed": 1, "total": 40.0, "current": ""}),
                     ("missing total", {"unindexed": 1, "current": "x"}),
                     ("numeric current", {"unindexed": 1, "total": 40, "current": 7}),
                     ("a count past int32", {"unindexed": 2**31, "total": 2**31, "current": ""})]
        for i, (what, prog) in enumerate(malformed):
            st, idx = await push_and_frames({"seq": 50 + i, "epoch": "1-deadbeef",
                                             "progress": prog})
            if st != 200 or idx != [{"t": "index", "seq": 50 + i, "epoch": "1-deadbeef"}]:
                failures.append(f"a push with {what} progress answered {st}, frames {idx!r}; "
                                "expected 200 and a frame without progress")
        got = (await welcome_of(port, "w-bad")).get("lbIndex")
        if got != {"seq": 50 + len(malformed) - 1, "epoch": "1-deadbeef"}:
            failures.append(f"welcome.lbIndex after malformed progress = {got!r}")
        # ...and a push whose seq moved still drops them.
        request(port, "/lb/artist/discography?nd_id=a1")
        with hits_lock:
            if hits.get("/api/artist/discography", 0) != disco_before + 1:
                failures.append("pushes whose seq moved left the discography cache in place")

        # A missing or null `current` is "", not malformed.
        st, idx = await push_and_frames({"seq": 60, "epoch": "1-deadbeef",
                                         "progress": {"unindexed": 0, "total": 40,
                                                      "current": None}})
        if idx != [{"t": "index", "seq": 60, "epoch": "1-deadbeef",
                    "progress": {"unindexed": 0, "total": 40, "current": ""}}]:
            failures.append(f"a null `current` was not read as \"\": {idx!r}")

        # The state the restart phase reads back (progress_survives_restart_from_state).
        # Same epoch, so this one is saved by the throttle or the graceful stop.
        st, _idx = await push_and_frames({"seq": 42, "epoch": "1-deadbeef",
                                          "progress": {"unindexed": 3, "total": 40,
                                                       "current": "Zed"}})
        if st != 200:
            failures.append(f"the last progress push answered {st}")
    finally:
        pool.shutdown(wait=False)
    return failures


async def phase_restart(port: int) -> list[str]:
    """lbIndex survives a restart; lb-bot down, then an inbound push revives it."""
    failures: list[str] = []
    first = await welcome_of(port, "r1")
    # progress_survives_restart_from_state: the last push before the restart had it.
    if first.get("lbIndex") != {"seq": 42, "epoch": "1-deadbeef",
                                "progress": {"unindexed": 3, "total": 40, "current": "Zed"}}:
        failures.append(f"lbIndex (with progress) did not survive a hub restart: "
                        f"{first.get('lbIndex')!r}")
    if (first.get("lb") or {}).get("available") is not False:
        failures.append(f"welcome.lb.available = {(first.get('lb') or {}).get('available')!r} "
                        "with lb-bot's port refusing connections")
    if "downloads" in (first.get("lb") or {}):
        failures.append("a restarted hub that never reached lb-bot advertised `downloads` "
                        f"{(first.get('lb') or {}).get('downloads')!r}")
    ws, _w = await connect(port, "r-watch")
    await drain(ws, 0.3)
    s, _b, _e = await asyncio.to_thread(request, port, "/lb/index", "POST",
                                        {"seq": 43, "epoch": "1-deadbeef"})
    if s != 200:
        failures.append(f"push after restart answered {s}")
    frames = await drain(ws, 0.6)
    await asyncio.to_thread(request, port, "/lb/index", "POST", {"seq": 44, "epoch": "1-deadbeef"})
    frames += await drain(ws, 0.6)
    await ws.close()
    lb_frames = [f for f in frames if f.get("t") == "lb"]
    want = {"t": "lb", "available": True, "routes": (first.get("lb") or {}).get("routes"),
            "webUrl": (first.get("lb") or {}).get("webUrl")}
    if lb_frames != [want]:
        failures.append(f"down->up by an inbound push should broadcast exactly one `lb` frame "
                        f"shaped like welcome.lb, got {lb_frames!r}")
    second = await welcome_of(port, "r2")
    if (second.get("lb") or {}).get("available") is not True:
        failures.append("an authenticated inbound push did not mark lb-bot available")
    # Pushes without `progress` (an older lb-bot, or a build that ended) leave
    # the hint without one: the bar goes rather than sticking at its last count.
    if second.get("lbIndex") != {"seq": 44, "epoch": "1-deadbeef"}:
        failures.append(f"welcome.lbIndex after the second push = {second.get('lbIndex')!r}")
    return failures


async def phase_old_lbbot(port: int) -> list[str]:
    """An lb-bot without /api/health: the probe falls back to /api/summary."""
    failures: list[str] = []
    with hits_lock:
        summary = hits.get("/api/summary", 0)
        health = hits.get("/api/health", 0)
    if summary < 1 or health < 1:
        failures.append(f"the hub's own probe did not try /api/health then /api/summary "
                        f"(health={health}, summary={summary})")
    first = await welcome_of(port, "o1")
    if (first.get("lb") or {}).get("available") is not True:
        failures.append("a 404 on /api/health made the hub report an old lb-bot as down")
    s, b, _e = request(port, "/lb/status")
    if s != 200 or json.loads(b).get("upstreamReachable") is not True:
        failures.append(f"/lb/status against an old lb-bot = {s} {b[:120]!r}")
    # health_without_downloads_omits_the_key, live: an lb-bot that says nothing
    # about downloads is advertised exactly as before.
    if "downloads" in (first.get("lb") or {}):
        failures.append(f"an old lb-bot's welcome.lb carries downloads "
                        f"{(first.get('lb') or {}).get('downloads')!r}")
    if s == 200 and "downloads" in json.loads(b):
        failures.append(f"an old lb-bot's /lb/status carries downloads: {b[:120]!r}")
    return failures


HARD_KILL_THROTTLE = 3.0   # HUB_LB_INDEX_SAVE_S for phase_hard_kill


async def wait_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A whole HTTP request: a bare connect makes the hub log an EOFError.
        status, _b, _e = await asyncio.to_thread(request, port, "/lb/status", timeout=1)
        if status is not None:
            return
        await asyncio.sleep(0.1)


async def phase_hard_kill(port: int, lb_url: str) -> list[str]:
    """Review finding 2: the hint on disk after a crash (SIGKILL, no graceful
    flush), with LB_INDEX_SAVE_THROTTLE scaled to HARD_KILL_THROTTLE.

    final_progress_saved_at_once: a build's last pushes land inside one throttle
    window, the last saying `unindexed: 0`; killed at once, the restarted hub
    must not show the count from the first of them. trailing_save_after_burst:
    a throttled push that is not an edge is on disk one window later."""
    failures: list[str] = []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    os.unlink(state)   # a hub with no state file yet
    epoch = "9-hardkill"

    def push(seq: int, unindexed: int) -> None:
        s, b, _e = request(port, "/lb/index", "POST",
                           {"seq": seq, "epoch": epoch,
                            "progress": {"unindexed": unindexed, "total": 10,
                                         "current": f"a{seq}"}})
        if s != 200:
            failures.append(f"hard-kill push seq {seq} answered {s} {b[:80]!r}")

    try:
        proc = spawn(port, state, lb_url, HUB_LB_INDEX_SAVE_S=str(HARD_KILL_THROTTLE))
        try:
            await wait_listening(port)
            push(1, 5)    # a new epoch: saved at once
            push(2, 3)    # throttled
            push(3, 0)    # the build's end, inside the same window
        finally:
            proc.kill()   # SIGKILL: no graceful flush
            proc.wait(timeout=5)
        proc = spawn(port, state, lb_url, HUB_LB_INDEX_SAVE_S=str(HARD_KILL_THROTTLE))
        try:
            await wait_listening(port)
            got = (await welcome_of(port, "hk1")).get("lbIndex")
            want = {"seq": 3, "epoch": epoch,
                    "progress": {"unindexed": 0, "total": 10, "current": "a3"}}
            if got != want:
                failures.append(f"final_progress_saved_at_once: after a SIGKILL the restarted "
                                f"hub's lbIndex is {got!r}, expected {want!r}")
            push(4, 0)    # the first push of a process: saved at once (leading edge)
            push(5, 2)    # throttled, not an edge (a count that moved): trailing save
            await asyncio.sleep(HARD_KILL_THROTTLE + 1.0)
        finally:
            proc.kill()
            proc.wait(timeout=5)
        proc = spawn(port, state, lb_url, HUB_LB_INDEX_SAVE_S=str(HARD_KILL_THROTTLE))
        try:
            await wait_listening(port)
            got = (await welcome_of(port, "hk2")).get("lbIndex")
            if (got or {}).get("seq") != 5:
                failures.append(f"trailing_save_after_burst: a throttled push was not on disk "
                                f"{HARD_KILL_THROTTLE + 1.0:.0f} s later: lbIndex {got!r}")
        finally:
            stop(proc)
    finally:
        for path in (state, state + ".bak", state + ".tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass
    return failures


class FakeHub:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def _broadcast(self, obj: dict) -> None:
        self.frames.append(obj)


async def unit_checks() -> list[str]:
    """The verdict machinery in-process: flips broadcast once, unchanged never."""
    import hub
    failures: list[str] = []
    fake = FakeHub()
    hub.HUB_INSTANCE = fake
    original_up = type(hub.LB).upstream
    original_call = hub._proxy_upstream_blocking
    answers: list[int] = []
    timeouts: list[float] = []

    def upstream(method, url, payload, token, timeout, label):
        timeouts.append(timeout)
        answer = answers.pop(0)
        if isinstance(answer, tuple):
            return answer[0], answer[1], "application/json"
        return answer, b'{"ok":true}', "application/json"

    async def settle():
        for _ in range(3):
            await asyncio.sleep(0)

    try:
        # ----- unconfigured: welcome.lb and /lb/status agree ------------- #
        hub.LbProxy.upstream = property(lambda self: "")
        off = hub.LbProxy()
        info = off.welcome_info()
        _st, _h, body = off.disabled_probe(("GET", "/lb/status"))
        if info != {"available": False, "routes": [], "webUrl": ""}:
            failures.append(f"unconfigured welcome.lb = {info!r}, expected no routes")
        if json.loads(body).get("webUrl") != "":
            failures.append(f"unconfigured /lb/status webUrl = {json.loads(body).get('webUrl')!r}")
        if json.loads(body).get("routes") != info["routes"]:
            failures.append(f"unconfigured /lb/status routes {json.loads(body).get('routes')!r} "
                            f"!= welcome.lb routes {info['routes']!r}")

        # ----- flips broadcast exactly once; unchanged never ------------- #
        # (The stub's health says nothing about downloads, so these frames have
        # no `downloads` key: the exact-dict asserts below also pin that.)
        hub.LbProxy.upstream = property(lambda self: "http://lb.invalid")
        hub._proxy_upstream_blocking = upstream
        lb = hub.LbProxy()
        steps = [("probe", 200, True), ("probe", 200, None), ("probe", 503, False),
                 ("probe", 503, None), ("alive", None, True), ("alive", None, None),
                 ("probe", 200, None)]
        for i, (kind, status, expect) in enumerate(steps):
            fake.frames.clear()
            if kind == "probe":
                answers.append(status)
                await lb.probe()
            else:
                lb.mark_alive()
            await settle()
            frames = [f for f in fake.frames if f.get("t") == "lb"]
            want = ([] if expect is None else
                    [{"t": "lb", "available": expect, "routes": lb.advertised_routes(),
                      "webUrl": "http://lb.invalid"}])
            if frames != want:
                failures.append(f"step {i} ({kind} {status}): lb frames {frames!r}, expected {want!r}")
        if not lb.advertised_routes():
            failures.append("an enabled proxy advertised no routes")

        # ----- Worbus P1: lb-bot's downloads mode ----------------------- #
        # downloads_flip_broadcasts_once + downloads_relayed_on_welcome_and_frame
        # (frame half) + health_without_downloads_omits_the_key. Each step's
        # `want` is the frame's lb-bot part, or None for "no frame".
        def body(obj) -> bytes:
            return json.dumps(obj).encode()

        lb = hub.LbProxy()
        steps = [
            ((200, body({"ok": True, "downloads": "on"})), {"available": True, "downloads": "on"}),
            ((200, body({"ok": True, "downloads": "on"})), None),
            # The mode alone changed (lb-bot restarted with its key removed between
            # two probes): one frame, though `available` did not move.
            ((200, body({"ok": True, "downloads": "off"})), {"available": True, "downloads": "off"}),
            ((200, body({"ok": True, "downloads": "off"})), None),
            # Unreachable: says nothing about the mode, which is kept...
            ((503, body({"error": "x"})), {"available": False, "downloads": "off"}),
            # ...so a push that marks it alive again advertises the mode it had.
            ("alive", {"available": True, "downloads": "off"}),
            # An lb-bot that stopped saying (downgraded): the key goes.
            ((200, body({"ok": True})), {"available": True}),
            ((200, body({"ok": True})), None),
        ]
        for i, (step, want_part) in enumerate(steps):
            fake.frames.clear()
            if step == "alive":
                lb.mark_alive()
            else:
                answers.append(step)
                await lb.probe()
            await settle()
            frames = [f for f in fake.frames if f.get("t") == "lb"]
            want = ([] if want_part is None else
                    [{"t": "lb", "routes": lb.advertised_routes(),
                      "webUrl": "http://lb.invalid", **want_part}])
            if frames != want:
                failures.append(f"downloads step {i}: lb frames {frames!r}, expected {want!r}")
            info = lb.welcome_info()
            if frames and {"t": "lb", **info} != frames[-1]:
                failures.append(f"downloads step {i}: the frame {frames[-1]!r} is not "
                                f"welcome.lb {info!r}")

        # garbage_downloads_ignored: never relayed, never a broadcast of its own.
        garbage = [body({"ok": True, "downloads": "ON"}), body({"ok": True, "downloads": True}),
                   body({"ok": True, "downloads": ["on"]}), body({"ok": True, "downloads": None}),
                   body({"ok": True, "downloads": ""}), body(["on"]), b"downloads: on",
                   body({"ok": True, "downloads": "on", "pad": "x" * getattr(hub, "LB_HEALTH_PARSE_MAX", 65536)})]
        lb = hub.LbProxy()
        answers.append(200)
        await lb.probe()
        await settle()
        for raw in garbage:
            fake.frames.clear()
            answers.append((200, raw))
            await lb.probe()
            await settle()
            info = lb.welcome_info()
            if "downloads" in info or any(f.get("t") == "lb" for f in fake.frames):
                failures.append(f"garbage downloads {raw[:60]!r} was relayed: welcome.lb "
                                f"{info.get('downloads')!r}, {len(fake.frames)} frame(s)")
        # From a reported "off", a garbage answer reads as "not reported": the key
        # goes (once), it does not stick at the last valid value nor take the bad one.
        answers.append((200, body({"downloads": "off"})))
        await lb.probe()
        await settle()
        fake.frames.clear()
        answers.append((200, body({"downloads": "maybe"})))
        await lb.probe()
        await settle()
        if [f.get("downloads", "absent") for f in fake.frames if f.get("t") == "lb"] != ["absent"]:
            failures.append("off -> garbage should broadcast one frame without downloads, got "
                            f"{[f.get('downloads', 'absent') for f in fake.frames]!r}")

        # ----- missing_config_logged_on_change (review nit 7) ------------ #
        # lb-bot's `missingConfig` is logged once each time it changes, names
        # only; never relayed, never a broadcast of its own.
        lines: list[str] = []
        original_log = hub.log
        hub.log = lambda *a: lines.append(" ".join(str(x) for x in a))
        try:
            lb = hub.LbProxy()
            seq = [({"ok": True, "downloads": "on"}, 0),               # older: no list
                   ({"ok": True, "downloads": "on", "missingConfig": []}, 0),
                   ({"ok": True, "downloads": "on",
                     "missingConfig": ["LB_BOT_HUB_URL", "NAVIDROME_PASSWORD"]}, 1),
                   ({"ok": True, "downloads": "on",
                     "missingConfig": ["NAVIDROME_PASSWORD", "LB_BOT_HUB_URL"]}, 0),
                   ({"ok": True, "downloads": "on",
                     "missingConfig": ["NAVIDROME_PASSWORD", "hunter2=secret", 7,
                                       "LB_BOT_HUB_URL"]}, 0),
                   ({"ok": True, "downloads": "on", "missingConfig": ["NAVIDROME_PASSWORD"]}, 1),
                   ({"ok": True, "downloads": "on"}, 0),               # stopped saying
                   ({"ok": True, "downloads": "on", "missingConfig": []}, 1)]
            for i, (health, want) in enumerate(seq):
                lines.clear()
                fake.frames.clear()
                answers.append((200, body(health)))
                await lb.probe()
                await settle()
                said = [ln for ln in lines if "required settings" in ln]
                if len(said) != want:
                    failures.append(f"missingConfig step {i}: {len(said)} log line(s) {said!r}, "
                                    f"expected {want}")
                if any("hunter2" in ln or "secret" in ln for ln in lines):
                    failures.append(f"missingConfig step {i}: a non-name entry reached the log")
                if "missingConfig" in lb.welcome_info() or any(
                        "missingConfig" in f for f in fake.frames):
                    failures.append(f"missingConfig step {i}: relayed on the wire")
                if i == 2 and said and not ("LB_BOT_HUB_URL" in said[0]
                                            and "NAVIDROME_PASSWORD" in said[0]):
                    failures.append(f"missingConfig: the log line does not name both: {said!r}")
        finally:
            hub.log = original_log

        # ----- the probe's own health call is short; the fallback is not -- #
        timeouts.clear()
        answers.extend([404, 200])
        await lb.probe()
        if timeouts != [hub.LB_PROBE_TIMEOUT, hub.PROXY_TIMEOUT]:
            failures.append(f"probe timeouts (health, fallback) = {timeouts!r}, expected "
                            f"[{hub.LB_PROBE_TIMEOUT}, {hub.PROXY_TIMEOUT}]")
        if not hub.LB_PROBE_TIMEOUT <= 5:
            failures.append(f"LB_PROBE_TIMEOUT = {hub.LB_PROBE_TIMEOUT}; a hanging lb-bot must "
                            "read as down within seconds")

        # ----- an expired first-welcome wait is not paid twice ----------- #
        hung = hub.LbProxy()
        started = time.monotonic()
        await hung.wait_first_probe(0.2)
        await hung.wait_first_probe(0.2)
        elapsed = time.monotonic() - started
        if elapsed > 0.35:
            failures.append(f"two welcomes during a hanging first probe waited {elapsed:.2f}s "
                            "in total — the second paid the wait again")
    finally:
        hub.LbProxy.upstream = original_up
        hub._proxy_upstream_blocking = original_call
        hub.HUB_INSTANCE = None
    return failures


async def generation_checks() -> list[str]:
    """Final-review I1: the invalidation generation is per ROUTE.

    `invalidate()` used to bump one proxy-wide counter, and `_call_upstream`
    caches only when the counter is unchanged. A bulk build pushes `/lb/index`
    every 2 s, so every cacheable read slower than that — `/lb/album/sources`
    (45-150 s), the six-hour MusicBrainz routes — was never cached at all while
    the build ran. A push must void only the reads it actually invalidates.
    """
    import hub
    failures: list[str] = []
    original_up = type(hub.LB).upstream
    original_call = hub._proxy_upstream_blocking
    push_invalidates = hub.LB_INDEX_INVALIDATES

    def slow_upstream(method, url, payload, token, timeout, label):
        # The push lands while this read is upstream — mid-flight, on the
        # worker thread, is the only placement that exercises the race.
        hub.LB.invalidate(push_invalidates)
        return 200, b'{"answer": 1}', "application/json"

    try:
        type(hub.LB).upstream = property(lambda self: "http://lb.invalid")
        hub._proxy_upstream_blocking = slow_upstream
        for route, params, should_cache in (
                (("GET", "/lb/album/sources"), [("rgid", "r1")], True),
                (("GET", "/lb/album/releases"), [("rgid", "r1")], True),
                (("GET", "/lb/artist/discography"), [("nd_id", "a1")], False)):
            hub.LB._cache.clear()
            await hub.LB.call(route, hub.LB_ROUTES[route], list(params), None)
            cached = bool(hub.LB._cache)
            if cached != should_cache:
                failures.append(
                    f"{route[1]} read with an /lb/index push mid-flight: cached={cached}, "
                    f"expected {should_cache}" + (
                        " — an unrelated push voided a slow read's cache entry"
                        if should_cache else
                        " — a read the push invalidated re-cached its stale answer"))
        # /lb/notify keeps its whole-set meaning: a library route read in flight
        # across it is still not re-cached.
        def notify_upstream(method, url, payload, token, timeout, label):
            hub.LB.invalidate(hub.LB_LIBRARY_ROUTES)
            return 200, b'{"answer": 1}', "application/json"
        hub._proxy_upstream_blocking = notify_upstream
        hub.LB._cache.clear()
        route = ("GET", "/lb/album/lookup")
        await hub.LB.call(route, hub.LB_ROUTES[route], [("q", "x")], None)
        if hub.LB._cache:
            failures.append("a library read in flight across /lb/notify re-cached its answer")
    finally:
        type(hub.LB).upstream = original_up
        hub._proxy_upstream_blocking = original_call
        hub.LB._cache.clear()
    return failures


async def run() -> list[str]:
    failures = static_checks()
    failures += await unit_checks()
    failures += await generation_checks()
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    lb_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    port = _free_port()
    try:
        mode["downloads"] = "off"
        proc = spawn(port, state, lb_url)
        try:
            await asyncio.sleep(1.5)
            failures += await phase_live(port, state, lb_url)
        finally:
            stop(proc)
            mode["downloads"] = None

        proc = spawn(port, state, dead_url)
        try:
            await asyncio.sleep(1.5)
            failures += await phase_restart(port)
        finally:
            stop(proc)

        failures += await phase_hard_kill(port, lb_url)

        mode["health"] = 404
        fresh = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        with hits_lock:
            hits.pop("/api/health", None)
            hits.pop("/api/summary", None)
        proc = spawn(port, fresh, lb_url)
        try:
            await asyncio.sleep(1.5)
            failures += await phase_old_lbbot(port)
        finally:
            stop(proc)
            os.unlink(fresh)
    finally:
        upstream.shutdown()
        try:
            os.unlink(state)
        except OSError:
            pass
    return failures


def main() -> int:
    failures = asyncio.run(run())
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - lb index: sync-routes-uncached-uncoalesced-whitelisted/sync-pool-independent/"
          "sync-busy-is-503/push-validated/index-frame-not-library/discography-only-invalidated/"
          "welcome-lb-and-lbIndex/lbIndex-survives-restart/inbound-marks-available/"
          "health-probe-falls-back-on-404/lb-frame-on-each-flip-only/short-probe-deadline/"
          "expired-welcome-wait-not-repeated/unconfigured-routes-agree/"
          "push-leaves-unrelated-slow-reads-cacheable/"
          "downloads-relayed-on-welcome-frame-and-status/downloads-flip-broadcasts-once/"
          "health-without-downloads-omits-the-key/garbage-downloads-ignored/"
          "progress-kept-in-hint-welcome-and-frame/malformed-progress-dropped-push-accepted/"
          "push-without-progress-unchanged/progress-survives-restart-from-state/"
          "missing-config-logged-on-change/progress-only-push-invalidates-nothing/"
          "final-progress-saved-at-once/trailing-save-after-burst")
    return 0


def _inherited_env() -> dict:
    """os.environ without PREVIEW_*: an exported PREVIEW_URL with no secret makes a
    hub refuse to start (B-303), and this suite starts its hubs without previews."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PREVIEW_")}


if __name__ == "__main__":
    sys.exit(main())
