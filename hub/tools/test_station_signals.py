#!/usr/bin/env python3
"""
Tests for what steers a station from the session (PROTOCOL §18.3), the binding
(§18.2), and the parts of §17 and B-136 that stations touched.

Real WebSocket clients drive a real hub subprocess (run with HUB_DEBUG so its
judgement of every track left is in its log), against the fake AudioMuse and
Navidrome of test_station_engine. A judgement shows up twice: as a `station` frame
whose `rev` moved (a skip or a play-through steers the bound station), and as the
hub's own debug line naming the verdict.

  1. 90 % then `next` = played through;
  2. `next` at 10 s = a skip;
  3. a backward jump = neither;
  4. a report that moves the index is judged by the report BEFORE it: 95 % then
     an auto-advance = played through, 5 s then an auto-advance = a skip;
  5. a stale report (naming the index just left) = nothing;
  6. setQueue = neither, and binds/unbinds by its `stationId`;
  7. an `ended` report = played through, once however often it repeats;
  8. the station frame and welcome.station, and "station" in welcome.features;
  9. skip/played signals for a bound station answer ignored: "session";
 10. the log holds no navidrome_password and no t=/s=/p= query string, and the
     proxy's timeout line names the path, never the query (B-136);
 11. mixes round-trip `character` beside the legacy `moodCharacter`.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import time

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_edits import Client, _free_port  # noqa: E402
from test_station_engine import (LIB, ND_PASS, AudioMuse, Navidrome, acall,  # noqa: E402
                                 serve, spawn, stop, unlink_all, wait_up)

DUR = 200_000


class SClient(Client):
    """test_edits.Client, also keeping station frames, welcome and the session."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.station_frames: list[dict] = []
        self.welcome: dict = {}
        self.session: dict = {}
        self.order: list[tuple] = []   # ("station" | "session", stationId), in arrival order

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    self.welcome = msg
                    self.session = msg.get("session", {})
                    self.mixes = msg.get("mixes", [])
                elif t == "session":
                    self.session = msg
                    self.order.append(("session", msg.get("stationId")))
                elif t == "station":
                    self.station_frames.append(msg)
                    self.order.append(("station", msg.get("stationId")))
                elif t == "mixes":
                    self.mixes = msg.get("mixes", [])
                elif t == "do":
                    self.dos.append(msg)
                elif t == "error":
                    self.errors.append(msg)
        except websockets.ConnectionClosed:
            pass

    def rev(self, sid):
        revs = [f["rev"] for f in self.station_frames if f.get("stationId") == sid]
        return max(revs) if revs else None


def judgements(log_path):
    """(left, new, verdict, steering) per track the hub judged, in order."""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        found = re.findall(r"STATION judged index (\d+) -> (\S+): (\w+) at \S+ ms"
                           r"( \(ended\))?( \(history only\))?", f.read())
    return [(int(i), j, v, not h) for i, j, v, _e, h in found]


async def settle(t=0.35):
    await asyncio.sleep(t)


