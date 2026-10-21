#!/usr/bin/env python3
"""
Time and count the moods (PROTOCOL §18.5) in-process against a real AudioMuse.

Read-only by construction: the only AudioMuse calls are `/api/clap/stats` and
`/api/clap/search` (and, with --stations, the station engine's own read-only
calls, see station_probe.py). The hub it builds keeps its state in a temporary
directory, holds its play history in memory and listens on no port.

  mood_probe.py --env FILE [--library CACHE_DB] [--coverage IDS_FILE]
                [--stations IDS] [--json]

--env names a file of KEY=VALUE lines (AUDIOMUSE_URL, AUDIOMUSE_TOKEN, optionally
HUB_ND_USER, HUB_ND_PASS, NAVIDROME_URL). Its values go into this process's
environment and are never printed. Make it from the hub container without the
token touching a terminal, e.g.
  ssh nas 'docker exec navi-connect-hub printenv' | grep -E '^(AUDIOMUSE|HUB_ND)' > env
(then delete the file).

What it measures (P7 Task A1 step 4, A5 step 2, B4 step 2):
  - the cold build of all 13 merged lists (searches, seconds, rows per mood);
  - one tile's list cold (Party's own prompt, not part of the build) and warm;
  - `for` warm over 200 ids, and the memory the lists hold (tracemalloc);
  - --library: Navic's Room `cache.db` (adb exec-out run-as paige.navic... cat
    databases/cache.db): per tile, how many of its ids are in the library and how
    many survive the mood's length limit — the smallest is the number to report;
  - --coverage: a file of song ids, one per line (e.g. the 200 most played): how
    many lie within the first 300 / 600 / 1000 / 1500 of some mood's merged list;
  - --stations ID[,ID...]: Part B — a mood station's start, a mood signal, and the
    count with a mood filter for the first 1, 3 and 6 ids at each character.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import tracemalloc

ENV_KEYS = ("AUDIOMUSE_URL", "AUDIOMUSE_TOKEN", "HUB_ND_USER", "HUB_ND_PASS",
            "NAVIDROME_URL")


def load_env(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in ENV_KEYS:
                os.environ[k.strip()] = v.strip()
    state_dir = tempfile.mkdtemp(prefix="mood-probe-")
    os.environ.update(HUB_STATE=os.path.join(state_dir, "state.json"),
                      HUB_STATION_HISTORY="0", HUB_MIRROR_PLAYQUEUE="false",
                      HUB_TOKEN="mood-probe")


def library(path: str) -> dict[str, int]:
    """Room's songs: id -> duration in seconds (Room stores milliseconds)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {sid: int((ms or 0) // 1000)
                for sid, ms in con.execute("SELECT songId, duration FROM SongEntity")}
    finally:
        con.close()


