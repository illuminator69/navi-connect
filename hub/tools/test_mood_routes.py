#!/usr/bin/env python3
"""
Tests for the moods' hub side (PROTOCOL §18.5, P7 Part A): the catalogue, a tile's
list and "which moods fit this song", through a real hub subprocess against a FAKE
AudioMuse that answers `/api/clap/search` from a dict `prompt -> ranked rows` and
counts every search.

Every prompt in the catalogue gets 700 rows of its own: ids `<p>_<rank>` (p = the
prompt's index in FAKE_PROMPTS), over 300 artists, so the artist cap of 3 leaves
more than 300; plus a few shared songs placed at chosen ranks of chosen prompts.
One prompt ("dominant") is led by 40 songs of one artist, the cap's mutation check.

Cases:
  1. catalogue: 13 moods in display order; ten `home` with four variants, three
     `extra` with none; `filter` exactly the eight; ids are wire slugs, unique;
     titles unique across all 40; hues distinct within a mood; maxSeconds and depth
     as the owner ruled; `version`; AUDIOMUSE_URL unset -> 200 configured:false;
     AudioMuse with CLAP not loaded -> available:false; no token -> 401;
  2. the rated prompts: the warm build searches every mood's four `prompts` (Party's
     rated fourth included) and Party's tile 4 alone searches its own prompt; every
     other tile searches the mood's prompt of the same position;
  3. list: rank order, at most 3 per artist, len == depth (300, sleep 200), ids are
     strings; the same call twice and three concurrent calls cost ONE search;
     mood_unknown / variant_unknown (an `extra` mood has no tile) / bad_request;
     an AudioMuse 500 is 503 {fallback} and the next call searches again; a body
     key `depth` is dropped, not obeyed;
  4. for-song: rank 5 in relax's merged list and 700 in sleep's -> relax only; best
     first; at most 3; an unknown id -> ready with no moods; before the build is
     done -> ready:false; a song only in Party's tile-4 prompt is in no mood;
  5. a failed build: one search of one mood 500s -> ready stays false, the mood is
     not served by `for`, and it is retried no sooner than MOOD_RETRY (2 s here);
  6. the proxy: `/sonic/mood/*` is MoodProxy's (SonicProxy would 404 it), and the
     station probe still answers;
  7. in-process: `_mood_merge` interleaves by rank, keeps the first sighting, caps
     per artist; with the cap at 99 the dominant artist floods the list.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_edits import _free_port  # noqa: E402
from test_station_engine import acall, serve, spawn, stop, unlink_all, wait_up  # noqa: E402

import hub as hubmod  # noqa: E402

# Every prompt the hub can send: the rated ones, then the tiles' own.
FAKE_PROMPTS: list[str] = []
for _m in hubmod.MOODS:
    for _p in _m["prompts"]:
        if _p not in FAKE_PROMPTS:
            FAKE_PROMPTS.append(_p)
    for _t in _m.get("tiles") or ():
        if _t.get("prompt") and _t["prompt"] not in FAKE_PROMPTS:
            FAKE_PROMPTS.append(_t["prompt"])
DOMINANT = "dominant test prompt"
FAKE_PROMPTS.append(DOMINANT)

RELAX = hubmod.MOOD_BY_ID["relax"]["prompts"]
SLEEP = hubmod.MOOD_BY_ID["sleep"]["prompts"]
PARTY = hubmod.MOOD_BY_ID["party"]
PARTY_TILE4 = PARTY["tiles"][3]["prompt"]


def _row(tid, artist, rank):
    return {"item_id": tid, "title": f"Song {tid}", "author": artist,
            "album": f"Album of {artist}", "top_genre": "indie", "top_mood": "relaxed",
            "mood_vector": "indie:0.55,pop:0.53",
            "other_features": "danceable:0.6,aggressive:0.5,happy:0.6,party:0.5,"
                              "relaxed:0.7,sad:0.6",
            "similarity": round(0.5 - rank * 0.0002, 5)}


def build_lists():
    lists: dict[str, list] = {}
    for p_i, prompt in enumerate(FAKE_PROMPTS):
        rows = []
        for rank in range(700):
            if prompt == DOMINANT and rank < 40:
                artist = "Dominant Artist"
            else:
                artist = f"Artist {p_i}-{rank % 300:03d}"
            rows.append(_row(f"p{p_i}_{rank}", artist, rank))
        lists[prompt] = rows

    def place(prompt, rank, tid, artist):
        # rank is 1-based in the merged sense only for the first prompt of a mood
        # (one rank at a time across four lists), so `rank` here is the row index
        # in that ONE prompt's list.
        lists[prompt].insert(rank, _row(tid, artist, rank))
        lists[prompt] = lists[prompt][:700]

    # SHARED_A: index 1 of relax's first prompt -> merged rank 5 (row 1 x 4 lists + 1);
    # deep in sleep's first prompt (index 400 -> merged rank ~1601: past 1000).
    place(RELAX[0], 1, "SHARED_A", "Shared A Artist")
    place(SLEEP[0], 400, "SHARED_A", "Shared A Artist")
    # SHARED_B: in five moods at different depths, so `for` cuts to three, best first.
    for mood_id, idx in (("commute", 30), ("focus", 2), ("upbeat", 10),
                         ("downbeat", 60), ("workout", 120)):
        place(hubmod.MOOD_BY_ID[mood_id]["prompts"][0], idx, "SHARED_B", "Shared B Artist")
    # SHARED_C: index 200 of commute's second prompt -> merged rank 802, inside the
    # 1000 kept but past MOOD_FOR_SONG_RANK: no chip.
    place(hubmod.MOOD_BY_ID["commute"]["prompts"][1], 200, "SHARED_C", "Shared C Artist")
    # TILE4_ONLY: only Party's reworded tile prompt holds it.
    place(PARTY_TILE4, 0, "TILE4_ONLY", "Tile Four Artist")
    return lists


FAKE = {"lists": build_lists(), "searches": [], "fail": set(), "slow": 0.0,
        "stats": {"clap_enabled": True, "loaded": True, "song_count": 15397},
        "lock": threading.Lock()}


class FakeAudioMuse(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if self.headers.get("Authorization") != "Bearer am-token":
            return self._send(401, {"error": "token"})
        if path == "/api/clap/stats":
            return self._send(200, FAKE["stats"])
        if path == "/api/similar_tracks":
            return self._send(404, {"error": "Target track not found in index"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != "Bearer am-token":
            return self._send(401, {"error": "token"})
        if self.path != "/api/clap/search":
            return self._send(404, {"error": "not found"})
        q, limit = body.get("query"), int(body.get("limit") or 100)
        with FAKE["lock"]:
            FAKE["searches"].append((q, limit))
            fail = q in FAKE["fail"]
            slow = FAKE["slow"]
        if slow:
            time.sleep(slow)
        if fail:
            return self._send(500, {"error": "boom"})
        rows = FAKE["lists"].get(q)
        if rows is None:
            return self._send(200, {"query": q, "results": [], "count": 0})
        return self._send(200, {"query": q, "results": rows[:limit], "count": limit})

    def log_message(self, *_a):
        pass


def searches_of(prompt):
    with FAKE["lock"]:
        return sum(1 for q, _l in FAKE["searches"] if q == prompt)


async def wait_ready(port, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        _s, c = await acall(port, "GET", "/sonic/mood/catalogue")
        if isinstance(c, dict) and c.get("ready"):
            return c
        await asyncio.sleep(0.2)
    return None


# --------------------------------------------------------------------------- #
def catalogue_checks(c, failures):
    moods = c.get("moods") or []
    order = [m.get("id") for m in moods]
    want = ["relax", "sleep", "romance", "sad", "energize", "feel-good", "party",
            "workout", "commute", "focus", "pump-up", "upbeat", "downbeat"]
    if order != want:
        failures.append(f"catalogue order: {order}")
    if c.get("version") != hubmod.MOODS_VERSION:
        failures.append(f"catalogue version: {c.get('version')}")
    home = [m for m in moods if m.get("group") == "home"]
    extra = [m for m in moods if m.get("group") == "extra"]
    if len(home) != 10 or any(len(m.get("variants") or []) != 4 for m in home):
        failures.append("not ten home moods with four variants each")
    if [m["id"] for m in extra] != ["pump-up", "upbeat", "downbeat"] or any(
            m.get("variants") for m in extra):
        failures.append(f"extra moods wrong: {[(m['id'], m.get('variants')) for m in extra]}")
    filt = sorted(m["id"] for m in moods if m.get("filter") is True)
    if filt != sorted(["energize", "relax", "feel-good", "sad", "focus", "pump-up",
                       "upbeat", "downbeat"]):
        failures.append(f"filter moods: {filt}")
    ids = [m["id"] for m in moods]
    if len(set(ids)) != len(ids) or not all(re.match(r"^[a-z0-9-]{1,32}$", i) for i in ids):
        failures.append(f"mood ids not unique slugs: {ids}")
    titles = [v["title"] for m in home for v in m["variants"]]
    if len(titles) != 40 or len(set(titles)) != 40:
        failures.append(f"variant titles not unique across 40: {len(set(titles))}")
    for m in home:
        vids = [v["id"] for v in m["variants"]]
        if len(set(vids)) != 4 or not all(re.match(r"^[a-z0-9-]{1,32}$", v) for v in vids):
            failures.append(f"{m['id']}: variant ids {vids}")
        hues = [tuple(v.get("hue") or ()) for v in m["variants"]]
        if len(set(hues)) != 4 or not all(len(h) == 2 and all(isinstance(x, int)
                                                             and 0 <= x < 360 for x in h)
                                          for h in hues):
            failures.append(f"{m['id']}: hues {hues}")
    no_limit = {"sleep", "focus", "workout", "pump-up", "energize"}
    for m in moods:
        want_max = None if m["id"] in no_limit else 480
        if m.get("maxSeconds", "absent") != want_max:
            failures.append(f"{m['id']}: maxSeconds {m.get('maxSeconds')} != {want_max}")
        want_depth = 200 if m["id"] in ("sleep", "focus") else 300
        if m.get("depth") != want_depth:
            failures.append(f"{m['id']}: depth {m.get('depth')}")


async def main_suite(failures):
    am_srv, am_url = serve(FakeAudioMuse)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    # Slow searches, so the warm build is still running when the first asks land.
    FAKE["slow"] = 0.05
    proc = spawn(port, state, am_url, None, extra={"HUB_MOOD_RETRY_S": "2"})
    try:
        await asyncio.to_thread(wait_up, port)

        # ----- 4 (first half). before the build is done ---------------------- #
        st_, f0 = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        if st_ != 200 or f0 != {"ready": False, "moods": []}:
            failures.append(f"for before the build: {st_} {f0}")

        # ----- 6. the proxy ----------------------------------------------- #
        st_, _b = await acall(port, "GET", "/sonic/mood/catalogue", token="wrong")
        if st_ != 401:
            failures.append(f"no token: {st_}")
        st_, ch = await acall(port, "GET", "/sonic/station/characters")
        if st_ != 200 or "characters" not in (ch or {}):
            failures.append(f"the station probe broke: {st_} {ch}")
        st_, nope = await acall(port, "POST", "/sonic/mood/nope", {})
        if st_ != 404 or (nope or {}).get("error") != "unknown route":
            failures.append(f"an unknown mood route: {st_} {nope}")

        # ----- 1. catalogue ------------------------------------------------ #
        st_, c = await acall(port, "GET", "/sonic/mood/catalogue")
        if st_ != 200 or c.get("configured") is not True or c.get("available") is not True:
            failures.append(f"catalogue: {st_} {c and {k: c.get(k) for k in ('configured', 'available', 'ready')}}")
        else:
            catalogue_checks(c, failures)

        c = await wait_ready(port)
        if c is None:
            failures.append("the 13 lists never became ready")
            return
        FAKE["slow"] = 0.0

        # ----- 2. the rated prompts --------------------------------------- #
        for m in hubmod.MOODS:
            for p in m["prompts"]:
                if searches_of(p) != 1:
                    failures.append(f"{m['id']}: rated prompt {p!r} searched "
                                    f"{searches_of(p)} times in the warm build")
        if searches_of(PARTY_TILE4) != 0:
            failures.append("the warm build searched Party's tile-4 prompt")
        with FAKE["lock"]:
            limits = {lim for _q, lim in FAKE["searches"]}
        if limits != {hubmod.MOOD_LIST_RAW}:
            failures.append(f"searches asked limits {limits}")
        for m in hubmod.MOODS:
            for i, t in enumerate(m.get("tiles") or ()):
                with FAKE["lock"]:
                    before = list(FAKE["searches"])
                st_, a = await acall(port, "POST", "/sonic/mood/list",
                                     {"mood": m["id"], "variant": t["id"]})
                with FAKE["lock"]:
                    new = FAKE["searches"][len(before):]
                want_prompt = PARTY_TILE4 if (m["id"], i) == ("party", 3) else m["prompts"][i]
                if st_ != 200:
                    failures.append(f"list {m['id']}/{t['id']}: {st_} {a}")
                    continue
                if (m["id"], i) == ("party", 3):
                    if [q for q, _l in new] != [PARTY_TILE4]:
                        failures.append(f"party tile 4 searched {new}")
                elif new:
                    failures.append(f"{m['id']}/{t['id']} searched again: {new}")
                expect = [r["item_id"] for r in FAKE["lists"][want_prompt]]
                if a["ids"][:5] != expect[:5]:
                    failures.append(f"{m['id']}/{t['id']}: not its prompt's rows: {a['ids'][:5]}")

        # ----- 3. list ------------------------------------------------------ #
        st_, a = await acall(port, "POST", "/sonic/mood/list",
                             {"mood": "relax", "variant": "indie", "depth": 5})
        if st_ != 200:
            failures.append(f"relax/indie: {st_} {a}")
        else:
            rows = FAKE["lists"][RELAX[3]]
            per, want = {}, []
            for r in rows[:hubmod.MOOD_LIST_RAW]:
                ak = hubmod._station_artist_key(r["author"])
                if per.get(ak, 0) < 3:
                    per[ak] = per.get(ak, 0) + 1
                    want.append(r["item_id"])
            if a["ids"] != want[:300] or len(a["ids"]) != 300 or a.get("depth") != 300:
                failures.append(f"relax/indie ids: len {len(a['ids'])}, depth {a.get('depth')} "
                                "(a body `depth` must be dropped, not obeyed)")
            if not all(isinstance(i, str) for i in a["ids"]):
                failures.append("ids are not strings")
            if (a.get("mood"), a.get("variant"), a.get("version")) != (
                    "relax", "indie", hubmod.MOODS_VERSION):
                failures.append(f"list answer keys: {a.get('mood')} {a.get('variant')} {a.get('version')}")
        st_, a = await acall(port, "POST", "/sonic/mood/list",
                             {"mood": "sleep", "variant": "piano"})
        if st_ != 200 or len(a["ids"]) != 200:
            failures.append(f"sleep's tile is not 200 deep: {st_} {len((a or {}).get('ids') or [])}")

        for body, want in (({"mood": "nope", "variant": "x"}, (404, "mood_unknown")),
                           ({"mood": "relax", "variant": "nope"}, (404, "variant_unknown")),
                           ({"mood": "pump-up", "variant": "x"}, (404, "variant_unknown")),
                           ({"mood": "relax"}, (400, "bad_request")),
                           ({"mood": 3, "variant": "indie"}, (400, "bad_request"))):
            st_, a = await acall(port, "POST", "/sonic/mood/list", body)
            if (st_, (a or {}).get("code")) != want:
                failures.append(f"list {body}: {st_} {a} != {want}")

        # Party tile 4 again: cached, no search. Three concurrent on a cold tile: one.
        n4 = searches_of(PARTY_TILE4)
        await acall(port, "POST", "/sonic/mood/list", {"mood": "party", "variant": "pop"})
        if searches_of(PARTY_TILE4) != n4:
            failures.append("the same tile twice searched twice")
        # (Three concurrent asks of a COLD tile: failure_suite, on a fresh hub.)

        # ----- 4. for-song --------------------------------------------------- #
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        if st_ != 200 or f != {"ready": True, "moods": [{"id": "relax", "rank": 5}]}:
            failures.append(f"for SHARED_A: {st_} {f}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_B"})
        got = [m["id"] for m in (f or {}).get("moods") or []]
        ranks = [m["rank"] for m in (f or {}).get("moods") or []]
        if got != ["focus", "upbeat", "commute"] or ranks != sorted(ranks):
            failures.append(f"for SHARED_B (best first, three at most): {f}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "NOT_IN_ANY"})
        if f != {"ready": True, "moods": []}:
            failures.append(f"for an unknown id: {f}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_C"})
        if f != {"ready": True, "moods": []}:
            failures.append(f"a song at merged rank 802 got a chip: {f}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "TILE4_ONLY"})
        if f != {"ready": True, "moods": []}:
            failures.append(f"a song only in Party's tile-4 prompt is in a mood: {f}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "bad id!"})
        if st_ != 400:
            failures.append(f"for a bad id: {st_}")
    finally:
        stop(proc)
        unlink_all(state)
        am_srv.shutdown()
        FAKE["fail"].clear()


async def failure_suite(failures):
    """A fresh hub whose AudioMuse fails one of relax's four searches."""
    am_srv, am_url = serve(FakeAudioMuse)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    with FAKE["lock"]:
        FAKE["searches"].clear()
    FAKE["fail"] = {RELAX[2], PARTY_TILE4}
    proc = spawn(port, state, am_url, None, extra={"HUB_MOOD_RETRY_S": "2"})
    try:
        await asyncio.to_thread(wait_up, port)
        # The warm build runs once: wait for every other prompt to be searched.
        end = time.time() + 15
        while time.time() < end and searches_of(hubmod.MOODS[-1]["prompts"][-1]) < 1:
            await asyncio.sleep(0.1)
        st_, c = await acall(port, "GET", "/sonic/mood/catalogue")
        if c.get("ready") is not False:
            failures.append(f"ready with one of relax's searches failing: {c.get('ready')}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        if f.get("ready") is not False or f.get("moods"):
            failures.append(f"for during a failed build: {f}")
        n = searches_of(RELAX[2])
        # Asked again at once: not searched (inside MOOD_RETRY).
        await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        await asyncio.sleep(0.3)
        if searches_of(RELAX[2]) != n:
            failures.append("a failed mood was retried inside MOOD_RETRY")
        # The tile route has no retry window: a 500 is 503, and the next call asks again.
        st_, a = await acall(port, "POST", "/sonic/mood/list",
                             {"mood": "party", "variant": "pop"})
        if st_ != 503 or (a or {}).get("fallback") is not True:
            failures.append(f"a failing tile search: {st_} {a}")
        before = searches_of(PARTY_TILE4)
        st_, a = await acall(port, "POST", "/sonic/mood/list",
                             {"mood": "party", "variant": "pop"})
        if searches_of(PARTY_TILE4) != before + 1:
            failures.append("a failed tile search was cached")
        # Three concurrent asks of a cold tile cost one search, once it answers.
        FAKE["fail"].discard(PARTY_TILE4)
        FAKE["slow"] = 0.3
        before = searches_of(PARTY_TILE4)
        res = await asyncio.gather(*(acall(port, "POST", "/sonic/mood/list",
                                           {"mood": "party", "variant": "pop"})
                                     for _ in range(3)))
        FAKE["slow"] = 0.0
        if searches_of(PARTY_TILE4) != before + 1 or any(s != 200 for s, _a in res):
            failures.append(f"three concurrent cold asks: {searches_of(PARTY_TILE4) - before} "
                            f"searches, {[s for s, _a in res]}")
        # AudioMuse mends; after MOOD_RETRY the build runs again and becomes ready.
        FAKE["fail"].clear()
        await asyncio.sleep(2.2)
        await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        c = await wait_ready(port, 10)
        if c is None:
            failures.append("never ready after the failing search mended")
        elif searches_of(RELAX[0]) != 1:
            failures.append(f"relax's good searches were asked again: {searches_of(RELAX[0])}")
        st_, f = await acall(port, "POST", "/sonic/mood/for", {"id": "SHARED_A"})
        if f != {"ready": True, "moods": [{"id": "relax", "rank": 5}]}:
            failures.append(f"for after the mend: {f}")
    finally:
        stop(proc)
        unlink_all(state)
        am_srv.shutdown()
        FAKE["fail"].clear()


async def unconfigured_suite(failures):
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, None, None)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, c = await acall(port, "GET", "/sonic/mood/catalogue")
        if st_ != 200 or c.get("configured") is not False or c.get("available") is not False:
            failures.append(f"unconfigured catalogue: {st_} {c}")
        elif len(c.get("moods") or []) != 13:
            failures.append("the unconfigured catalogue lacks the moods")
        st_, a = await acall(port, "POST", "/sonic/mood/list", {"mood": "relax",
                                                               "variant": "indie"})
        if st_ != 503 or (a or {}).get("fallback") is not True:
            failures.append(f"unconfigured list: {st_} {a}")
    finally:
        stop(proc)
        unlink_all(state)

    # CLAP not loaded: configured, not available, and no chips for anyone.
    am_srv, am_url = serve(FakeAudioMuse)
    FAKE["stats"] = {"clap_enabled": True, "loaded": False, "song_count": 0}
    port = _free_port()
    proc = spawn(port, state, am_url, None)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, c = await acall(port, "GET", "/sonic/mood/catalogue")
        if c.get("configured") is not True or c.get("available") is not False:
            failures.append(f"CLAP unloaded: {c.get('configured')} {c.get('available')}")
    finally:
        FAKE["stats"] = {"clap_enabled": True, "loaded": True, "song_count": 15397}
        stop(proc)
        unlink_all(state)
        am_srv.shutdown()