async def session_suite(failures, log_path):
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, am_url, nd_url, extra={"HUB_DEBUG": "1"}, log_path=log_path)
    try:
        await asyncio.to_thread(wait_up, port)
        r = SClient("Recv", device_id="recv")
        c = SClient("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url)
        await c.connect(url)
        if "station" not in c.welcome.get("features", []):
            failures.append(f"welcome.features lacks station: {c.welcome.get('features')}")
        if c.welcome.get("station", "absent") is not None:
            failures.append(f"welcome.station should be null when unbound: "
                            f"{c.welcome.get('station', 'absent')}")
        if "stationId" not in c.session:
            failures.append("session carries no stationId key")

        seed = "L12_0"
        st_, a = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [seed], "name": "Indie Song 12-0"},
            "character": "balanced", "count": 14, "deviceId": "recv"})
        if st_ != 200:
            failures.append(f"start: {st_} {a}")
            return
        sid = a["stationId"]
        await settle()
        if not any(f.get("stationId") == sid and f.get("seq") == 14
                   and f.get("name") == "Indie Song 12-0 radio" and f.get("origin") == "radio"
                   for f in c.station_frames):
            failures.append(f"no station frame (with name and origin) after start: "
                            f"{c.station_frames}")
        queue = [{"id": seed, "title": LIB[seed]["title"], "artist": LIB[seed]["artist"],
                  "durationMs": DUR}] + [
            {"id": t["id"], "title": t["title"], "artist": t["artist"], "durationMs": DUR}
            for t in a["tracks"]]
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_sig", stationId=sid)
        await settle()
        if c.session.get("stationId") != sid:
            failures.append(f"setQueue with stationId did not bind: {c.session.get('stationId')}")
        d = SClient("Late", device_id="late", caps=["controller"])
        await d.connect(url)
        ws_ = d.welcome.get("station") or {}
        if ws_.get("stationId") != sid or ws_.get("seq") != 14 or ws_.get("name") != \
                "Indie Song 12-0 radio" or ws_.get("seed", {}).get("ids") != [seed] \
                or ws_.get("character") != "balanced" or "exhausted" not in ws_ \
                or ws_.get("origin") != "radio":
            failures.append(f"welcome.station: {ws_}")
        await d.ws.close()
        rev0 = c.rev(sid)

        # ----- 1. 90 % then next = played through --------------------------- #
        await r.report(index=0, positionMs=180_000, isPlaying=True)
        await settle()
        await c.act(action="next")
        await settle()
        if c.rev(sid) != rev0 + 1:
            failures.append(f"90% then next did not steer: rev {rev0} -> {c.rev(sid)}")

        # ----- 2. next at 10 s = a skip ------------------------------------- #
        await r.report(index=1, positionMs=10_000, isPlaying=True)
        await settle()
        await c.act(action="next")
        await settle()
        if c.rev(sid) != rev0 + 2:
            failures.append(f"next at 10 s did not steer: rev {c.rev(sid)}")

        # ----- 3. a backward jump = neither --------------------------------- #
        await r.report(index=2, positionMs=20_000, isPlaying=True)
        await settle()
        await c.act(action="jump", index=0)
        await settle()
        if c.rev(sid) != rev0 + 2:
            failures.append(f"a backward jump steered: rev {c.rev(sid)}")

        # ----- 4. report-driven: the report BEFORE the move decides ---------- #
        # On positions not judged yet: a position judged once is never judged
        # again (case 6).
        await c.act(action="jump", index=3)      # 0 -> 3: not the next one, neither
        await asyncio.sleep(2.3)   # past INTENT_GRACE, so reports move the index again
        await r.report(index=3, positionMs=190_000, isPlaying=True)
        await settle(0.15)
        await r.report(index=4, positionMs=500, isPlaying=True)   # auto-advance
        await settle()
        if c.rev(sid) != rev0 + 3:
            failures.append(f"95% then an auto-advance did not steer: rev {c.rev(sid)}")
        await r.report(index=4, positionMs=5_000, isPlaying=True)
        await settle(0.15)
        await r.report(index=5, positionMs=300, isPlaying=True)
        await settle()
        if c.rev(sid) != rev0 + 4:
            failures.append(f"5 s then an auto-advance did not steer: rev {c.rev(sid)}")

        # ----- 5. a stale report = nothing ----------------------------------- #
        await c.act(action="next")          # 5 -> 6 at 300 ms: a skip, by the act
        await r.report(index=5, positionMs=12_000, isPlaying=True)   # sampled before it
        await settle(0.15)
        await r.report(index=6, positionMs=900, isPlaying=True)
        await settle()
        if c.rev(sid) != rev0 + 5:
            failures.append(f"a stale report was judged, or the act was not: rev {c.rev(sid)}")
        if c.session.get("index") != 6:
            failures.append(f"the stale report moved the session: index {c.session.get('index')}")

        # ----- 6. an index flipping back and forth counts once --------------- #
        # A stale report after INTENT_GRACE moves the session back, the next moves
        # it forward again: the forward move is judged a skip of index 5 a second
        # time, and must not steer twice.
        await asyncio.sleep(2.3)
        await r.report(index=5, positionMs=12_000, isPlaying=True)
        await settle(0.15)
        await r.report(index=6, positionMs=400, isPlaying=True)
        await settle()
        if c.rev(sid) != rev0 + 5:
            failures.append(f"a flip back and forth steered again: rev {c.rev(sid)}")

        # ----- 7. a `next` on a paused session is not judged ----------------- #
        await r.report(index=6, positionMs=10_000, isPlaying=False)
        await settle()
        await c.act(action="next")             # 6 -> 7 while paused
        await settle()
        if c.rev(sid) != rev0 + 5:
            failures.append(f"a next on a paused session steered: rev {c.rev(sid)}")
        await r.report(index=7, positionMs=1_000, isPlaying=True)
        await settle()

        # ----- 8. a skip is a step forward in PLAY order (shuffle) ----------- #
        await c.act(action="shuffle", on=True)
        await settle()
        order = c.session.get("order") or []
        if not order or order[0] != 7:
            failures.append(f"shuffle order should start at the current track: {order}")
        await r.report(index=7, positionMs=10_000, isPlaying=True)
        await settle()
        await c.act(action="next")             # 7 -> order[1]: the next in play order
        await settle()
        x = c.session.get("index")
        if x != (order[1] if len(order) > 1 else None) or c.rev(sid) != rev0 + 6:
            failures.append(f"next under shuffle was not a skip: index {x}, order {order}, "
                            f"rev {c.rev(sid)}")
        pos = order.index(x) if x in order else 0
        y = next(i for i in range(len(queue)) if i != x
                 and i != order[(pos + 1) % len(order)] and abs(i - x) == 1)
        await c.act(action="jump", index=y)    # the neighbour by index, not by play order
        await settle()
        if c.rev(sid) != rev0 + 6:
            failures.append(f"a jump to index {y}, not next in play order {order}, steered")
        await c.act(action="shuffle", on=False)
        await settle()

        # ----- 9. setQueue = neither; binds and unbinds ---------------------- #
        await r.report(index=y, positionMs=150_000, isPlaying=True)
        await settle()
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_sig", stationId=sid)
        await settle()
        if c.rev(sid) != rev0 + 6:
            failures.append(f"setQueue steered the station: rev {c.rev(sid)}")
        await r.act(action="setQueue", tracks=queue[:3], index=0, play=True,
                    savedQueueId="q_sig2")
        await settle()
        if c.session.get("stationId") is not None:
            failures.append(f"a setQueue without stationId did not unbind: "
                            f"{c.session.get('stationId')}")
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_sig", stationId="st_0_nope")
        await settle()
        if c.session.get("stationId") is not None:
            failures.append("a setQueue naming an unknown station bound to it")
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_sig", stationId=sid)
        await settle()
        if c.session.get("stationId") != sid:
            failures.append("re-binding by setQueue failed")

        # ----- 10. ended = played through, once ------------------------------ #
        last = len(queue) - 1
        await c.act(action="jump", index=last)   # 0 -> last: not the next one, neither
        await settle()
        before = c.rev(sid)
        await asyncio.sleep(2.3)
        await r.report(index=last, positionMs=199_000, isPlaying=False, ended=True)
        await settle()
        await r.report(index=last, positionMs=199_000, isPlaying=False, ended=True)
        await settle()
        if before != rev0 + 6 or c.rev(sid) != rev0 + 7:
            failures.append(f"ended: rev {rev0 + 6} -> {before} -> {c.rev(sid)}, "
                            "expected one step, at the ended report")

        # ----- 11. bound: skip/played signals are the hub's own --------------- #
        for kind in ("skip", "played"):
            st_, sg = await acall(port, "POST", "/sonic/station/signal", {
                "stationId": sid, "trackId": a["tracks"][0]["id"], "kind": kind})
            if sg.get("ignored") != "session":
                failures.append(f"a {kind} signal for a bound station: {sg}")
        st_, sg = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": sid, "trackId": a["tracks"][1]["id"], "kind": "like"})
        if not sg.get("applied") or sg.get("rev") != rev0 + 8:
            failures.append(f"a like on a bound station: {sg}")
        st_, sg = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": sid, "kind": "character", "value": "explore"})
        await settle()
        if not sg.get("applied") or not any(f.get("character") == "explore"
                                            for f in c.station_frames):
            failures.append(f"a character change did not reach the frame: {sg}")

        # ----- stop: autoplay Off, on the frame and in welcome --------------- #
        st_, sp = await acall(port, "POST", "/sonic/station/signal",
                              {"stationId": sid, "kind": "stop"})
        await settle()
        if not any(f.get("stationId") == sid and f.get("stopped") is True
                   and f.get("rev") == sp.get("rev") for f in c.station_frames):
            failures.append(f"no station frame saying stopped: {c.station_frames[-1:]}")
        d2 = SClient("Late2", device_id="late2", caps=["controller"])
        await d2.connect(url)
        if (d2.welcome.get("station") or {}).get("stopped") is not True:
            failures.append(f"welcome.station must carry stopped: {d2.welcome.get('station')}")
        await d2.ws.close()
        st_, cs = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": sid, "kind": "character", "value": "explore"})   # its own
        await settle()
        if cs.get("rev") != sp.get("rev") + 1 or (c.station_frames[-1].get("stopped")
                                                   is not False):
            failures.append(f"a character signal must clear stopped on the frame: "
                            f"{cs} {c.station_frames[-1:]}")

        # ----- a bound start: its station frame before the session naming it - #
        await r.act(action="setQueue", tracks=queue[:2], index=0, play=True,
                    savedQueueId="q_order")
        await settle()
        c.order.clear()
        st_, ob = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L13_0"]}, "count": 3,
            "bindSession": "q_order"})
        await settle()
        oid = ob.get("stationId")
        frame_at = next((i for i, (t, x) in enumerate(c.order)
                         if t == "station" and x == oid), None)
        session_at = next((i for i, (t, x) in enumerate(c.order)
                           if t == "session" and x == oid), None)
        if not ob.get("bound") or frame_at is None or session_at is None \
                or frame_at > session_at:
            failures.append(f"a bound start must send its station frame before the "
                            f"session naming it: {c.order}")

        # ----- 12. a queue track with an id that is not a song id ------------ #
        bogus = [{"id": "../not a song id", "title": "Bogus", "artist": "X",
                  "durationMs": DUR}, queue[1]]
        await r.act(action="setQueue", tracks=bogus, index=0, play=True,
                    savedQueueId="q_bogus")
        await settle()
        await r.report(index=0, positionMs=195_000, isPlaying=True)
        await settle(0.15)
        await r.report(index=1, positionMs=300, isPlaying=True)
        await settle()

        # ----- 13. no active device: nothing is judged ----------------------- #
        await r.ws.close()
        await settle(0.6)
        if c.session.get("activeDeviceId") is not None:
            failures.append("the receiver left but the session kept it active")
        await c.act(action="previous")
        await c.act(action="next")
        await settle()

        # clear unbinds
        await c.act(action="setQueue", tracks=queue, index=0, play=False,
                    savedQueueId="q_sig", stationId=sid)
        await settle()
        await c.act(action="clear")
        await settle()
        if c.session.get("stationId") is not None:
            failures.append("clear did not unbind the station")

        # ----- 11. mixes: character beside moodCharacter --------------------- #
        await c.act(action="saveMix", name="Radio A", kind="station", seedId=seed,
                    character="explore")
        await c.act(action="saveMix", name="Old B", kind="adaptive",
                    moodCharacter="EchoMatch")
        await c.act(action="saveMix", name="Mixed C", kind="similar", seedId=seed,
                    character="close", moodCharacter="TransitionMaestro")
        await c.act(action="saveMix", name="Bad D", kind="similar", seedId=seed,
                    character="sideways", moodCharacter="SteadyVibes")
        await settle(0.6)
        e = SClient("Mixes", device_id="mixes", caps=["controller"])
        await e.connect(url)
        by = {m["name"]: m for m in e.mixes}
        want = {"Radio A": ("station", "explore", "TransitionMaestro"),
                "Old B": ("adaptive", "close", "EchoMatch"),
                "Mixed C": ("similar", "close", "EchoMatch"),
                "Bad D": ("similar", "balanced", "SteadyVibes")}
        for name, (kind, ch, mood) in want.items():
            m = by.get(name) or {}
            if (m.get("kind"), m.get("character"), m.get("moodCharacter")) != (kind, ch, mood):
                failures.append(f"mix {name}: {m.get('kind')} {m.get('character')} "
                                f"{m.get('moodCharacter')}, expected {kind} {ch} {mood}")
        await e.ws.close()
        await c.ws.close()
    finally:
        stop(proc)
        am_srv.shutdown()
        nd_srv.shutdown()
        try:
            with open(state + ".history", encoding="utf-8") as f:
                hist = f.read()
            if "not a song id" in hist:
                failures.append("a queue id that is not a song id entered the history")
            if '"plays"' not in hist:
                failures.append("the history file has no plays")
        except FileNotFoundError:
            failures.append("no history file after the session suite")
        unlink_all(state)

    got = judgements(log_path)
    steering = [(i, j, v) for i, j, v, steer in got if steer]
    head = [(0, "1", "played"), (1, "2", "skip"), (2, "0", "neither"),
            (0, "3", "neither"), (3, "4", "played"), (4, "5", "skip"), (5, "6", "skip"),
            (6, "5", "neither"), (5, "6", "skip")]
    if steering[:len(head)] != head:
        failures.append(f"the hub's judgements: {steering[:len(head)]}, expected {head}")
    tail = steering[len(head):]
    # 8: the skip under shuffle and the neighbour jump; 10: the jump and `ended`;
    # 12: the bogus queue's play-through. Nothing for the paused next (7), the
    # repeated `ended`, or the acts with no active device (13).
    kinds = [v for _i, _j, v in tail]
    if kinds != ["skip", "neither", "neither", "played", "played"]:
        failures.append(f"judgements after the flip: {tail}")
    history_only = [g for g in got if not g[3]]
    # Case 9's four, and case 12's (the bound-start case before it left the
    # session playing).
    if len(history_only) != 5:
        failures.append(f"the five setQueues on a playing session should each feed the "
                        f"history only: {history_only}")