async def part_a(hub, out: dict, lib: dict | None, coverage: list | None) -> None:
    svc = hub.HUB_INSTANCE.moods
    t0 = time.monotonic()
    await svc.refresh_stats(wait=20)
    out["available"] = svc.available()
    out["statsSeconds"] = round(time.monotonic() - t0, 2)
    if not svc.available():
        return
    tracemalloc.start()
    calls = {"n": 0}
    real = hub.SONIC.call

    async def counting(route, spec, params, body):
        if route == hub.SONIC_INTERNAL["clap_search"][0]:
            calls["n"] += 1
        return await real(route, spec, params, body)
    hub.SONIC.call = counting
    t0 = time.monotonic()
    ok = await svc.build_all()
    out["build"] = {"ready": ok, "seconds": round(time.monotonic() - t0, 1),
                    "searches": calls["n"]}
    size, _peak = tracemalloc.get_traced_memory()
    out["build"]["memoryMB"] = round(size / 1e6, 1)
    tracemalloc.stop()
    out["merged"] = {m["id"]: len((svc._merged.get(m["id"]) or (0, []))[1])  # noqa: SLF001
                     for m in hub.MOODS}
    out["searchRows"] = {m["id"]: [len(svc._searches.get(p, (0, []))[1])  # noqa: SLF001
                                   for p in m["prompts"]] for m in hub.MOODS}
    t0 = time.monotonic()
    cold = await svc.tile("party", "pop")
    t1 = time.monotonic()
    warm = await svc.tile("party", "pop")
    t2 = time.monotonic()
    out["tile"] = {"coldSeconds": round(t1 - t0, 2), "warmMs": round((t2 - t1) * 1000, 2),
                   "ids": len(cold or []), "same": cold == warm}
    ids = [r["id"] for r in (svc._merged["relax"][1] if ok else [])][:200]  # noqa: SLF001
    t0 = time.monotonic()
    for i in ids:
        await svc.for_song(i)
    out["forSongWarmMs"] = round((time.monotonic() - t0) * 1000 / max(1, len(ids)), 3)

    if lib is not None:
        tiles = []
        for m in hub.MOODS:
            for t in m.get("tiles") or ():
                tids = await svc.tile(m["id"], t["id"]) or []
                owned = [i for i in tids if i in lib]
                fits = [i for i in owned if m["maxSeconds"] is None
                        or lib[i] <= m["maxSeconds"]]
                tiles.append({"mood": m["id"], "tile": t["id"], "title": t["title"],
                              "ids": len(tids), "inLibrary": len(owned),
                              "fitLength": len(fits)})
        out["library"] = {"songs": len(lib), "tiles": tiles,
                          "smallest": min(tiles, key=lambda x: x["fitLength"])}
    if coverage is not None:
        ranks = {m["id"]: svc._merged[m["id"]][2] for m in hub.MOODS  # noqa: SLF001
                 if m["id"] in svc._merged}  # noqa: SLF001
        cov = {}
        for n in (300, 600, 1000, 1500):
            hit = [i for i in coverage
                   if any(r.get(i, 10 ** 9) <= n for r in ranks.values())]
            two = [i for i in coverage
                   if sum(1 for r in ranks.values() if r.get(i, 10 ** 9) <= n) >= 2]
            cov[str(n)] = {"oneOrMore": len(hit), "twoOrMore": len(two)}
        out["coverage"] = {"ids": len(coverage), "within": cov,
                           "note": "the merged lists are kept to 1000, so 1500 = 1000"}


async def part_b(hub, out: dict, ids: list[str]) -> None:
    eng = hub.HUB_INSTANCE.stations
    await eng.probe()
    res = []
    for ch in hub.STATION_CHARACTERS:
        t0 = time.monotonic()
        st, a = await eng.start({"seed": {"kind": "track", "ids": ids[:1]}, "character": ch,
                                 "count": 10, "mood": "relax"})
        res.append({"what": f"start mood=relax {ch}", "status": st,
                    "seconds": round(time.monotonic() - t0, 2),
                    "tracks": len((a or {}).get("tracks") or []),
                    "partial": (a or {}).get("partial")})
        if st == 200:
            t0 = time.monotonic()
            s2, sig = await eng.signal({"stationId": a["stationId"], "kind": "mood",
                                        "value": "party"})
            res.append({"what": f"signal mood=party {ch}", "status": s2,
                        "seconds": round(time.monotonic() - t0, 2)})
        for n in (1, 3, 6):
            seed = {"kind": "track" if n == 1 else "tracks", "ids": ids[:n]}
            for mood in (None, "pump-up", "relax"):
                body = {"seed": seed, "character": ch}
                if mood:
                    body["moodFilter"] = mood
                t0 = time.monotonic()
                s3, c = await eng.count(body)
                res.append({"what": f"count {n} anchors {ch} filter={mood}", "status": s3,
                            "count": (c or {}).get("count"),
                            "seconds": round(time.monotonic() - t0, 2)})
    out["stations"] = res


async def main_async(args) -> dict:
    import hub  # noqa: E402 — configured from the environment just set
    h = hub.Hub()
    hub.HUB_INSTANCE = h
    out: dict = {"audiomuse": bool(hub.AUDIOMUSE_URL)}
    lib = library(args.library) if args.library else None
    cov = None
    if args.coverage:
        with open(args.coverage, encoding="utf-8") as f:
            cov = [x.strip() for x in f if x.strip()]
    await part_a(hub, out, lib, cov)
    if args.stations:
        await part_b(hub, out, [x for x in args.stations.split(",") if x])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--library")
    ap.add_argument("--coverage")
    ap.add_argument("--stations")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    load_env(args.env)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    with contextlib.redirect_stdout(sys.stderr):  # the hub's own log lines
        out = asyncio.run(main_async(args))
    print(json.dumps(out, indent=None if args.json else 1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