def unit_checks(failures):
    def row(tid, artist):
        return hubmod._station_row({"item_id": tid, "title": tid, "author": artist})
    a = [row("a1", "X"), row("a2", "Y"), row("dup", "Z")]
    b = [row("dup", "Z"), row("b2", "X"), row("b3", "W")]
    got = [r["id"] for r in hubmod._mood_merge([a, b])]
    if got != ["a1", "dup", "a2", "b2", "b3"]:
        failures.append(f"merge order / first sighting: {got}")
    flood = [row(f"d{i}", "Dominant Artist") for i in range(40)] + \
        [row(f"o{i}", f"Other {i}") for i in range(40)]
    capped = hubmod._mood_merge([flood])
    if sum(1 for r in capped if r["ak"] == "dominant artist") != 3:
        failures.append("the artist cap does not hold at 3")
    loose = hubmod._mood_merge([flood], cap=99)
    if sum(1 for r in loose[:20] if r["ak"] == "dominant artist") <= 3:
        failures.append("mutation check: with cap 99 the dominant artist should flood")
    # The catalogue's own invariants, in-process too.
    titles = [t["title"] for m in hubmod.MOODS for t in m.get("tiles") or ()]
    if len(titles) != len(set(titles)):
        failures.append("duplicate tile titles in MOODS")
    for m in hubmod.MOODS:
        if len(m["prompts"]) != 4:
            failures.append(f"{m['id']} has {len(m['prompts'])} prompts")
        for i, t in enumerate(m.get("tiles") or ()):
            if hubmod._mood_prompt(m, i) != (t.get("prompt") or m["prompts"][i]):
                failures.append(f"{m['id']} tile {i}: wrong prompt")
    reworded = [(m["id"], i) for m in hubmod.MOODS for i, t in enumerate(m.get("tiles") or ())
                if t.get("prompt")]
    if reworded != [("party", 3)]:
        failures.append(f"tiles with their own prompt: {reworded}")


