#!/usr/bin/env python3
"""
Run the station engine (PROTOCOL §18) in-process against a real AudioMuse and
Navidrome, and print what each batch serves and why.

Read-only by construction: the engine calls only AudioMuse's similar_tracks,
alchemy (compute only), sonic_fingerprint, similar_artists and external/get_score,
and Navidrome's getOpenSubsonicExtensions, getSonicSimilarTracks, getSimilarSongs2
and getSong. This tool adds one AudioMuse search_tracks call to resolve
--title/--artist. The hub it builds mirrors nothing to Navidrome, keeps its state
in a temporary file and its play history in memory, and listens on no port.

  station_probe.py --env FILE (--seed ID | --title T --artist A | --your-mix)
                   [--character close|balanced|explore|all] [--refills N]
                   [--count K] [--skip I,J] [--played I,J] [--navidrome URL]
                   [--tier1-only] [--json]

--env names a file of KEY=VALUE lines (AUDIOMUSE_URL, AUDIOMUSE_TOKEN,
HUB_ND_USER, HUB_ND_PASS, optionally NAVIDROME_URL). Its values are put in this
process's environment and never printed. --skip I,J sends a `skip` signal for
the I-th and J-th track served (0-based, over the whole run of one character),
right after the batch that served it; --played does the same with `played`, which
gives Explore played-through tracks to drift from. --tier1-only drops AUDIOMUSE_URL
after reading the file, so the engine runs on Navidrome's similar songs alone.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections import Counter

ENV_KEYS = ("AUDIOMUSE_URL", "AUDIOMUSE_TOKEN", "HUB_ND_USER", "HUB_ND_PASS",
            "NAVIDROME_URL")
HIPHOP = ("hip-hop", "hip hop", "rap", "trap")


def load_env(path: str, navidrome: str, tier1_only: bool = False) -> None:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in ENV_KEYS:
                os.environ[k.strip()] = v.strip()
    os.environ.setdefault("NAVIDROME_URL", navidrome)
    if tier1_only:
        os.environ.pop("AUDIOMUSE_URL", None)
    state_dir = tempfile.mkdtemp(prefix="station-probe-")
    os.environ.update(HUB_STATE=os.path.join(state_dir, "state.json"),
                      HUB_STATION_HISTORY="0", HUB_MIRROR_PLAYQUEUE="false",
                      HUB_TOKEN="station-probe")


def is_hiphop(tag: str) -> bool:
    return any(h in (tag or "").lower() for h in HIPHOP)


async def capabilities(hub, known_id: str) -> dict:
    """Which tiers and routes answer, and what shape the rows have. Statuses and
    key names only, never a value."""
    eng = hub.HUB_INSTANCE.stations
    await eng.probe()
    caps: dict = {"tier2": eng.tier2_ok, "tier1": eng.tier1_ok,
                  "navidromeSonicSimilarity": eng.sonic}
    if not hub.AUDIOMUSE_URL:
        return caps
    for path in ("/external/get_score", "/get_score"):
        spec = {"method": "GET", "path": path, "params": ("id",), "cache": False,
                "timeout": 8}
        st, _raw, _ct = await hub.SONIC.call(("GET", f"probe:{path}"), spec,
                                             [("id", known_id)], None)
        caps[f"GET {path}"] = st
    status, rows = await eng._am("similar_tracks", {  # noqa: SLF001
        "item_id": known_id, "n": 3, "radius_similarity": "false",
        "eliminate_duplicates": "false"}, None, None)
    if status == 200 and rows:
        r = rows[0]
        caps["similar_tracks row keys"] = sorted(r)
        caps["other_features is"] = type(r.get("other_features")).__name__
        caps["mood_vector is"] = type(r.get("mood_vector")).__name__
        caps["nearest-first"] = all(a["distance"] <= b["distance"]
                                    for a, b in zip(rows, rows[1:]))
    else:
        caps["similar_tracks"] = status
    return caps


async def resolve(hub, title: str, artist: str) -> str:
    spec = {"method": "GET", "path": "/api/search_tracks", "params": ("title", "artist"),
            "cache": False, "timeout": 8}
    st, raw, _ = await hub.SONIC.call(("GET", "probe:search"), spec,
                                      [("title", title), ("artist", artist)], None)
    rows = json.loads(raw) if st == 200 else []
    if not rows:
        raise SystemExit(f"search_tracks found nothing for {title!r} by {artist!r} ({st})")
    return rows[0]["item_id"]


async def seed_row(hub, seed: str) -> dict:
    eng = hub.HUB_INSTANCE.stations
    if hub.AUDIOMUSE_URL:
        status, data = await eng._am("score", {"id": seed}, None, None)  # noqa: SLF001
        row = hub._station_row(data) if status == 200 else None  # noqa: SLF001
    else:
        outcome, payload = await eng._nd("getSong", {"id": seed}, None)  # noqa: SLF001
        row = hub._station_row((payload or {}).get("song")) if outcome == "ok" else None
    return row or {"title": "?", "artist": "?", "tag": ""}


async def run_character(hub, seed, your_mix, character, refills, count, skips, seed_tag,
                        plays=()):
    eng = hub.HUB_INSTANCE.stations
    body = {"seed": {"kind": "yourMix"} if your_mix else {"kind": "track", "ids": [seed]},
            "character": character, "count": count, "deviceId": "probe"}
    status, ans = await eng.start(body, debug=True)
    if status != 200:
        return {"character": character, "error": {"status": status, **ans}}
    batches = [ans]
    sid, seq = ans["stationId"], ans["seq"]
    served = list(ans["tracks"])

    async def skip_due():
        for kind, which in (("skip", skips), ("played", plays)):
            for i in which:
                if i < len(served) and not served[i].get("_signalled"):
                    served[i]["_signalled"] = kind
                    await eng.signal({"stationId": sid, "trackId": served[i]["id"],
                                      "kind": kind})
    await skip_due()
    for _ in range(refills):
        status, nx = await eng.next({"stationId": sid, "after": seq, "count": count,
                                     "deviceId": "probe"}, debug=True)
        if status != 200:
            batches.append({"error": status, **nx})
            break
        batches.append(nx)
        seq = nx["seq"]
        served += nx["tracks"]
        await skip_due()
        if nx["exhausted"]:
            break
    ids = [t["id"] for t in served]
    keys = [t["_debug"]["tk"] for t in served]
    artists = Counter(t["_debug"]["ak"] for t in served)
    rungs = Counter(r for b in batches for r in (b.get("_debug") or {}).get("rungs", []))
    per_batch_off = [sum(1 for t in b.get("tracks", [])
                         if (t["_debug"]["tag"] or "").lower() != (seed_tag or "").lower())
                     for b in batches]
    return {
        "character": character, "stationId": sid, "name": ans.get("name"),
        "engine": ans.get("engine"), "served": len(served),
        "repeatsById": len(ids) - len(set(ids)),
        "repeatsByTitleKey": len(keys) - len(set(keys)),
        "offTagPerBatch": per_batch_off,
        "hipHopLed": [f"{t['artist']} - {t['title']}" for t in served
                      if is_hiphop(t["_debug"]["tag"])],
        "distinctArtists": len(artists), "maxPerArtist": max(artists.values() or [0]),
        "why": dict(Counter(t["why"] for t in served)),
        "maxRank": max((t["rank"] for t in served), default=0),
        "rungs": dict(rungs),
        "partialBatches": sum(1 for b in batches if b.get("partial")),
        "exhausted": bool(batches[-1].get("exhausted")),
        "batches": [{"tracks": [{k: t[k] for k in ("title", "artist", "why", "rank")}
                                | {"tag": t["_debug"]["tag"], "rung": t["_debug"]["rung"]}
                                for t in b.get("tracks", [])],
                     "rungs": (b.get("_debug") or {}).get("rungs"),
                     "partial": b.get("partial"), "exhausted": b.get("exhausted")}
                    for b in batches],
    }


def print_report(seed_desc, caps, results):
    print(f"seed: {seed_desc}")
    print("capabilities: " + ", ".join(f"{k}={v}" for k, v in caps.items()))
    for r in results:
        print()
        if "error" in r:
            print(f"== {r['character']}: refused {r['error']}")
            continue
        print(f"== {r['character']} ({r['name']}, engine {r['engine']}): {r['served']} served, "
              f"repeats id/title {r['repeatsById']}/{r['repeatsByTitleKey']}, "
              f"{r['distinctArtists']} artists (max {r['maxPerArtist']} each), "
              f"max rank {r['maxRank']}, why {r['why']}, rungs {r['rungs']}, "
              f"partial batches {r['partialBatches']}, exhausted {r['exhausted']}")
        print(f"   off-tag per batch: {r['offTagPerBatch']}   hip-hop-led: "
              f"{len(r['hipHopLed'])} {r['hipHopLed'][:5]}")
        for i, b in enumerate(r["batches"]):
            print(f"   batch {i} rungs={b['rungs']} partial={b['partial']} "
                  f"exhausted={b['exhausted']}")
            for t in b["tracks"]:
                print(f"     {t['title'][:34]:34} | {t['artist'][:24]:24} | "
                      f"{(t['tag'] or '')[:18]:18} | {t['why']:6} | rank {t['rank']:4} "
                      f"| {t['rung']}")


async def main_async(args):
    import hub  # noqa: E402 — configured from the environment just set
    h = hub.Hub()
    hub.HUB_INSTANCE = h
    seed = None
    if args.your_mix:
        seed_desc, seed_tag = "Your mix (fingerprint of HUB_ND_USER)", None
    else:
        seed = args.seed or await resolve(hub, args.title, args.artist)
        row = await seed_row(hub, seed)
        seed_tag = row.get("tag")
        seed_desc = f"{row.get('title')} - {row.get('artist')} [{seed_tag}] ({seed})"
    caps = await capabilities(hub, seed or "76Rl620lkV4y1L0bxTjT04")
    chars = hub.STATION_CHARACTERS if args.character == "all" else (args.character,)
    skips = [int(x) for x in args.skip.split(",") if x.strip()] if args.skip else []
    plays = [int(x) for x in args.played.split(",") if x.strip()] if args.played else []
    results = []
    for ch in chars:
        results.append(await run_character(hub, seed, args.your_mix, ch, args.refills,
                                           args.count, skips, seed_tag, plays))
    return seed_desc, caps, results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed")
    g.add_argument("--title")
    g.add_argument("--your-mix", action="store_true")
    ap.add_argument("--artist", default="")
    ap.add_argument("--character", default="all",
                    choices=("close", "balanced", "explore", "all"))
    ap.add_argument("--refills", type=int, default=12)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--skip", default="")
    ap.add_argument("--played", default="")
    ap.add_argument("--navidrome", default="http://localhost:4533")
    ap.add_argument("--tier1-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    load_env(args.env, args.navidrome, args.tier1_only)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    if args.json:
        with contextlib.redirect_stdout(sys.stderr):  # the hub's own log lines
            seed_desc, caps, results = asyncio.run(main_async(args))
        print(json.dumps({"seed": seed_desc, "capabilities": caps, "results": results},
                         indent=1, ensure_ascii=False))
    else:
        seed_desc, caps, results = asyncio.run(main_async(args))
        print_report(seed_desc, caps, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