class Sleepy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(1.0)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_a):
        pass


async def timeout_suite(failures):
    """B-136 through the real path: a fingerprint call that times out inside the
    hub (Your mix, via SONIC with the Navidrome creds injected), the hub's own
    output captured."""
    import test_station_engine as eng_t
    am_srv, am_url = serve(AudioMuse)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    log_path = state + ".log"
    eng_t.AM["slow_fp"] = 9.0           # past STATION_CALL_TIMEOUT (8 s)
    proc = spawn(port, state, am_url, None, extra={"HUB_STATION_HISTORY": "0"},
                 log_path=log_path)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, ym = await acall(port, "POST", "/sonic/station/start",
                              {"seed": {"kind": "yourMix"}, "count": 3})
        if st_ != 503 or not ym.get("fallback"):
            failures.append(f"Your mix with a timed-out fingerprint: {st_} {ym}")
    finally:
        stop(proc)
        eng_t.AM["slow_fp"] = 0.0
        am_srv.shutdown()
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        unlink_all(state)
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass
    line = next((ln for ln in text.splitlines() if "timed out after" in ln), "")
    if "/api/sonic_fingerprint/generate" not in line:
        failures.append(f"the timed-out fingerprint call left no log line: {text[-400:]!r}")
    if ND_PASS in text or "navidrome_password" in text or "?" in line:
        failures.append(f"the timeout line carries the query: {line!r}")