async def inprocess_checks(failures):
    """MoodService.merged on its own (the station engine calls it directly): a
    failed build is not asked again inside MOOD_RETRY, and three of four searches
    are never merged."""
    calls: list[str] = []
    fail = {RELAX[1]}

    async def fake_call(route, spec, params, body):
        calls.append(body["query"])
        if body["query"] in fail:
            return 500, b'{"error":"boom"}', "application/json"
        rows = FAKE["lists"][body["query"]][:body["limit"]]
        return 200, json.dumps({"results": rows}).encode(), "application/json"

    real = hubmod.SONIC.call
    old_url, old_retry = hubmod.AUDIOMUSE_URL, hubmod.MOOD_RETRY
    hubmod.SONIC.call, hubmod.AUDIOMUSE_URL, hubmod.MOOD_RETRY = fake_call, "http://x", 60.0
    try:
        svc = hubmod.MoodService(None)
        if await svc.merged("relax") is not None:
            failures.append("merged served three of four searches")
        n = len(calls)
        if await svc.merged("relax") is not None or len(calls) != n:
            failures.append(f"merged asked again inside MOOD_RETRY ({len(calls) - n} searches)")
        hubmod.MOOD_RETRY = 0.0
        fail.clear()
        rows = await svc.merged("relax")
        if not rows or len(rows) != hubmod.MOOD_GATE_DEPTH:
            failures.append(f"merged after the retry window: {rows and len(rows)}")
        if calls.count(RELAX[0]) != 1:
            failures.append("a good search of a failed build was asked again")
        if await svc.merged("nope") is not None:
            failures.append("merged answered an unknown mood")
    finally:
        hubmod.SONIC.call, hubmod.AUDIOMUSE_URL, hubmod.MOOD_RETRY = real, old_url, old_retry


async def main():
    failures: list[str] = []
    unit_checks(failures)
    await inprocess_checks(failures)
    await main_suite(failures)
    await failure_suite(failures)
    await unconfigured_suite(failures)
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("OK: mood catalogue, tile lists, for-song, failed builds, proxy, merge")


if __name__ == "__main__":
    asyncio.run(main())
