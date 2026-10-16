#!/usr/bin/env python3
"""
Tests for the station engine (PROTOCOL §18): `/sonic/station/*` through a real hub
subprocess, against a FAKE AudioMuse and a FAKE Navidrome served from this process.

The fake AudioMuse holds a synthetic library of ~330 tracks in clusters on a
plane. `similar_tracks` answers like the real one with `radius_similarity=false`:
nearest first, with distances, the six features and the tag list as the "k:v,..."
STRINGS the live server sends. It answers only the tracks within NEIGHBOURHOOD of
the item, which stands in for a 15,768-song library where everything else is
further away than any list reaches.

  L  200 indie tracks, 40 artists, round the origin   (the seeds' neighbourhood)
     + 4 versions of one song ("Echo Song", remaster, live, feat.)
  B   60 electronic tracks at x≈0.32                  (partly inside L's reach)
  C   50 aggressive hip-hop tracks at x≈0.68          (reachable ONLY from B)
  N   16 tracks far away, 16 artists                  (a 15-track neighbourhood)

Alchemy and similar_artists deliberately answer C tracks as lures, so the gate is
what keeps them out. The fake Navidrome counts every method it is asked, and the
test asserts `getRandomSongs` is never one of them.

Cases:
  1. the characters probe: configured, available, tier2, the catalogue text;
  2. 30 refills of 5 repeat no id and no title key, keep the artist spacing, and
     never serve a seed or a C track (Balanced, with alchemy's lures in play);
  3. Close serves within rank 150 while candidates there remain, and C never
     appears on Close or Balanced even after a played-through track and a like;
     Explore serves no C before a play-through, and drifts (`why: drift`) from
     the neighbours of a track it served once that track is played through;
  4. a 15-track neighbourhood yields 15 then `exhausted` — and an AudioMuse 500
     on the ladder's deeper ask is `partial`, never `exhausted`;
  5. `after`: a same-device retry replays, another device's stale `after` is
     `servedElsewhere`, and only `after == seq` computes;
  6. `bindSession`: binds the session; a second start for it is 409 session_bound;
  7. unlisted body keys are dropped (never forwarded, never obeyed);
  8. a restart keeps the station and the play history: a new station from the
     same seed serves nothing played, by id or by title key;
  9. AudioMuse down: engine tier1 through getSonicSimilarTracks, and through
     getSimilarSongs2 when Navidrome has no sonicSimilarity;
 10. nothing configured: the probe answers configured:false, start 503 fallback;
 11. the deepest list asked for stays under PROXY_MAX_RESPONSE;
 12. the 12 s budget runs from a request's arrival: a third start queued behind
     two slow ones still answers inside it, `partial`.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HUB_DIR = os.path.join(HERE, "..")
sys.path.insert(0, HUB_DIR)
sys.path.insert(0, HERE)

from test_edits import TOKEN, Client, _free_port  # noqa: E402

FEATS = ("danceable", "aggressive", "happy", "party", "relaxed", "sad")
NEIGHBOURHOOD = 0.3
ND_USER, ND_PASS = "station-test-user", "station-test-pass-9x7"
# A live row, measured 2026-10-16: 1000 rows of similar_tracks = 405,202 bytes.
LIVE_ROW_BYTES = 406

# --------------------------------------------------------------------------- #
# The synthetic library
# --------------------------------------------------------------------------- #
LIB: dict[str, dict] = {}


def _add(tid, title, artist, album, x, y, feats, tags, genre, mood, dur=200):
    LIB[tid] = {"id": tid, "title": title, "artist": artist, "album": album,
                "x": x, "y": y, "feats": feats, "tags": tags, "genre": genre,
                "mood": mood, "duration": dur}


def _feats(rng, base):
    return {k: round(min(0.99, max(0.01, base[i] + rng.uniform(-0.04, 0.04))), 2)
            for i, k in enumerate(FEATS)}


def build_library():
    rng = random.Random(1609)
    indie = (0.62, 0.55, 0.60, 0.58, 0.68, 0.64)
    for a in range(40):
        for t in range(5):
            r, th = 0.12 * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
            _add(f"L{a:02d}_{t}", f"Indie Song {a}-{t}", f"L Artist {a:02d}",
                 f"L Album {a}", r * math.cos(th), r * math.sin(th),
                 _feats(rng, indie), "female vocalists:0.561,indie:0.549,pop:0.53",
                 "indie", "relaxed")
    for i, title in enumerate(("Echo Song", "Echo Song (Remastered 2011)",
                               "Echo Song - Live at Wembley", "Echo Song feat. Someone")):
        _add(f"V{i}", title, "Version Band", "Echoes", 0.01 * i, 0.02,
             _feats(rng, indie), "indie:0.55,female vocalists:0.54", "indie", "relaxed")
    elec = (0.64, 0.57, 0.61, 0.60, 0.64, 0.61)   # compressed, as on the live library
    for a in range(20):
        for t in range(3):
            _add(f"B{a:02d}_{t}", f"Pulse {a}-{t}", f"B Artist {a:02d}", f"B Album {a}",
                 0.32 + rng.uniform(-0.06, 0.06), rng.uniform(-0.05, 0.05),
                 _feats(rng, elec), "electronic:0.56,dance:0.54", "electronic", "party")
    # Compressed features, as on the live library: C sits within ~0.08 of L on
    # every one of the six (Lana Del Rey -> OsamaSon measured 0.078 mean), so
    # only the neighbourhood gate can keep it out — smoothing would welcome it.
    aggro = (0.64, 0.61, 0.58, 0.60, 0.64, 0.62)
    for a in range(10):
        for t in range(5):
            _add(f"C{a:02d}_{t}", f"Rage {a}-{t}", f"Aggro {a}", f"Aggro Tape {a}",
                 0.68 + rng.uniform(-0.05, 0.05), rng.uniform(-0.05, 0.05),
                 _feats(rng, aggro), "Hip-Hop:0.61,rap:0.58", "hip-hop", "aggressive")
    for i in range(16):
        _add(f"N{i:02d}", f"Island {i}", f"Islander {i:02d}", "Island",
             5 + rng.uniform(-0.05, 0.05), 5 + rng.uniform(-0.05, 0.05),
             _feats(rng, indie), "ambient:0.55", "ambient", "relaxed")


build_library()


def _dist(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _row(t, d):
    return {"item_id": t["id"], "title": t["title"], "author": t["artist"],
            "album": t["album"], "album_artist": t["artist"], "distance": round(d, 6),
            "mood_vector": t["tags"] + ",chillout:0.528,jazz:0.519,fun:0.51",
            "other_features": ",".join(f"{k}:{v:.2f}" for k, v in t["feats"].items()),
            "top_genre": t["genre"], "top_mood": t["mood"]}


def _near(item, n, radius=NEIGHBOURHOOD):
    t = LIB[item]
    rows = sorted(((_dist(t, o), o) for o in LIB.values() if o["id"] != item),
                  key=lambda x: (x[0], x[1]["id"]))
    return [(d, o) for d, o in rows if d <= radius][:n]


# --------------------------------------------------------------------------- #
# Fake AudioMuse
# --------------------------------------------------------------------------- #
AM = {"calls": [], "fail_min_n": None, "max_body": 0, "max_n": 0, "creds_seen": 0,
      "slow": {}, "lock": threading.Lock()}


class AudioMuse(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        with AM["lock"]:
            AM["max_body"] = max(AM["max_body"], len(body))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        with AM["lock"]:
            AM["calls"].append((path, q))
        if self.headers.get("Authorization") != "Bearer am-token":
            return self._send(401, {"error": "token"})
        if path == "/api/similar_tracks":
            item, n = q.get("item_id", ""), int(q.get("n", "10"))
            if item in AM["slow"]:
                time.sleep(AM["slow"][item])
            with AM["lock"]:
                AM["max_n"] = max(AM["max_n"], n)
            if AM["fail_min_n"] is not None and n >= AM["fail_min_n"]:
                return self._send(500, {"error": "boom"})
            if item not in LIB:
                return self._send(404, {"error": "Target track not found in index"})
            return self._send(200, [_row(o, d) for d, o in _near(item, n)])
        if path == "/api/sonic_fingerprint/generate":
            if q.get("navidrome_user") == ND_USER and q.get("navidrome_password") == ND_PASS:
                with AM["lock"]:
                    AM["creds_seen"] += 1
            n = int(q.get("n", "10"))
            top = [LIB["L05_0"], LIB["L06_0"], LIB["L07_0"]]
            rows = [_row(t, 0.0) for t in top]
            rows += [_row(o, d + 0.05) for d, o in _near("L05_0", n - 3)
                     if o["id"] not in {t["id"] for t in top}]
            return self._send(200, rows[:n])
        if path == "/api/similar_artists":
            # Lures: an aggressive artist first, then an electronic one.
            return self._send(200, [
                {"artist": "Aggro 0", "artist_id": "a0", "component_matches": [
                    {"artist2_representative_songs": [
                        {"item_id": f"C00_{i}", "title": LIB[f"C00_{i}"]["title"]}
                        for i in range(3)]}]},
                {"artist": "B Artist 01", "artist_id": "b1", "component_matches": [
                    {"artist2_representative_songs": [
                        {"item_id": f"B01_{i}", "title": LIB[f"B01_{i}"]["title"]}
                        for i in range(3)]}]}])
        if path == "/external/get_score":
            t = LIB.get(q.get("id", ""))
            if t is None:
                return self._send(404, {"error": "Score not found"})
            row = _row(t, 0.0)
            row.pop("distance")
            row["duration"] = float(t["duration"])
            return self._send(200, row)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with AM["lock"]:
            AM["calls"].append((self.path, body))
        if self.path == "/api/alchemy":
            adds = [LIB[i["id"]] for i in body.get("items", [])
                    if i.get("op") == "add" and i.get("id") in LIB]
            if not adds:
                return self._send(200, {"results": []})
            cx = sum(t["x"] for t in adds) / len(adds)
            cy = sum(t["y"] for t in adds) / len(adds)
            c = {"x": cx, "y": cy}
            rows = sorted(((_dist(c, o), o) for o in LIB.values()), key=lambda x: x[0])
            # Stochastic, as the real one is: 20 of the 80 nearest the centroid.
            pick = random.Random(len(AM["calls"])).sample(rows[:80], 20)
            out = [_row(o, d) for d, o in sorted(pick, key=lambda x: x[0])]
            # The lure: aggressive tracks a careless engine would serve.
            out += [_row(LIB[f"C0{a}_0"], 0.05) for a in range(5)]
            return self._send(200, {"results": out})
        return self._send(404, {"error": "not found"})

    def log_message(self, *_a):
        pass


# --------------------------------------------------------------------------- #
# Fake Navidrome
# --------------------------------------------------------------------------- #
ND = {"methods": {}, "sonic": True, "lock": threading.Lock()}


def _song(t):
    return {"id": t["id"], "title": t["title"], "artist": t["artist"],
            "album": t["album"], "duration": t["duration"], "genre": t["genre"],
            "path": f"/music/{t['artist']}/{t['title']}.flac"}


class Navidrome(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload):
        body = json.dumps({"subsonic-response": {"status": "ok", "version": "1.16.1",
                                                 **payload}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message):
        body = json.dumps({"subsonic-response": {"status": "failed", "version": "1.16.1",
                                                 "error": {"code": code,
                                                           "message": message}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        method = path.rsplit("/", 1)[-1].replace(".view", "")
        with ND["lock"]:
            ND["methods"][method] = ND["methods"].get(method, 0) + 1
        if q.get("u") != ND_USER or not q.get("t") or not q.get("s"):
            return self._fail(40, "wrong username or password")
        if method == "ping":
            return self._send({})
        if method == "getOpenSubsonicExtensions":
            ext = [{"name": "formPost", "versions": [1]}]
            if ND["sonic"]:
                ext.append({"name": "sonicSimilarity", "versions": [1]})
            return self._send({"openSubsonicExtensions": ext})
        tid = q.get("id", "")
        if method == "getSong":
            if tid not in LIB:
                return self._fail(70, "data not found")
            return self._send({"song": _song(LIB[tid])})
        if method == "getSonicSimilarTracks":
            if not ND["sonic"]:
                return self._fail(0, "not supported")
            if tid not in LIB:
                return self._fail(70, "data not found")
            rows = _near(tid, int(q.get("count", "10")))
            return self._send({"sonicMatch": [{"entry": _song(o), "similarity": 1 - d}
                                              for d, o in rows]})
        if method == "getSimilarSongs2":
            if tid not in LIB:
                return self._fail(70, "data not found")
            rows = _near(tid, int(q.get("count", "10")))
            return self._send({"similarSongs2": {"song": [_song(o) for _d, o in rows]}})
        if method == "getRandomSongs":
            # Answered as Navidrome would, so a hub that asked would be USING it.
            return self._send({"randomSongs": {"song": [_song(o) for o in
                                                        list(LIB.values())[:10]]}})
        return self._fail(0, f"unknown method {method}")

    def log_message(self, *_a):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve(handler):
    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# --------------------------------------------------------------------------- #
# The hub under test, and a client's way of calling it
# --------------------------------------------------------------------------- #
def unlink_all(path):
    for suffix in ("", ".bak", ".history", ".history.tmp", ".tmp"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def spawn(port, state, audiomuse=None, navidrome=None, extra=None, log_path=None):
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_HEALTH_PORT": str(_free_port())}
    for k in ("AUDIOMUSE_URL", "AUDIOMUSE_TOKEN", "NAVIDROME_URL", "HUB_ND_USER",
              "HUB_ND_PASS", "LBBOT_URL", "PREVIEW_URL"):
        env.pop(k, None)
    if audiomuse:
        env.update(AUDIOMUSE_URL=audiomuse, AUDIOMUSE_TOKEN="am-token")
    if navidrome:
        env["NAVIDROME_URL"] = navidrome
    if audiomuse or navidrome:
        env.update(HUB_ND_USER=ND_USER, HUB_ND_PASS=ND_PASS)
    env.update(extra or {})
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                            stdout=out, stderr=subprocess.STDOUT)
    return proc


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_up(port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"hub on {port} did not come up")


def http_call(port, method, path, body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None
                                        else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


async def acall(port, method, path, body=None, **kw):
    return await asyncio.to_thread(http_call, port, method, path, body, **kw)


def title_key(title, artist):
    import hub as hubmod
    return hubmod._station_title_key(title, artist)


def check_spacing(seq_artists, gap, cap, seed_artist=None, seed_cap=None):
    """Every artist at least `gap` tracks apart, and at most `cap` in any 10."""
    bad = []
    for i, a in enumerate(seq_artists):
        if a in seq_artists[max(0, i - gap):i]:
            bad.append(f"{a} again within {gap} at position {i}")
        window = seq_artists[max(0, i - 9):i + 1]
        limit = seed_cap if (seed_artist and a == seed_artist and seed_cap) else cap
        if window.count(a) > limit:
            bad.append(f"{a} {window.count(a)}x in 10 at position {i}")
    return bad


def artist_key(name):
    import hub as hubmod
    return hubmod._station_artist_key(name)


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
async def main_suite(failures):
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    log_path = state + ".log"
    proc = spawn(port, state, am_url, nd_url, log_path=log_path)
    try:
        await asyncio.to_thread(wait_up, port)

        # ----- 1. the probe ------------------------------------------------- #
        st_, ch = await acall(port, "GET", "/sonic/station/characters")
        if st_ != 200 or not ch or ch.get("configured") is not True \
                or ch.get("available") is not True or ch.get("engine") != "tier2":
            failures.append(f"characters probe: {st_} {ch}")
        want = [("close", "Close", "EchoMatch", "Stays right next to what you started with."),
                ("balanced", "Balanced", "SteadyVibes",
                 "Near what you started with, with room to move."),
                ("explore", "Explore", "TransitionMaestro",
                 "Wanders further, steered by what you skip and like.")]
        got = [(c.get("id"), c.get("label"), c.get("legacy"), c.get("description"))
               for c in (ch or {}).get("characters", [])]
        if got != want or (ch or {}).get("default") != "balanced":
            failures.append(f"the catalogue is not §18.1's: {got}")
        st_, _ = await acall(port, "GET", "/sonic/station/characters", token="wrong")
        if st_ != 401:
            failures.append(f"a wrong token must be 401, got {st_}")

        # ----- 2. 30 refills of 5 on Balanced ------------------------------- #
        seed = "L03_0"
        st_, a = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [seed], "name": "Indie Song 3-0"},
            "character": "balanced", "count": 5, "deviceId": "dev-a"})
        if st_ != 200:
            failures.append(f"start failed: {st_} {a}")
            return
        sid = a["stationId"]
        if not re.fullmatch(r"st_\d+_[0-9a-f]{4}", sid):
            failures.append(f"stationId shape: {sid}")
        if a.get("name") != "Indie Song 3-0 radio" or a.get("seed", {}).get("ids") != [seed]:
            failures.append(f"name/seed echo: {a.get('name')} {a.get('seed')}")
        if a.get("origin") != "radio" or a.get("bound") is not False:
            failures.append(f"a start without origin or bindSession: origin={a.get('origin')} "
                            f"bound={a.get('bound')}")
        served = list(a["tracks"])
        seq = a["seq"]
        for _ in range(30):
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": sid, "after": seq, "count": 5, "deviceId": "dev-a"})
            if st_ != 200:
                failures.append(f"next failed: {st_} {nx}")
                break
            if len(nx["tracks"]) != 5:
                failures.append(f"a refill came back with {len(nx['tracks'])} "
                                f"(exhausted={nx['exhausted']} partial={nx['partial']})")
            served += nx["tracks"]
            seq = nx["seq"]
        ids = [t["id"] for t in served]
        if len(ids) != len(set(ids)):
            failures.append("an id was served twice")
        keys = [title_key(t["title"], t["artist"]) for t in served]
        if len(keys) != len(set(keys)):
            failures.append("a title key (a version of one song) was served twice")
        if seed in ids:
            failures.append("the seed was served")
        if any(i.startswith("C") for i in ids):
            failures.append(f"a C track (second hop / lure) was served on Balanced: "
                            f"{[i for i in ids if i.startswith('C')]}")
        if seq != len(served):
            failures.append(f"seq {seq} != tracks served {len(served)}")
        bad = check_spacing([artist_key(LIB[seed]["artist"])]
                            + [artist_key(t["artist"]) for t in served], gap=4, cap=2)
        if bad:
            failures.append(f"Balanced spacing broken: {bad[:3]}")
        whys = {t["why"] for t in served}
        if not whys <= {"near", "blend"}:
            failures.append(f"Balanced served a why outside near/blend: {whys}")
        if "blend" not in whys:
            failures.append("Balanced never used alchemy (every other batch)")
        alch = [c for c in AM["calls"] if c[0] == "/api/alchemy"]
        if not alch:
            failures.append("the fake alchemy was never asked, so its lures were not tested")
        elif any(not all(i.get("type") == "song" for i in c[1]["items"]) for c in alch):
            failures.append("alchemy items must carry type: song")

        # ----- 3. Close: the rank ceiling, and C never, after a play and a like - #
        st_, c0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L10_0"]}, "character": "close",
            "count": 10, "deviceId": "dev-a"})
        csid, cseq, cserved = c0["stationId"], c0["seq"], list(c0["tracks"])
        # A played-through track and a like, on L tracks (signals on an UNBOUND
        # station apply directly).
        for kind in ("played", "like"):
            st_, sg = await acall(port, "POST", "/sonic/station/signal", {
                "stationId": csid, "trackId": cserved[0]["id"], "kind": kind})
            if st_ != 200 or not sg.get("applied"):
                failures.append(f"signal {kind} on an unbound station: {st_} {sg}")
        for _ in range(4):
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": csid, "after": cseq, "count": 10, "deviceId": "dev-a"})
            cserved += nx["tracks"]
            cseq = nx["seq"]
        if any(t["rank"] > 150 for t in cserved):
            failures.append(f"Close served past rank 150 with candidates left: "
                            f"{sorted(t['rank'] for t in cserved)[-3:]}")
        if any(t["id"].startswith("C") for t in cserved):
            failures.append("a C track was served on Close")
        if [t["why"] for t in cserved if t["why"] != "near"]:
            failures.append("Close served something other than its nearest lists")
        bad = check_spacing([artist_key("L Artist 10")]
                            + [artist_key(t["artist"]) for t in cserved], gap=3, cap=2,
                            seed_artist=artist_key("L Artist 10"), seed_cap=3)
        if bad:
            failures.append(f"Close spacing broken: {bad[:3]}")
        st_, sg = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": csid, "trackId": "C00_0", "kind": "like"})
        if sg.get("ignored") != "unknown_track":
            failures.append(f"a like of a track never served: {sg}")
        st_, sg = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": csid, "kind": "character", "value": "sideways"})
        if st_ != 400 or sg.get("code") != "bad_character":
            failures.append(f"a bad character value: {st_} {sg}")

        # Explore: nothing beyond the anchor gate until a played-through track
        # opens its own neighbourhood (two hops, no more).
        # Seeded at L's far edge, so B is outside the seed's reach: the similar-artist
        # lures (B01, C00) and the alchemy lures (C) must all stay out.
        edge = min((t for t in LIB.values() if t["id"].startswith("L")),
                   key=lambda t: t["x"])["id"]
        st_, x0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [edge]}, "character": "explore",
            "count": 10, "deviceId": "dev-a"})
        xsid, xseq, xserved = x0["stationId"], x0["seq"], list(x0["tracks"])
        for _ in range(3):
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": xsid, "after": xseq, "count": 10, "deviceId": "dev-a"})
            xserved += nx["tracks"]
            xseq = nx["seq"]
        if any(t["id"][0] in "BC" for t in xserved):
            failures.append("Explore served past the seed's reach before anything was "
                            "played through")
        if any(t["why"] == "drift" for t in xserved):
            failures.append("Explore drifted before anything was played through")
        # Play through the served tracks furthest towards B, then drift appears.
        towards = sorted(xserved, key=lambda t: -LIB[t["id"]]["x"])[:3]
        for t in towards:
            await acall(port, "POST", "/sonic/station/signal", {
                "stationId": xsid, "trackId": t["id"], "kind": "played"})
        drift = []
        for _ in range(4):
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": xsid, "after": xseq, "count": 10, "deviceId": "dev-a"})
            xseq = nx["seq"]
            drift += [t for t in nx["tracks"] if t["why"] == "drift"]
            if any(t["why"] == "drift" and t["rank"] > 200 for t in nx["tracks"]):
                failures.append("a drift track outside the 200 of its played-through track")
        if not drift:
            failures.append("Explore never drifted from a played-through track")

        # ----- 4. a 15-track neighbourhood, and a 500 on the ladder ---------- #
        st_, n0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["N00"]}, "character": "close",
            "count": 5, "deviceId": "dev-a"})
        nsid, nseq, nserved = n0["stationId"], n0["seq"], list(n0["tracks"])
        for _ in range(2):
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": nsid, "after": nseq, "count": 5, "deviceId": "dev-a"})
            nserved += nx["tracks"]
            nseq = nx["seq"]
        if len(nserved) != 15 or len({t["id"] for t in nserved}) != 15:
            failures.append(f"the 15-track neighbourhood yielded {len(nserved)}")
        AM["fail_min_n"] = 400   # the ladder's deeper ask fails
        st_, nx = await acall(port, "POST", "/sonic/station/next", {
            "stationId": nsid, "after": nseq, "count": 5, "deviceId": "dev-a"})
        if nx.get("exhausted") or not nx.get("partial") or nx.get("tracks"):
            failures.append(f"a 500 mid-ladder must be partial, never exhausted: "
                            f"exhausted={nx.get('exhausted')} partial={nx.get('partial')}")
        AM["fail_min_n"] = None
        st_, nx = await acall(port, "POST", "/sonic/station/next", {
            "stationId": nsid, "after": nx["seq"], "count": 5, "deviceId": "dev-a"})
        if nx.get("exhausted") is not True or nx.get("tracks") or nx.get("partial"):
            failures.append(f"an answered, empty ladder must be exhausted: {nx}")

        # The same on Explore, which widens to the deepest list there is (case 11).
        st_, e0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["N01"]}, "character": "explore",
            "count": 20, "deviceId": "dev-a"})
        st_, ex = await acall(port, "POST", "/sonic/station/next", {
            "stationId": e0["stationId"], "after": e0["seq"], "count": 5,
            "deviceId": "dev-a"})
        if not ex.get("exhausted"):
            failures.append(f"Explore on 15 tracks should end exhausted: {ex}")

        # ----- 5. after: replay, servedElsewhere ---------------------------- #
        st_, r0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L20_0"]}, "count": 5, "deviceId": "dev-a"})
        rsid = r0["stationId"]
        st_, r1 = await acall(port, "POST", "/sonic/station/next", {
            "stationId": rsid, "after": 5, "count": 5, "deviceId": "dev-a"})
        st_, r1b = await acall(port, "POST", "/sonic/station/next", {
            "stationId": rsid, "after": 5, "count": 5, "deviceId": "dev-a"})
        if [t["id"] for t in r1b["tracks"]] != [t["id"] for t in r1["tracks"]] \
                or r1b["seq"] != 10:
            failures.append(f"a same-device retry did not replay: {r1['seq']} {r1b['seq']}")
        st_, r2 = await acall(port, "POST", "/sonic/station/next", {
            "stationId": rsid, "after": 5, "count": 5, "deviceId": "dev-b"})
        if r2.get("servedElsewhere") is not True or r2.get("tracks") or r2.get("seq") != 10:
            failures.append(f"another device's stale after: {r2}")
        st_, r3 = await acall(port, "POST", "/sonic/station/next", {
            "stationId": rsid, "after": 10, "count": 5, "deviceId": "dev-b"})
        if len(r3["tracks"]) != 5 or r3["seq"] != 15:
            failures.append(f"after == seq must compute: {r3}")
        st_, r4 = await acall(port, "POST", "/sonic/station/next", {
            "stationId": rsid, "after": 10, "count": 5, "deviceId": "dev-a"})
        if r4.get("servedElsewhere") is not True:
            failures.append(f"dev-a's after 10 after dev-b served 10->15: {r4}")
        st_, r5 = await acall(port, "POST", "/sonic/station/next", {
            "stationId": "st_0_dead", "after": 0})
        if st_ != 404 or r5.get("code") != "station_unknown":
            failures.append(f"an unknown station: {st_} {r5}")
        st_, r6 = await acall(port, "POST", "/sonic/station/next", {"stationId": rsid})
        if st_ != 400:
            failures.append(f"next without after: {st_} {r6}")

        # ----- 6. bindSession and session_bound ----------------------------- #
        cl = Client("Binder", device_id="binder")
        await cl.connect(f"ws://127.0.0.1:{port}")
        await cl.act(action="setQueue", tracks=[{"id": "L30_0", "title": "x",
                                                  "durationMs": 200000}],
                     index=0, play=True, savedQueueId="q_bind_test")
        await asyncio.sleep(0.4)
        st_, b1 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "tracks", "ids": ["L30_0", "L31_0"]},
            "bindSession": "q_bind_test", "count": 5, "deviceId": "binder"})
        if st_ != 200 or b1.get("bound") is not True:
            failures.append(f"a bound start failed: {st_} {b1.get('bound')}")
        st_, b2 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L32_0"]},
            "bindSession": "q_bind_test", "count": 5, "deviceId": "other"})
        if st_ != 409 or b2.get("code") != "session_bound" \
                or b2.get("stationId") != b1.get("stationId"):
            failures.append(f"a second bound start must be 409 session_bound: {st_} {b2}")
        st_, b3 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L32_0"]},
            "bindSession": "q_some_other", "count": 5, "origin": "continue"})
        if st_ != 200 or b3.get("bound") is not False:
            failures.append(f"a bindSession naming another queue must still start, "
                            f"bound:false: {st_} {b3.get('bound')}")
        if b3.get("origin") != "continue":
            failures.append(f"origin continue not echoed by start: {b3.get('origin')}")
        st_, b3n = await acall(port, "POST", "/sonic/station/next", {
            "stationId": b3["stationId"], "after": b3["seq"], "count": 2})
        if b3n.get("origin") != "continue":
            failures.append(f"origin continue not echoed by next: {b3n.get('origin')}")
        st_, b4 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L34_0"]}, "count": 2, "origin": "sideways"})
        if b4.get("origin") != "radio":
            failures.append(f"an unknown origin must read as radio: {b4.get('origin')}")
        st_, sg = await acall(port, "POST", "/sonic/station/signal", {
            "stationId": b1["stationId"], "trackId": b1["tracks"][0]["id"],
            "kind": "skip"})
        if sg.get("ignored") != "session":
            failures.append(f"a skip signal for a bound station: {sg}")
        await cl.ws.close()

        # ----- 7. unlisted body keys are dropped ---------------------------- #
        AM["calls"].clear()
        st_, k0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L33_0"]}, "count": 3,
            "stationId": "st_evil", "seq": 999, "n": 5000, "temperature": 9,
            "navidrome_password": "x"})
        if st_ != 200 or k0["stationId"] == "st_evil" or k0["seq"] != 3:
            failures.append(f"an unlisted key was obeyed: {k0.get('stationId')} {k0.get('seq')}")
        for path, q in AM["calls"]:
            if (isinstance(q, dict) and (str(q.get("n")) == "5000"
                                         or q.get("temperature") == 9)):
                failures.append(f"an unlisted key reached AudioMuse: {path} {q}")
        st_, bad = await acall(port, "POST", "/sonic/station/start",
                               {"seed": {"kind": "artist", "ids": ["x"]}})
        if st_ != 400 or bad.get("code") != "bad_seed":
            failures.append(f"an artist seed must be 400 bad_seed: {st_} {bad}")
        st_, bad = await acall(port, "POST", "/sonic/station/start",
                               {"seed": {"kind": "track", "ids": ["NOPE_1"]}})
        if st_ != 404 or bad.get("code") != "seed_unknown":
            failures.append(f"an unknown seed must be 404 seed_unknown: {st_} {bad}")
        st_, bad = await acall(port, "POST", "/sonic/station/start",
                               {"seed": {"kind": "track", "ids": ["L01_0"]},
                                "character": "wild"})
        if st_ != 400 or bad.get("code") != "bad_character":
            failures.append(f"a bad character must be 400: {st_} {bad}")

        # Your mix: the fingerprint's top rows are served first, the hub injects
        # the Navidrome creds (the client sends none).
        st_, ym = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "yourMix"}, "count": 6})
        if st_ != 200 or ym.get("name") != "Your mix":
            failures.append(f"Your mix: {st_} {ym}")
        elif {t["id"] for t in ym["tracks"][:3]} != {"L05_0", "L06_0", "L07_0"}:
            failures.append(f"Your mix did not open with its anchors: "
                            f"{[t['id'] for t in ym['tracks']]}")
        if AM["creds_seen"] < 1:
            failures.append("the fingerprint call did not carry the hub's Navidrome creds")

        # The cap: more than STATION_MAX stations were started since station 2 was
        # last used, so it is the one evicted.
        st_, ev = await acall(port, "POST", "/sonic/station/next", {
            "stationId": sid, "after": seq, "count": 5, "deviceId": "dev-a"})
        if st_ != 404 or ev.get("code") != "station_unknown":
            failures.append(f"the least recently used station should be evicted: {st_}")

        # ----- 12. the budget runs from arrival ------------------------------ #
        AM["slow"].update({"L35_0": 7.0, "L36_0": 7.0, "L37_0": 7.0})
        t0 = time.monotonic()
        res = await asyncio.gather(*(
            acall(port, "POST", "/sonic/station/start", {
                "seed": {"kind": "track", "ids": [i]}, "count": 5})
            for i in ("L35_0", "L36_0", "L37_0")))
        took = time.monotonic() - t0
        AM["slow"].clear()
        if took > 13.5:
            failures.append(f"three starts behind a slow AudioMuse took {took:.1f}s: the "
                            "12 s budget must run from arrival, not from a computing slot")
        late = [b for st_, b in res if st_ != 200 or b.get("partial")]
        if not late:
            failures.append(f"one of three slow starts should have run out of budget: "
                            f"{[(st_, b.get('partial')) for st_, b in res]}")

        # ----- 8. a restart keeps the station and the history --------------- #
        # A fresh station from the same seed; play five of its tracks through, on a
        # session bound to it.
        st_, p0 = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [seed]}, "character": "balanced",
            "count": 10, "deviceId": "dev-a", "origin": "continue"})
        sid, seq = p0["stationId"], p0["seq"]
        ids = [t["id"] for t in p0["tracks"]]
        rx = Client("Player", device_id="player")
        await rx.connect(f"ws://127.0.0.1:{port}")
        played = p0["tracks"][:5]
        queue = [{"id": seed, "title": LIB[seed]["title"], "artist": LIB[seed]["artist"],
                  "durationMs": 200000}] + [
            {"id": t["id"], "title": t["title"], "artist": t["artist"],
             "durationMs": 200000} for t in played]
        await rx.act(action="setQueue", tracks=queue, index=0, play=True, stationId=sid)
        await asyncio.sleep(0.4)
        for i in range(len(queue) - 1):
            await rx.report(index=i, positionMs=195000, isPlaying=True)
            await asyncio.sleep(0.05)
            await rx.report(index=i + 1, positionMs=400, isPlaying=True)
            await asyncio.sleep(0.05)
        await rx.report(index=len(queue) - 1, positionMs=199000, isPlaying=False,
                        ended=True)
        await asyncio.sleep(0.4)
        await rx.ws.close()
        # Also a version of a song: whichever Echo Song station 2 served (if any),
        # its siblings must now be out for every station.
        before_seq = seq
    finally:
        stop(proc)

    hist = state + ".history"
    try:
        with open(hist, encoding="utf-8") as f:
            hdata = json.load(f)
        plays = hdata.get("plays", [])
        if len(plays) < 6:
            failures.append(f"the history file holds {len(plays)} plays, expected 6")
        text = json.dumps(hdata)
        if any(t["title"] in text for t in played):
            failures.append("the history file holds a title (ids and keys only)")
    except FileNotFoundError:
        failures.append("no history file after a graceful stop")
        plays = []

    proc = spawn(port, state, am_url, nd_url, log_path=log_path)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, nx = await acall(port, "POST", "/sonic/station/next", {
            "stationId": sid, "after": before_seq, "count": 5, "deviceId": "dev-a"})
        if st_ != 200 or nx.get("seq") != before_seq + len(nx.get("tracks", [])):
            failures.append(f"the station did not survive the restart: {st_} {nx}")
        elif {t["id"] for t in nx["tracks"]} & set(ids):
            failures.append("the restarted station served a track it had served")
        if nx.get("origin") != "continue":
            failures.append(f"origin did not survive the restart: {nx.get('origin')}")
        st_, again = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [seed]}, "character": "balanced",
            "count": 50, "deviceId": "dev-a"})
        played_ids = {t["id"] for t in played}
        played_keys = {title_key(t["title"], t["artist"]) for t in played}
        got_ids = {t["id"] for t in again.get("tracks", [])}
        got_keys = {title_key(t["title"], t["artist"]) for t in again.get("tracks", [])}
        if got_ids & played_ids:
            failures.append(f"a new station served a track played before the restart: "
                            f"{got_ids & played_ids}")
        if got_keys & played_keys:
            failures.append("a new station served a version of a played song")
        if len(again.get("tracks", [])) < 40:
            failures.append(f"the new station came up short: {len(again.get('tracks', []))}")

        # ----- 11. the largest response ------------------------------------- #
        import hub as hubmod
        if AM["max_n"] > hubmod.STATION_LIST_DEPTHS[-1]:
            failures.append(f"a list deeper than {hubmod.STATION_LIST_DEPTHS[-1]} was "
                            f"asked for: {AM['max_n']}")
        if AM["max_n"] * LIVE_ROW_BYTES >= hubmod.PROXY_MAX_RESPONSE:
            failures.append(f"n={AM['max_n']} would be ~{AM['max_n'] * LIVE_ROW_BYTES} B "
                            f"on the live AudioMuse, over the {hubmod.PROXY_MAX_RESPONSE} cap")
        if AM["max_body"] >= hubmod.PROXY_MAX_RESPONSE:
            failures.append(f"the fake served a body over the cap: {AM['max_body']}")
        if AM["max_n"] < 2000:
            failures.append(f"Explore never widened to its deepest list ({AM['max_n']}), "
                            "so the size check checked nothing")
    finally:
        stop(proc)
        am_srv.shutdown()
        nd_srv.shutdown()
        if ND["methods"].get("getRandomSongs"):
            failures.append(f"getRandomSongs was called {ND['methods']['getRandomSongs']}x")
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if ND_PASS in text or re.search(r"[?&](t|p|s|navidrome_password)=", text):
            failures.append("a credential reached the hub's log")
        unlink_all(state)
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass


async def tier1_suite(failures):
    """Cases 9 and 10: AudioMuse down; nothing configured."""
    nd_srv, nd_url = serve(Navidrome)
    dead = f"http://127.0.0.1:{_free_port()}"   # nothing listens there
    for sonic in (True, False):
        ND["sonic"] = sonic
        before = dict(ND["methods"])
        port = _free_port()
        state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        proc = spawn(port, state, dead, nd_url)
        try:
            await asyncio.to_thread(wait_up, port)
            st_, ch = await acall(port, "GET", "/sonic/station/characters")
            if ch.get("engine") != "tier1" or ch.get("available") is not True:
                failures.append(f"AudioMuse down: the probe should say tier1: {ch}")
            st_, a = await acall(port, "POST", "/sonic/station/start", {
                "seed": {"kind": "track", "ids": ["L04_0"]}, "count": 10})
            if st_ != 200 or a.get("engine") != "tier1" or len(a.get("tracks", [])) != 10:
                failures.append(f"AudioMuse down (sonic={sonic}): {st_} "
                                f"engine={a.get('engine')} n={len(a.get('tracks', []))}")
            elif {t["why"] for t in a["tracks"]} != {"tier1"} or not a.get("partial"):
                failures.append(f"tier1 tracks must say why tier1 and the answer partial: "
                                f"{ {t['why'] for t in a['tracks']} } {a.get('partial')}")
            st_, nx = await acall(port, "POST", "/sonic/station/next", {
                "stationId": a.get("stationId"), "after": a.get("seq"), "count": 5})
            if len(nx.get("tracks", [])) != 5 or {t["id"] for t in nx["tracks"]} & \
                    {t["id"] for t in a.get("tracks", [])}:
                failures.append(f"tier1 next: {nx}")
            method = "getSonicSimilarTracks" if sonic else "getSimilarSongs2"
            if ND["methods"].get(method, 0) <= before.get(method, 0):
                failures.append(f"tier1 with sonic={sonic} never asked {method}")
            st_, ym = await acall(port, "POST", "/sonic/station/start",
                                  {"seed": {"kind": "yourMix"}})
            if st_ != 503 or not ym.get("fallback"):
                failures.append(f"Your mix without AudioMuse must be 503 fallback: {st_} {ym}")
        finally:
            stop(proc)
            unlink_all(state)
    ND["sonic"] = True
    nd_srv.shutdown()
    if ND["methods"].get("getRandomSongs"):
        failures.append("getRandomSongs was called on the tier1 path")

    # ----- 10. nothing configured ------------------------------------------- #
    port = _free_port()
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state)
    try:
        await asyncio.to_thread(wait_up, port)
        st_, ch = await acall(port, "GET", "/sonic/station/characters")
        if st_ != 200 or ch.get("configured") is not False or ch.get("available") \
                or ch.get("engine") is not None or len(ch.get("characters", [])) != 3:
            failures.append(f"nothing configured: the probe {st_} {ch}")
        st_, a = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L01_0"]}})
        if st_ != 503 or a.get("fallback") is not True:
            failures.append(f"nothing configured: start must be 503 fallback: {st_} {a}")
        st_, a = await acall(port, "POST", "/sonic/station/next",
                             {"stationId": "st_1_beef", "after": 0})
        if st_ != 503 or a.get("fallback") is not True:
            failures.append(f"nothing configured: next must be 503 fallback: {st_} {a}")
    finally:
        stop(proc)
        unlink_all(state)


def unit_checks(failures):
    """The title key and the artist key, in-process."""
    import hub as hubmod
    k = hubmod._station_title_key
    same = [("Wish You Were Here", "Pink Floyd"),
            ("Wish You Were Here - 2011 Remastered Version", "Pink Floyd"),
            ("Wish You Were Here (Live at Knebworth)", "Pink Floyd"),
            ("Wish You Were Here [Remastered]", "Pink Floyd feat. Someone"),
            ("Wish You Were Here feat. Guest", "Pink Floyd & Friends"),
            ("WISH YOU WERE HERE!", "pink floyd")]
    if len({k(t, a) for t, a in same}) != 1:
        failures.append(f"versions of one song keyed apart: "
                        f"{[hubmod._station_title(t) for t, _ in same]}")
    if k("Live Forever", "Oasis") == k("Forever", "Oasis"):
        failures.append("a leading 'Live' was stripped as if it were a tail")
    if k("Wish You Were Here", "Pink Floyd") == k("Wish You Were Here", "Avril Lavigne"):
        failures.append("the same title by another artist must key apart")
    if k("夜に駆ける", "YOASOBI") == k("群青", "YOASOBI") or not hubmod._station_title("夜に駆ける"):
        failures.append("non-Latin titles must keep their letters")
    if hubmod._station_artist_key("AC/DC") != "ac dc" or \
            hubmod._station_artist_key("Simon & Garfunkel") != "simon":
        failures.append(f"artist key: {hubmod._station_artist_key('AC/DC')!r} "
                        f"{hubmod._station_artist_key('Simon & Garfunkel')!r}")
    kv = hubmod._station_features("danceable:0.63,aggressive:0.62,happy:0.62,party:0.60,"
                                  "relaxed:0.68,sad:0.64")
    if kv != (0.63, 0.62, 0.62, 0.60, 0.68, 0.64):
        failures.append(f"other_features string parse: {kv}")


async def main():
    failures: list[str] = []
    unit_checks(failures)
    await main_suite(failures)
    await tier1_suite(failures)
    if failures:
        print("FAIL (station engine):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - station engine: probe+catalogue/30-refills-no-repeat-no-title-repeat-"
          "spacing/no-second-hop-on-close-balanced/rank-ceiling/15-then-exhausted/"
          "500-mid-ladder-partial/after-replay+servedElsewhere/session_bound-409/"
          "unlisted-keys-dropped/your-mix/restart-keeps-station+history/"
          "explore-drifts-only-after-a-play-through/budget-from-arrival/"
          "tier1-sonic+similarSongs2/unconfigured-503-fallback/largest-under-cap/"
          "no-getRandomSongs/title-key")


if __name__ == "__main__":
    asyncio.run(main())
