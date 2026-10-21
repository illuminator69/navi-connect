#!/usr/bin/env python3
"""
The tiles' ear sheet (P7 Task A0): for each of Home's 40 mood tiles, its title, its
prompt and its first 25 songs after the artist cap of 3, plus how much the four
tiles of each mood share. Written as Markdown for the owner to rate by ear.

Read-only: AudioMuse's `/api/clap/search` only, through the hub's own MoodService
in-process (so the sheet shows exactly what the hub would serve). No token in the
output; --env as in mood_probe.py.

  mood_earsheet.py --env FILE [--out ear-sheet-tiles.md] [--top 25]

Rate each tile as the spike did (very accurate / pretty accurate / not bad / about
N %). A tile under "pretty accurate" is reworded by describing the sound, not the
feeling, in `MOODS` (a tile's own `prompt`), and the sheet is run again.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mood_probe import load_env  # noqa: E402


async def build(hub, top: int) -> str:
    h = hub.Hub()
    hub.HUB_INSTANCE = h
    svc = h.moods
    await svc.refresh_stats(wait=20)
    if not svc.available():
        return "AudioMuse is not available (or CLAP is not loaded): nothing to rate.\n"
    lines = [f"# Mood tiles: the ear sheet ({hub.MOODS_VERSION})", "",
             "Each Home tile is ONE prompt, searched alone, at most 3 songs per artist. "
             f"The first {top} of its list are below. Rate each tile: very accurate / "
             "pretty accurate / not bad / about N %.", ""]
    overlap_rows = []
    for m in hub.MOODS:
        tiles = m.get("tiles") or ()
        if not tiles:
            continue
        lists = []
        lines += [f"## {m['label']}", ""]
        for i, t in enumerate(tiles):
            prompt = hub._mood_prompt(m, i)  # noqa: SLF001
            rows = await svc._search(prompt)  # noqa: SLF001
            if rows is None:
                lines += [f"### {t['title']}: the search failed", ""]
                lists.append(set())
                continue
            capped = hub._mood_merge([rows])[:m.get("depth", hub.MOOD_DEPTH_DEFAULT)]  # noqa: SLF001
            lists.append({r["id"] for r in capped})
            own = " (the tile's own prompt)" if t.get("prompt") else ""
            lines += [f"### {t['title']}", "", f"Prompt{own}: `{prompt}`", "",
                      "| # | artist | title | album | genre |", "|---|---|---|---|---|"]
            for n, r in enumerate(capped[:top], 1):
                lines.append(f"| {n} | {r['artist']} | {r['title']} | {r['album']} | "
                             f"{r.get('tag') or ''} |")
            lines += ["", "Rating: ______", ""]
        for a in range(len(lists)):
            for b in range(a + 1, len(lists)):
                if lists[a] and lists[b]:
                    share = len(lists[a] & lists[b]) / max(1, min(len(lists[a]),
                                                                   len(lists[b])))
                    overlap_rows.append((share, m["label"], tiles[a]["title"],
                                         tiles[b]["title"]))
    overlap_rows.sort(reverse=True)
    lines += ["## How much two tiles of one mood share", "",
              "Of their lists (300, Sleep and Focus 200). The plan's line is 50 %.", "",
              "| share | mood | tile | tile |", "|---|---|---|---|"]
    for share, mood, a, b in overlap_rows:
        lines.append(f"| {share:.0%} | {mood} | {a} | {b} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--out", default="ear-sheet-tiles.md")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    load_env(args.env)
    sys.path.insert(0, os.path.join(HERE, ".."))
    import hub  # noqa: E402
    with contextlib.redirect_stdout(sys.stderr):
        text = asyncio.run(build(hub, args.top))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
