#!/usr/bin/env python3
"""
Tests for a mood as a station's SOURCE or a radio's FILTER (PROTOCOL §18.5, P7 Part B),
through a real hub subprocess and its HTTP proxy path, against test_station_engine's
fake AudioMuse and Navidrome with its fake CLAP switched on.

The library gains cluster M: 1,200 tracks by 400 artists (three each), far from
everything, so nothing reaches them as a neighbour. The moods' lists (each mood's
first prompt; the other three answer nothing, so a merged list is that prompt's):

  relax    M0-M9, then L00_1..L04_1 (five of the seed's neighbours), then M10-M1199
  upbeat   L01_2..L39_2 (39 of the seed's neighbours), then M200-M1199
  focus    M only (no neighbour of the seed)
  pump-up  M only; its prompt fails after a restart (case 6)
  downbeat its prompt always fails (never built)

Cases:
  1. the probe: `moods: true` and `"mood"` in `filters` while CLAP is there, neither
     with it off; the routes take `mood` / `moodFilter` through the proxy;
  2. source: Close with mood relax serves relax's list in rank order, from its first
     150; Balanced within 400, Explore within 1000; never a neighbour of the seed that
     the list lacks; a like still leaves every id in the list;
  3. signal: `mood` on a bound station with apply swaps the station's unplayed rows
     for relax's, keeps the playing track and the user's row; "" returns to the
     seed's neighbours; a `character` keeps the mood; unknown -> 400 bad_mood; a mood
     whose list cannot be had -> 409 mood_unavailable and the mood is unchanged;
  4. filter: every served id is in the pool and in the filter's list; an empty
     intersection is `exhausted`, not relaxed; count with a filter is pool ∩ list, and
     without one it is the plain pool (the regression); mood + moodFilter is 400, a
     mood signal to a filtered radio is 400, an unknown moodFilter 400;
  5. old clients: a station without the keys serves exactly what a hub without CLAP
     serves for the same seed (ids and order);
  6. persistence: a restart keeps `mood` and `moodFilter`; after it, a filter whose
     list cannot be built makes `next` partial with nothing served, never unfiltered;
  7. `_sanitize_mix` keeps a valid `moodFilter` and drops "nope" (in-process).

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import test_station_engine as tse  # noqa: E402
from test_edits import _free_port  # noqa: E402
from test_station_edits import EClient, row, settle, signal  # noqa: E402
from test_station_engine import (AM, LIB, AudioMuse, Navidrome, acall, serve, spawn,  # noqa: E402
                                 stop, unlink_all, wait_up)

import hub as hubmod  # noqa: E402

SEED = "L00_0"


def build_mood_cluster():
    for i in range(1200):
        tse._add(f"M{i:04d}", f"Mood Song {i}", f"M Artist {i % 400:03d}", f"M Album {i % 400}",
                 20 + (i % 40) * 0.001, 20 + (i // 40) * 0.001,
                 {k: 0.5 for k in tse.FEATS}, "ambient:0.55", "ambient", "relaxed")


build_mood_cluster()
M = [f"M{i:04d}" for i in range(1200)]
RELAX = M[:10] + [f"L{a:02d}_1" for a in range(5)] + M[10:]
UPBEAT = [f"L{a:02d}_2" for a in range(1, 40)] + M[200:]
FOCUS = list(M)
PUMP = list(M)


def first_prompt(mood_id):
    return hubmod.MOOD_BY_ID[mood_id]["prompts"][0]


def setup_clap():
    AM["clap_on"] = True
    AM["clap"] = {first_prompt("relax"): RELAX, first_prompt("upbeat"): UPBEAT,
                  first_prompt("focus"): FOCUS, first_prompt("pump-up"): PUMP}
    AM["clap_fail"] = {first_prompt("downbeat")}


def capped(ids):
    """The merged list as the hub makes it: at most 3 per artist, first 1000."""
    per, out = {}, []
    for i in ids:
        ak = hubmod._station_artist_key(LIB[i]["artist"])
        if per.get(ak, 0) < 3:
            per[ak] = per.get(ak, 0) + 1
            out.append(i)
    return out[:hubmod.MOOD_GATE_DEPTH]


async def wait_moods(port, timeout=30.0):
    """Until every buildable mood is built: the station probe says moods, and a mood
    start no longer answers 409 (the warm build has reached it)."""
    end = time.time() + timeout
    while time.time() < end:
        _s, ch = await acall(port, "GET", "/sonic/station/characters")
        if (ch or {}).get("moods"):
            st_, _a = await acall(port, "POST", "/sonic/station/count", {
                "seed": {"kind": "track", "ids": [SEED]}, "character": "close",
                "moodFilter": "pump-up"})
            if st_ == 200:
                return True
        await asyncio.sleep(0.3)
    return False


async def start(port, character, **extra):
    body = {"seed": {"kind": "track", "ids": [SEED]}, "character": character, "count": 10}
    body.update(extra)
    return await acall(port, "POST", "/sonic/station/start", body)


async def refill(port, a, n=3, count=5):
    served, seq = [t["id"] for t in a["tracks"]], a["seq"]
    last = a
    for _ in range(n):
        st_, nx = await acall(port, "POST", "/sonic/station/next", {
            "stationId": a["stationId"], "after": seq, "count": count})
        if st_ != 200:
            break
        served += [t["id"] for t in nx["tracks"]]
        seq, last = nx["seq"], nx
    return served, last


async def engine_suite(failures):
    setup_clap()
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, am_url, nd_url, extra={"HUB_MOOD_RETRY_S": "1"})
    try:
        await asyncio.to_thread(wait_up, port)
        if not await wait_moods(port):
            failures.append("the moods never became usable")
            return

        # ----- 1. the probe --------------------------------------------------- #
        _s, ch = await acall(port, "GET", "/sonic/station/characters")
        if ch.get("moods") is not True or ch.get("filters") != ["mood"]:
            failures.append(f"the probe does not advertise moods: {ch.get('moods')} {ch.get('filters')}")

        relax = capped(RELAX)
        outside = {i for i in LIB if i.startswith("L")} - set(relax)

        # ----- 2. source ------------------------------------------------------ #
        st_, a = await start(port, "close", mood="relax")
        if st_ != 200 or a.get("mood") != "relax":
            failures.append(f"a Close mood start: {st_} {a}")
        else:
            served, last = await refill(port, a, n=4)
            if served != relax[:len(served)]:
                failures.append(f"Close + relax is not relax in rank order: {served[:12]}")
            if any(i not in relax[:150] for i in served):
                failures.append("Close + relax served past rank 150")
            if last.get("mood") != "relax":
                failures.append(f"next does not echo the mood: {last.get('mood')}")
            # A like is an anchor for alchemy only: the gate stays the mood.
            st_, lk = await acall(port, "POST", "/sonic/station/signal", {
                "stationId": a["stationId"], "trackId": served[0], "kind": "like"})
            more, _l = await refill(port, {**last, "tracks": []}, n=2)
            if set(more) - set(relax):
                failures.append(f"after a like, served outside the mood: {set(more) - set(relax)}")
        for character, ceiling in (("balanced", 400), ("explore", 1000)):
            st_, a = await start(port, character, mood="relax")
            if st_ != 200:
                failures.append(f"{character} mood start: {st_} {a}")
                continue
            served, _l = await refill(port, a, n=3)
            if set(served) & outside:
                failures.append(f"{character} + relax served the seed's neighbours: "
                                f"{sorted(set(served) & outside)[:5]}")
            if any(i not in relax[:ceiling] for i in served):
                failures.append(f"{character} + relax served past rank {ceiling}")

        # ----- 4. filter ------------------------------------------------------ #
        upbeat = set(capped(UPBEAT))
        st_, plain = await acall(port, "POST", "/sonic/station/count", {
            "seed": {"kind": "track", "ids": [SEED]}, "character": "close"})
        pool = {o["id"] for _d, o in tse._near(SEED, 150)} - {SEED}
        if st_ != 200 or plain.get("count") != len(pool):
            failures.append(f"count without a filter changed: {plain} != {len(pool)}")
        st_, filt = await acall(port, "POST", "/sonic/station/count", {
            "seed": {"kind": "track", "ids": [SEED]}, "character": "close",
            "moodFilter": "upbeat"})
        if st_ != 200 or filt.get("count") != len(pool & upbeat) or not 0 < filt["count"] < len(pool):
            failures.append(f"count with a filter: {filt} != {len(pool & upbeat)}")
        st_, a = await start(port, "close", moodFilter="upbeat")
        if st_ != 200 or a.get("moodFilter") != "upbeat":
            failures.append(f"a filtered start: {st_} {a}")
        else:
            served, _l = await refill(port, a, n=6)
            reach = {o["id"] for _d, o in tse._near(SEED, 2000)}
            if not served or set(served) - upbeat or set(served) - reach:
                failures.append(f"a filtered radio served outside pool ∩ list: "
                                f"{sorted(set(served) - upbeat)[:5]}")
            st_, bad = await acall(port, "POST", "/sonic/station/signal", {
                "stationId": a["stationId"], "kind": "mood", "value": "relax"})
            if st_ != 400 or bad.get("code") != "bad_mood":
                failures.append(f"a mood signal to a filtered radio: {st_} {bad}")
        st_, a = await start(port, "close", moodFilter="focus")
        if st_ != 200 or a.get("tracks") or a.get("exhausted") is not True or a.get("partial"):
            failures.append(f"an empty intersection must be exhausted, not relaxed: "
                            f"{st_} tracks={len((a or {}).get('tracks') or [])} "
                            f"exhausted={(a or {}).get('exhausted')} partial={(a or {}).get('partial')}")
        for body, want in (({"mood": "relax", "moodFilter": "upbeat"}, (400, "bad_mood")),
                           ({"mood": "nope"}, (400, "bad_mood")),
                           ({"moodFilter": "nope"}, (400, "bad_mood")),
                           ({"mood": "downbeat"}, (409, "mood_unavailable"))):
            st_, r = await start(port, "close", **body)
            if (st_, (r or {}).get("code")) != want:
                failures.append(f"start {body}: {st_} {r} != {want}")

        # ----- 3. signal, on a bound station ---------------------------------- #
        rcv = EClient("Recv", device_id="recv")
        ctl = EClient("Ctl", device_id="ctl", caps=["controller"])
        await rcv.connect(url)
        await ctl.connect(url)
        st_, a = await start(port, "balanced")
        sid = a["stationId"]
        served = [t["id"] for t in a["tracks"]]
        user = next(i for i in LIB if i.startswith("L") and i not in served
                    and i != SEED and i not in relax)
        queue = [row(SEED)] + [row(i) for i in served[:3]] + [row(user)] + \
            [row(i) for i in served[3:]]
        await rcv.act(action="setQueue", tracks=queue, index=0, play=True,
                      savedQueueId="q_mood", stationId=sid)
        await settle()
        await rcv.report(index=0, positionMs=10_000, isPlaying=True)
        await settle()
        ans = await signal(port, sid, "mood", "relax", apply=True)
        await settle()
        now = ctl.ids()
        ed = ans.get("edited") or {}
        added = [i for i in now[1:] if i != user]
        if not ed.get("added") or now[0] != SEED or user not in now:
            failures.append(f"mood + apply: {ans} {now}")
        if set(added) - set(relax):
            failures.append(f"mood + apply put in songs outside the mood: {set(added) - set(relax)}")
        frames = [f for f in ctl.station_frames if f.get("stationId") == sid]
        if not frames or frames[-1].get("mood") != "relax":
            failures.append(f"the station frame does not say the mood: {frames[-1:]}")
        ans = await signal(port, sid, "character", "close")
        await settle()
        frames = [f for f in ctl.station_frames if f.get("stationId") == sid]
        if frames[-1].get("mood") != "relax" or frames[-1].get("character") != "close":
            failures.append(f"a character signal dropped the mood: {frames[-1]}")
        st_, bad = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": sid, "kind": "mood", "value": "nope"})
        if st_ != 400 or bad.get("code") != "bad_mood":
            failures.append(f"an unknown mood signal: {st_} {bad}")
        rev = frames[-1].get("rev")
        st_, bad = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": sid, "kind": "mood", "value": "downbeat"})
        await settle()
        frames = [f for f in ctl.station_frames if f.get("stationId") == sid]
        if st_ != 409 or bad.get("code") != "mood_unavailable" or frames[-1].get("mood") != "relax" \
                or frames[-1].get("rev") != rev:
            failures.append(f"a mood whose list cannot be had: {st_} {bad} {frames[-1]}")
        ans = await signal(port, sid, "mood", "", apply=True)
        await settle()
        frames = [f for f in ctl.station_frames if f.get("stationId") == sid]
        back = [i for i in ctl.ids()[1:] if i != user]
        if frames[-1].get("mood") is not None or not back or set(back) & set(M):
            failures.append(f"All did not return to the seed's neighbours: {ans} {back[:5]}")
        await rcv.ws.close()
        await ctl.ws.close()

        # ----- 6 (first half). stations to find after a restart ------------- #
        st_, keep_m = await start(port, "close", mood="relax")
        st_, keep_f = await start(port, "close", moodFilter="pump-up")
    finally:
        stop(proc)
        nd_srv.shutdown()
        am_srv.shutdown()

    # ----- 6. restart ------------------------------------------------------- #
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    AM["clap_fail"] = {first_prompt("downbeat"), first_prompt("pump-up")}
    port = _free_port()
    proc = spawn(port, state, am_url, nd_url, extra={"HUB_MOOD_RETRY_S": "60"})
    try:
        await asyncio.to_thread(wait_up, port)
        await asyncio.sleep(2.5)  # the warm build: relax built, pump-up failed
        st_, nm = await acall(port, "POST", "/sonic/station/next", {
            "stationId": keep_m["stationId"], "after": keep_m["seq"], "count": 5})
        if st_ != 200 or nm.get("mood") != "relax" or not nm.get("tracks") \
                or {t["id"] for t in nm["tracks"]} - set(relax):
            failures.append(f"a restart lost the mood: {st_} {nm}")
        st_, nf = await acall(port, "POST", "/sonic/station/next", {
            "stationId": keep_f["stationId"], "after": keep_f["seq"], "count": 5})
        if st_ != 200 or nf.get("moodFilter") != "pump-up" or nf.get("tracks") \
                or nf.get("partial") is not True or nf.get("exhausted"):
            failures.append(f"a filter whose list cannot be had must be partial and empty: "
                            f"{st_} {nf}")
    finally:
        stop(proc)
        unlink_all(state)
        nd_srv.shutdown()
        am_srv.shutdown()


async def old_client_suite(failures):
    """A station started with no mood keys serves what a hub without CLAP serves."""
    results = []
    for clap in (True, False):
        setup_clap()
        AM["clap_on"] = clap
        am_srv, am_url = serve(AudioMuse)
        nd_srv, nd_url = serve(Navidrome)
        port = _free_port()
        state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        proc = spawn(port, state, am_url, nd_url)
        try:
            await asyncio.to_thread(wait_up, port)
            if clap:
                await wait_moods(port)
            else:
                _s, ch = await acall(port, "GET", "/sonic/station/characters")
                if ch.get("moods") is not False or ch.get("filters") != []:
                    failures.append(f"without CLAP the probe offers moods: {ch}")
            st_, a = await start(port, "close")
            served, _l = await refill(port, a, n=3)
            results.append(served)
        finally:
            stop(proc)
            unlink_all(state)
            nd_srv.shutdown()
            am_srv.shutdown()
    if results[0] != results[1] or not results[0]:
        failures.append(f"a station without mood keys changed: {results[0][:6]} vs {results[1][:6]}")


def sanitize_checks(failures):
    base = {"id": "mx_1", "name": "Radio", "kind": "artist", "seedId": "ar-1"}
    rec = hubmod.Hub._sanitize_mix({**base, "moodFilter": "pump-up"})
    if (rec or {}).get("moodFilter") != "pump-up":
        failures.append(f"_sanitize_mix dropped a valid moodFilter: {rec}")
    rec = hubmod.Hub._sanitize_mix({**base, "moodFilter": "nope"})
    if rec is None or "moodFilter" in rec:
        failures.append(f"_sanitize_mix kept moodFilter nope: {rec}")
    if "mood" not in hubmod._STATION_SIGNALS:
        failures.append("mood is not a station signal")
    start = hubmod.STATION_ROUTES[("POST", "/sonic/station/start")]["body"]
    count = hubmod.STATION_ROUTES[("POST", "/sonic/station/count")]["body"]
    if "mood" not in start or "moodFilter" not in start or "moodFilter" not in count:
        failures.append("the station routes do not list mood / moodFilter")


async def main():
    failures: list[str] = []
    sanitize_checks(failures)
    try:
        await engine_suite(failures)
        await old_client_suite(failures)
    finally:
        AM["clap_on"] = False
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("OK: mood source and filter, signals, count, old clients, restart, sanitiser")


if __name__ == "__main__":
    asyncio.run(main())
