#!/usr/bin/env python3
"""
A mood build never holds a request past the station budget (PROTOCOL §18.4 / §18.5,
fix round 2026-10-24, audit L3-03). In-process, no sockets: a StationEngine with a
real MoodService whose CLAP search (SONIC.call) sleeps, and STATION_BUDGET_S shortened.

Measured on the NAS after a restart: a mood ask held for the whole build (27 and 31 s;
52 serial searches at CLAP's 4 s for all 13 moods) while a plain start answered 503
"computing two stations already". Now a mood ask whose list is not built inside the
budget answers 409 mood_unavailable with `building: true`, and the build goes on.

Cases:
  start_mood_within_budget   mood start -> 409 mood_unavailable + building inside the
                             budget; the build finishes in the background (4 searches),
                             a second start answers 200 with relax's rows and asks
                             no fifth search
  plain_start_not_blocked    a plain start answers 200 while mood asks are building
  filter_branch_bounded      a radio with moodFilter: start -> 409 + building; the
                             ladder's own filter branch -> empty and partial, never the
                             neighbours unfiltered
  nearest_bounded            the mood anchor's list, asked by the ladder, is bounded
  count_bounded              count with moodFilter -> 409 + building
  signal_bounded             signal mood -> 409 + building, the station's mood unchanged
  follower_not_cancelled     two asks for one building mood: neither is handed a
                             CancelledError, and the build still lands
  failure_is_not_building    a refused search answers 409 without `building`

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import hub as hubmod  # noqa: E402

DELAY = 0.6          # one CLAP search
BUDGET = 0.3         # STATION_BUDGET_S while the cases run
FAST = 0.8           # an answer inside the budget must come in under this

CLAP_ROWS = [{"item_id": f"M{i:03d}", "title": f"Mood Song {i}", "author": f"Artist {i}"}
             for i in range(40)]
NEIGHBOURS = [{"item_id": f"N{i:03d}", "title": f"Near Song {i}", "author": f"Near {i}",
               "distance": 0.1 + i * 0.01} for i in range(60)]


class StubHub:
    """What StationEngine needs of a Hub."""

    def __init__(self):
        self.session = hubmod.Session()
        self.frames = []
        self.moods = hubmod.MoodService(self)

    def _save(self):
        pass

    def _spawn(self, coro):
        asyncio.ensure_future(coro)

    async def _broadcast(self, obj):
        self.frames.append(obj)

    async def _broadcast_session(self):
        pass


class Fake:
    """AudioMuse, as SONIC.call sees it."""

    def __init__(self):
        self.searches = 0
        self.fail_search = False
        self.cancelled = 0
        self.delay = DELAY

    async def call(self, route, spec, params, body):
        kind = route[1]
        if kind == "mood:clap_search":
            self.searches += 1
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            if self.fail_search:
                return 500, b'{"error":"boom"}', "application/json"
            return 200, json.dumps({"results": CLAP_ROWS}).encode(), "application/json"
        if kind == "station:score":
            tid = dict(params).get("id", "L00_0")
            return 200, json.dumps({"item_id": tid, "title": "Seed", "author": "Seed Artist",
                                    "album": "A"}).encode(), "application/json"
        if kind == "station:similar_tracks":
            return 200, json.dumps(NEIGHBOURS).encode(), "application/json"
        return 404, b"{}", "application/json"


def make_engine():
    hub = StubHub()
    eng = hubmod.StationEngine(hub)
    hub.stations = eng
    eng.tier2_ok = True
    hub.moods._stats_ok = True      # CLAP is there (the last verdict)
    return hub, eng, hub.moods


async def timed(coro):
    t0 = time.monotonic()
    out = await coro
    return out, time.monotonic() - t0


def start_body(**extra):
    return {"seed": {"kind": "track", "ids": ["L00_0"]}, "character": "close", "count": 10,
            **extra}


def expect_building(failures, name, res, took):
    status, body = res
    if status != 409 or body.get("code") != "mood_unavailable" or body.get("building") is not True:
        failures.append(f"{name}: expected 409 mood_unavailable + building, got {status} {body}")
    if took > FAST:
        failures.append(f"{name}: held the request {took:.2f} s (budget {BUDGET} s)")


async def case_start_within_budget(failures, fake):
    hub, eng, svc = make_engine()
    res, took = await timed(eng.start(start_body(mood="relax")))
    expect_building(failures, "start_mood_within_budget", res, took)
    # The build was not cancelled: it finishes in the background (4 prompts, serial).
    end = time.monotonic() + 15
    while time.monotonic() < end and "relax" not in svc._merged:
        await asyncio.sleep(0.1)
    if fake.searches != 4:
        failures.append(f"start_mood_within_budget: the build asked {fake.searches} searches, not 4")
    n = fake.searches
    (status, body), took = await timed(eng.start(start_body(mood="relax")))
    if status != 200 or not body.get("tracks") or body.get("mood") != "relax":
        failures.append(f"start_mood_within_budget: the second start did not find the list: "
                        f"{status} {str(body)[:200]}")
    elif not all(t["id"].startswith("M") for t in body["tracks"]):
        failures.append(f"start_mood_within_budget: not relax's rows: "
                        f"{[t['id'] for t in body['tracks']][:5]}")
    if fake.searches != n:
        failures.append(f"start_mood_within_budget: the second start asked "
                        f"{fake.searches - n} more searches")


async def case_plain_start_not_blocked(failures, fake):
    hub, eng, svc = make_engine()
    moods = [asyncio.ensure_future(eng.start(start_body(mood="relax"))) for _ in range(3)]
    moods.append(asyncio.ensure_future(eng.count(
        {"seed": {"kind": "track", "ids": ["L00_0"]}, "moodFilter": "relax"})))
    await asyncio.sleep(0.05)
    (status, body), took = await timed(eng.start(start_body()))
    if status != 200 or not body.get("tracks"):
        failures.append(f"plain_start_not_blocked: {status} {str(body)[:160]}")
    if took > BUDGET + 0.5:
        failures.append(f"plain_start_not_blocked: took {took:.2f} s")
    await asyncio.gather(*moods, return_exceptions=True)
    await drain(svc)


async def case_filter_branch(failures, fake):
    hub, eng, svc = make_engine()
    res, took = await timed(eng.start(start_body(moodFilter="relax")))
    expect_building(failures, "filter_branch_bounded (start)", res, took)
    await drain(svc)
    # A fresh service: the ladder's own filter branch, reached with a station that
    # holds the filter (a `next` on a restarted radio whose list is not rebuilt yet).
    hub, eng, svc = make_engine()
    st = station("relax_filter", mood_filter="relax")
    b = hubmod._StationBatch(time.monotonic() + BUDGET)
    tracks, took = await timed(eng._compute(st, 5, set(), b))
    if tracks:
        failures.append(f"filter_branch_bounded: served {len(tracks)} tracks without the list")
    if not (b.failed and b.timed_out):
        failures.append(f"filter_branch_bounded: failed={b.failed} timed_out={b.timed_out}")
    if took > FAST:
        failures.append(f"filter_branch_bounded: held {took:.2f} s")
    await drain(svc)


async def case_nearest(failures, fake):
    hub, eng, svc = make_engine()
    b = hubmod._StationBatch(time.monotonic() + BUDGET)
    rows, took = await timed(eng._nearest("mood:relax", 100, b))
    if rows is not None or not (b.failed and b.timed_out):
        failures.append(f"nearest_bounded: rows={rows and len(rows)} failed={b.failed} "
                        f"timed_out={b.timed_out}")
    if took > FAST:
        failures.append(f"nearest_bounded: held {took:.2f} s")
    await drain(svc)


async def case_count(failures, fake):
    hub, eng, svc = make_engine()
    res, took = await timed(eng.count(
        {"seed": {"kind": "track", "ids": ["L00_0"]}, "moodFilter": "relax"}))
    expect_building(failures, "count_bounded", res, took)
    await drain(svc)


def station(name, **kw):
    now = int(time.time() * 1000)
    st = hubmod.Station(id=f"st_1_{name}", seed={"kind": "track", "ids": ["L00_0"]},
                        name="x", character="close", created_at=now, used_at=now,
                        seed_ids=["L00_0"],
                        anchor_meta=[{"id": "L00_0", "title": "", "artist": "", "album": "",
                                      "ak": "", "tk": "", "feats": None, "mix": False}], **kw)
    st.index()
    return st


async def case_signal(failures, fake):
    hub, eng, svc = make_engine()
    st = station("signal")
    eng.stations[st.id] = st
    (status, body), took = await timed(eng.signal(
        {"stationId": st.id, "kind": "mood", "value": "relax"}))
    expect_building(failures, "signal_bounded", (status, body), took)
    if st.mood is not None:
        failures.append(f"signal_bounded: the station's mood changed to {st.mood}")
    await drain(svc)


async def case_follower(failures, fake):
    hub, eng, svc = make_engine()
    a = asyncio.ensure_future(eng.start(start_body(mood="relax")))
    await asyncio.sleep(BUDGET / 2)
    b = asyncio.ensure_future(eng.start(start_body(mood="relax")))
    out = await asyncio.gather(a, b, return_exceptions=True)
    for name, o in zip("ab", out):
        if isinstance(o, BaseException):
            failures.append(f"follower_not_cancelled: start {name} raised {o!r}")
        elif o[0] != 409:
            failures.append(f"follower_not_cancelled: start {name} answered {o[0]}")
    await drain(svc)
    if fake.cancelled:
        failures.append(f"follower_not_cancelled: {fake.cancelled} searches were cancelled")
    if not ("relax" in svc._merged):
        failures.append("follower_not_cancelled: the build did not land")


async def case_failure(failures, fake):
    hub, eng, svc = make_engine()
    fake.fail_search = True
    fake.delay = 0.0      # a refusal that comes at once is not a build in progress
    (status, body), took = await timed(eng.start(start_body(mood="relax")))
    if status != 409 or body.get("code") != "mood_unavailable":
        failures.append(f"failure_is_not_building: {status} {body}")
    elif "building" in body:
        failures.append(f"failure_is_not_building: a refused search says building: {body}")
    await drain(svc)


async def drain(svc, limit=20.0):
    """Until nothing builds in the background."""
    end = time.monotonic() + limit
    while svc._inflight and time.monotonic() < end:
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.2)


async def main():
    failures: list[str] = []
    hubmod.STATE_PATH = os.path.join(tempfile.mkdtemp(prefix="mood-deadline-"), "state.json")
    hubmod.AUDIOMUSE_URL = "http://audiomuse.invalid"
    hubmod.STATION_BUDGET_S = BUDGET
    for case in (case_start_within_budget, case_plain_start_not_blocked, case_filter_branch,
                 case_nearest, case_count, case_signal, case_follower, case_failure):
        fake = Fake()
        hubmod.SONIC.call = fake.call
        try:
            await case(failures, fake)
        except Exception as e:  # noqa: BLE001 — a case that raises is a failed case
            failures.append(f"{case.__name__}: raised {e!r}")
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - mood deadline: start-409-building-inside-budget+build-lands+second-start-200-"
          "no-fifth-search/plain-start-not-blocked/filter-branch+nearest+count+signal-bounded/"
          "follower-not-cancelled/failure-is-not-building")


if __name__ == "__main__":
    asyncio.run(main())