def log_checks(failures, log_path):
    with open(log_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if ND_PASS in text or "navidrome_password" in text:
        failures.append("the hub's log holds the Navidrome password")
    if re.search(r"[?&](t|s|p|u|navidrome_password)=", text):
        failures.append(f"the hub's log holds a query string: "
                        f"{re.search(r'.{40}[?&](t|s|p|u|navidrome_password)=.{20}', text)}")
    # B-136 itself: the timeout line of the shared upstream call.
    import hub as hubmod
    srv = http.server.HTTPServer(("127.0.0.1", 0), Sleepy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    buf = io.StringIO()
    url = (f"http://127.0.0.1:{srv.server_address[1]}/api/sonic_fingerprint/generate"
           f"?n=5&navidrome_user=u&navidrome_password=hunter2&t=tok&s=salt")
    with contextlib.redirect_stdout(buf):
        status, _body, _ct = hubmod._proxy_upstream_blocking("GET", url, None, "", 0.3,
                                                             "audiomuse")
    srv.shutdown()
    line = buf.getvalue()
    if status != 504:
        failures.append(f"a slow upstream should be 504, got {status}")
    if "hunter2" in line or "?" in line or "/api/sonic_fingerprint/generate" not in line:
        failures.append(f"the timeout line must name the path and no query: {line.strip()!r}")


async def main():
    failures: list[str] = []
    log_path = tempfile.NamedTemporaryFile(suffix=".log", delete=False).name
    try:
        await session_suite(failures, log_path)
        log_checks(failures, log_path)
        await timeout_suite(failures)
    finally:
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass
    if failures:
        print("FAIL (station signals):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - station signals: 90%-next-played/next-at-10s-skip/backward-neither/"
          "report-before-the-move-decides/stale-report-nothing/flip-back-counts-once/"
          "paused-next-not-judged/skip-in-shuffle-play-order/setQueue-neither+binds+"
          "unbinds/ended-played-once/frame+welcome.station+feature/bound-signals-ignored/"
          "bad-queue-id-not-in-history/no-active-device-not-judged/clear-unbinds/"
          "stop-on-frame+welcome/character-clears-stop/bound-start-frame-before-session/"
          "no-credentials-in-logs/timeout-line-path-only/timed-out-fingerprint-logs-path-"
          "only/mix-character+legacy")


if __name__ == "__main__":
    asyncio.run(main())
