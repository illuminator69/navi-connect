#!/usr/bin/env python3
"""
Tests for the custom radio's hub side (PROTOCOL §18.1, P5): Artist variety, twelve
seeds, the read-only count, and the seed-artist rule with several seed artists.

Uses test_station_engine's fake AudioMuse and Navidrome and its synthetic library,
plus one cluster of its own, G: 12 artists x 6 tracks laid out along a line, an
artist's tracks next to each other, so a nearest list walks one artist's songs
before the next artist's. That is the library where the spacing rule decides
everything, and so where Artist variety shows.

Cases:
  1. variety: Low lets an artist return sooner and more often than Medium; High
     never serves one artist twice within 8 tracks; Medium and an absent value
     serve the same ids in the same order; `wild` is 400 bad_variety; the start
     answer echoes it; a restart keeps it;
  2. seeds: 12 ids are accepted, 13 are bad_seed, duplicates collapse, and a
     256 KB body of short ids is refused fast;
  3. the seed-artist rule covers every seed artist: three seed artists on Close
     each reach the seed cap (3), never more, in any 10;
  4. count: the union of two overlapping anchors is smaller than the sum; Close
     <= Balanced <= Explore; an unknown id among three is named in `unknown` and
     the count covers the other two; all unknown is 404 seed_unknown; an
     AudioMuse 500 on one anchor is `partial: true`; a second count of the same
     seeds asks AudioMuse nothing; the route creates no station; with Tier 1 only
     the engine is tier1 and Navidrome is asked for at most 500 per anchor;
  5. the probe advertises count, the varieties and an empty filter list, and the
     catalogue's labels are Familiar / Blend / Discover;
  6. nothing configured: count is 503 fallback;
  7. the body whitelist: count reads `seed` and `character` only (an unlisted
     `variety` is dropped, not obeyed), and start reads `variety`.

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

import test_station_engine as tse  # noqa: E402
from test_station_engine import (AM, ND, AudioMuse, Navidrome, acall, artist_key,  # noqa: E402
                                 check_spacing, serve, spawn, stop, unlink_all,
                                 wait_up)
from test_edits import _free_port  # noqa: E402


def build_line_cluster():
    for a in range(12):
        for t in range(6):
            tse._add(f"G{a:02d}_{t}", f"Line Song {a}-{t}", f"G Artist {a:02d}",
                     f"G Album {a}", -5 + a * 0.01 + t * 0.0012, -5.0,
                     {k: 0.5 for k in tse.FEATS}, "folk:0.55", "folk", "relaxed")


build_line_cluster()


async def run(port, seed_ids, character, variety=None, first=10, refills=4, n=5):
    """A start and some refills: (start answer, every track served)."""
    body = {"seed": {"kind": "tracks", "ids": seed_ids}, "character": character,
            "count": first}
    if variety is not None:
        body["variety"] = variety
    st_, a = await acall(port, "POST", "/sonic/station/start", body)
    if st_ != 200:
        return st_, a, []
    served, seq = list(a["tracks"]), a["seq"]
    for _ in range(refills):
        st2, nx = await acall(port, "POST", "/sonic/station/next", {
            "stationId": a["stationId"], "after": seq, "count": n})
        if st2 != 200:
            break
        served += nx["tracks"]
        seq = nx["seq"]
    return st_, a, served


def most_in_ten(artists):
    best = 0
    for i in range(len(artists)):
        window = artists[max(0, i - 9):i + 1]
        best = max(best, max(window.count(x) for x in window))
    return best


def one_in_ten(artists, a):
    return max((artists[max(0, i - 9):i + 1].count(a) for i in range(len(artists))), default=0)


def nearest_repeat(artists):
    """The smallest number of other tracks between two by one artist (99 = none)."""
    best = 99
    last: dict = {}
    for i, a in enumerate(artists):
        if a in last:
            best = min(best, i - last[a] - 1)
        last[a] = i
    return best


async def engine_suite(failures):
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, am_url, nd_url)
    try:
        await asyncio.to_thread(wait_up, port)

        # ----- 5. the probe ------------------------------------------------- #
        st_, ch = await acall(port, "GET", "/sonic/station/characters")
        if ch.get("count") is not True or ch.get("varieties") != ["low", "medium", "high"] \
                or ch.get("filters") != []:
            failures.append(f"the probe does not advertise count/varieties/filters: {ch}")
        labels = [(c.get("id"), c.get("label"), c.get("legacy"))
                  for c in ch.get("characters", [])]
        if labels != [("close", "Familiar", "EchoMatch"), ("balanced", "Blend", "SteadyVibes"),
                      ("explore", "Discover", "TransitionMaestro")]:
            failures.append(f"the catalogue's words are not Familiar/Blend/Discover: {labels}")

        # ----- 1. variety --------------------------------------------------- #
        g0 = artist_key("G Artist 00")
        res = {}
        for v in ("low", "medium", "high"):
            st_, a, served = await run(port, ["G00_0"], "close", v, first=10, refills=4)
            if st_ != 200:
                failures.append(f"start with variety {v}: {st_} {a}")
                continue
            if a.get("variety") != v:
                failures.append(f"the start answer does not echo variety {v}: {a.get('variety')}")
            res[v] = [artist_key(t["artist"]) for t in served]
        if len(res) == 3:
            low, med, high = res["low"], res["medium"], res["high"]
            if most_in_ten(low) <= 2:
                failures.append(f"Low never let an artist in more than twice in 10: {low}")
            if nearest_repeat(low) >= nearest_repeat(med):
                failures.append(f"Low did not let an artist return sooner than Medium: "
                                f"low {nearest_repeat(low)}, medium {nearest_repeat(med)}")
            bad = check_spacing(high, 8, 1)
            if bad or len(high) < 20:
                failures.append(f"High: {bad[:3]} ({len(high)} served)")
            bad = check_spacing(med, 3, 2, seed_artist=g0, seed_cap=3)
            if bad:
                failures.append(f"Medium broke Close's own numbers: {bad[:3]}")
        # Medium and absent: the same ids in the same order (Close has no alchemy,
        # the one stochastic source, so a difference is the engine's).
        _, _, absent = await run(port, ["L03_0"], "close", None, first=10, refills=4)
        _, _, medium = await run(port, ["L03_0"], "close", "medium", first=10, refills=4)
        if [t["id"] for t in absent] != [t["id"] for t in medium] or not absent:
            failures.append("Medium and an absent variety served different tracks")
        _, _, b_abs = await run(port, ["G03_0"], "balanced", None, first=10, refills=2)
        _, _, b_med = await run(port, ["G03_0"], "balanced", "medium", first=10, refills=2)
        if check_spacing([artist_key(t["artist"]) for t in b_med], 4, 2,
                         seed_artist=artist_key("G Artist 03"), seed_cap=2) or not b_abs:
            failures.append("Balanced with Medium broke Balanced's own numbers")
        st_, bad_v = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L03_0"]}, "variety": "wild"})
        if st_ != 400 or (bad_v or {}).get("code") != "bad_variety":
            failures.append(f"variety 'wild' should be 400 bad_variety: {st_} {bad_v}")

        # ----- 2. seeds ------------------------------------------------------ #
        twelve = [f"L{i:02d}_1" for i in range(12)]
        st_, a12 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "tracks", "ids": twelve}, "count": 5})
        if st_ != 200 or a12.get("seed", {}).get("ids") != twelve:
            failures.append(f"12 seeds were not accepted: {st_} {a12.get('seed')}")
        st_, a13 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "tracks", "ids": twelve + ["L12_1"]}, "count": 5})
        if st_ != 400 or a13.get("code") != "bad_seed":
            failures.append(f"13 seeds should be bad_seed: {st_} {a13}")
        st_, dup = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "tracks", "ids": ["L01_1", "L02_1", "L01_1"]}, "count": 5})
        if st_ != 200 or dup.get("seed", {}).get("ids") != ["L01_1", "L02_1"]:
            failures.append(f"duplicate seeds did not collapse: {st_} {dup.get('seed')}")
        raw = json.dumps({"seed": {"kind": "tracks",
                                   "ids": [f"x{i}" for i in range(40_000)]}}).encode()[:250_000]
        raw = raw.rsplit(b",", 1)[0] + b"]}}"
        t0 = time.monotonic()
        st_, big = await acall(port, "POST", "/sonic/station/start", raw=raw)
        if st_ not in (400, 413) or time.monotonic() - t0 > 1.0:
            failures.append(f"a 250 KB seed body: {st_} in {time.monotonic() - t0:.2f}s")

        # ----- 3. every seed artist is a seed artist -------------------------- #
        _, _, three = await run(port, ["G00_0", "G04_0", "G08_0"], "close", None,
                                first=10, refills=4)
        arts = [artist_key(t["artist"]) for t in three]
        seeds3 = {artist_key(f"G Artist {k:02d}") for k in (0, 4, 8)}
        over = [a for a in seeds3 if one_in_ten(arts, a) > 3]
        reached = [a for a in seeds3 if one_in_ten(arts, a) == 3]
        if over:
            failures.append(f"a seed artist went past the seed cap: {over}")
        if len(reached) < 2:
            # Only the first seed's artist treated as "the seed's" would hold the
            # others to artist_cap (2).
            failures.append(f"the second and third seed artists were held to artist_cap: "
                            f"{sorted(reached)} reached 3 in 10 ({arts})")

        # ----- 4. count ------------------------------------------------------ #
        def count(ids, character="balanced", **extra):
            return acall(port, "POST", "/sonic/station/count",
                         {"seed": {"kind": "tracks", "ids": ids}, "character": character,
                          **extra})
        st_, ca = await count(["L03_0"])
        st_b, cb = await count(["L04_0"])
        st_ab, cab = await count(["L03_0", "L04_0"])
        if st_ != 200 or st_b != 200 or st_ab != 200:
            failures.append(f"count: {st_} {ca} / {st_b} {cb} / {st_ab} {cab}")
        else:
            # Each seed is in the other's list and the union leaves both out: hence -1.
            if not (max(ca["count"], cb["count"]) - 1 <= cab["count"]
                    < ca["count"] + cb["count"]):
                failures.append(f"the union is not a union: {ca['count']} + {cb['count']} "
                                f"-> {cab['count']}")
            # Exactly the fake library's union at Balanced's ceiling, seeds left out.
            want = len({o["id"] for i in ("L03_0", "L04_0")
                        for _d, o in tse._near(i, 400)} - {"L03_0", "L04_0"})
            if cab["count"] != want:
                failures.append(f"the count is {cab['count']}, the library's union {want}")
            if cab.get("engine") != "tier2" or cab.get("anchors") != 2 \
                    or cab.get("unknown") != [] or cab.get("partial") is not False \
                    or cab.get("character") != "balanced":
                failures.append(f"count answer fields: {cab}")
        counts = []
        for c in ("close", "balanced", "explore"):
            _s, r = await count(["L03_0", "B02_1"], c)
            counts.append(r.get("count", -1))
        if not counts[0] <= counts[1] <= counts[2] or counts[0] <= 0:
            failures.append(f"Close <= Balanced <= Explore broken: {counts}")
        _s, one = await count(["L03_0", "L04_0", "ZZ_unknown"])
        if one.get("unknown") != ["ZZ_unknown"] or one.get("count") != cab.get("count") \
                or one.get("anchors") != 2:
            failures.append(f"one unknown of three: {one}")
        st_, none = await count(["ZZ_1", "ZZ_2"])
        if st_ != 404 or none.get("code") != "seed_unknown":
            failures.append(f"all unknown should be 404 seed_unknown: {st_} {none}")
        st_, bc = await acall(port, "POST", "/sonic/station/count",
                              {"seed": {"kind": "tracks", "ids": ["L03_0"]},
                               "character": "wild"})
        if st_ != 400 or bc.get("code") != "bad_character":
            failures.append(f"count with a bad character: {st_} {bc}")
        st_, bs = await acall(port, "POST", "/sonic/station/count", {"seed": {"kind": "x"}})
        if st_ != 400 or bs.get("code") != "bad_seed":
            failures.append(f"count with a bad seed: {st_} {bs}")
        AM["calls"].clear()
        await count(["L03_0", "L04_0"])
        if any(p == "/api/similar_tracks" for p, _q in AM["calls"]):
            failures.append("a second count of the same seeds asked AudioMuse again")
        AM["status"]["L20_0"] = (500, {"error": "boom"})
        st_, part = await count(["L21_0", "L20_0"])
        AM["status"].clear()
        if st_ != 200 or part.get("partial") is not True:
            failures.append(f"an AudioMuse 500 on one anchor must be partial: {st_} {part}")
        # Variety is not a count key: the route does not list it.
        _s, with_v = await count(["L03_0", "L04_0"], variety="high")
        if with_v.get("count") != cab.get("count"):
            failures.append(f"variety changed the count: {with_v} vs {cab}")
    finally:
        stop(proc)
    # The count created no station: the stations file holds only the starts.
    try:
        with open(state + ".stations", encoding="utf-8") as f:
            stations = json.load(f).get("stations", [])
    except FileNotFoundError:
        stations = []
    seeds = [s.get("seed", {}).get("ids") for s in stations]
    if any(ids in (["L03_0", "L04_0"], ["L21_0", "L20_0"], ["L03_0", "B02_1"])
           for ids in seeds):
        failures.append(f"a count left a station behind: {seeds}")

    # ----- 1 (cont). a restart keeps a station's variety --------------------- #
    proc = spawn(port, state, am_url, nd_url)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, a, served = await run(port, ["G06_0"], "close", "high", first=5, refills=0)
        stop(proc)
        proc = spawn(port, state, am_url, nd_url)
        await asyncio.to_thread(wait_up, port)
        seq = a["seq"]
        for _ in range(4):
            st2, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": a["stationId"], "after": seq, "count": 5})
            if st2 != 200:
                failures.append(f"next after a restart: {st2} {nx}")
                break
            served += nx["tracks"]
            seq = nx["seq"]
        bad = check_spacing([artist_key(t["artist"]) for t in served], 8, 1)
        if bad:
            failures.append(f"a restart lost the station's variety: {bad[:3]}")
    finally:
        stop(proc)
        unlink_all(state)
        am_srv.shutdown()
        nd_srv.shutdown()


async def tier1_suite(failures):
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, None, nd_url)
    try:
        await asyncio.to_thread(wait_up, port)
        with ND["lock"]:
            ND["calls"].clear()
        st_, r = await acall(port, "POST", "/sonic/station/count", {
            "seed": {"kind": "tracks", "ids": ["L03_0", "L04_0"]}, "character": "explore"})
        asked = [int(c) for m, _i, c in ND["calls"]
                 if m in ("getSonicSimilarTracks", "getSimilarSongs2") and c]
        if st_ != 200 or r.get("engine") != "tier1" or r.get("count", 0) <= 0:
            failures.append(f"Tier 1 count: {st_} {r}")
        if not asked or max(asked) > 500:
            failures.append(f"Tier 1 count asked Navidrome for {asked}")
    finally:
        stop(proc)
        unlink_all(state)
        nd_srv.shutdown()

    port = _free_port()
    proc = spawn(port, state)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, r = await acall(port, "POST", "/sonic/station/count", {
            "seed": {"kind": "tracks", "ids": ["L03_0"]}})
        if st_ != 503 or not (r or {}).get("fallback"):
            failures.append(f"count with nothing configured: {st_} {r}")
    finally:
        stop(proc)
        unlink_all(state)


async def whitelist_checks(failures):
    import types
    import hub as hubmod
    saved = {k: getattr(hubmod, k) for k in ("TOKEN", "HUB_INSTANCE", "AUDIOMUSE_URL")}
    seen = []

    class _Stations:
        async def route(self, path, body):
            seen.append((path, sorted(body)))
            return 200, {"ok": True}
    try:
        hubmod.TOKEN = "t"
        hubmod.AUDIOMUSE_URL = "http://audiomuse.invalid"
        hubmod.HUB_INSTANCE = types.SimpleNamespace(stations=_Stations())
        cases = {"/sonic/station/count": ({"seed": {"kind": "tracks", "ids": ["a"]},
                                           "character": "close"},
                                          ["variety", "count", "bindSession", "stationId"]),
                 "/sonic/station/start": ({"seed": {"kind": "track", "ids": ["a"]},
                                           "variety": "high"}, ["stationId", "trackId"])}
        for path, (good, bad) in cases.items():
            raw = json.dumps({**good, **{k: 1 for k in bad}}).encode()
            proto = types.SimpleNamespace(reader=asyncio.StreamReader(), body_consumed=False)
            proto.reader.feed_data(raw)
            proto.reader.feed_eof()
            res = await hubmod.STATION.handle(
                proto, path, {"Authorization": "Bearer t", "Content-Length": str(len(raw))},
                "POST")
            if res is None or not seen or seen[-1][0] != path:
                failures.append(f"{path} did not reach the engine: {res}")
                continue
            if set(seen[-1][1]) != set(good):
                failures.append(f"{path} passed {seen[-1][1]}, the route lists {sorted(good)}")
        # The variety table (P5 call 3), at the function.
        w = hubmod._with_variety
        for ch, tune in hubmod.STATION_TUNING.items():
            if w(tune, None) != tune or w(tune, "medium") != tune:
                failures.append(f"{ch}: Medium or absent changed the numbers")
            lo, hi = w(tune, "low"), w(tune, "high")
            if lo.artist_cap != min(3, tune.artist_cap + 1) \
                    or lo.artist_gap != max(1, tune.artist_gap - 2):
                failures.append(f"{ch}: Low is {lo.artist_cap}/{lo.artist_gap}")
            if hi.artist_cap != 1 or hi.seed_artist_cap != 1 \
                    or hi.artist_gap != max(8, tune.artist_gap):
                failures.append(f"{ch}: High is {hi.artist_cap}/{hi.seed_artist_cap}/"
                                f"{hi.artist_gap}")
            if (lo.ceiling, hi.ceiling) != (tune.ceiling, tune.ceiling):
                failures.append(f"{ch}: variety moved the ceiling (it must not move the count)")
    finally:
        for k, v in saved.items():
            setattr(hubmod, k, v)


async def main():
    failures: list[str] = []
    await whitelist_checks(failures)
    await engine_suite(failures)
    await tier1_suite(failures)
    if failures:
        print("FAIL (station count + variety):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - station count + variety: probe-count+varieties+filters/familiar-blend-"
          "discover/low-sooner+more/high-gap-8/medium=absent/bad_variety/variety-echo/"
          "restart-keeps-variety/12-seeds/13-bad_seed/dupes-collapse/250KB-fast/"
          "every-seed-artist-capped/count-union/close<=balanced<=explore/one-unknown/"
          "seed_unknown/bad-character+seed/cached-recount/500-partial/variety-not-a-key/"
          "no-station-left/tier1-500/unconfigured-503/whitelists/variety-table")


if __name__ == "__main__":
    asyncio.run(main())
