#!/usr/bin/env python3
"""
A bullet or a spaced " - " in an artist credit splits acts (B-176, Q-120; bug run H3).

`_station_artist_key` is the "same artist" key every station rule uses (artist
spacing, the seed-artist cap, skip cooldowns, the mood lists' per-artist cap) and part
of the hashed "same song" key the play history stores. It keyed
"Phoebe Bridgers • Phoebe Bridgers, Christian Lee Hutson" and
"Lana Del Rey - Nikki Lane" as artists other than Phoebe Bridgers and Lana Del Rey.
It now splits on `•` and on a spaced ` - `, and still never on an unspaced hyphen
(Jay-Z) or a slash (AC/DC).

No hub subprocess: the regex, the key and two of its consumers are called directly.
Exits non-zero on failure.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402


def main() -> int:
    failures: list[str] = []
    split = hub._ARTIST_SEP_RE.split
    key = hub._station_artist_key

    for credit, want in (
        ("A • B", ["A", "B"]),
        ("A•B", ["A", "B"]),
        ("A - B", ["A", "B"]),
        ("Jay-Z", ["Jay-Z"]),
        ("Run-DMC", ["Run-DMC"]),
        ("A -B", ["A -B"]),
        ("A- B", ["A- B"]),
        ("AC/DC", ["AC/DC"]),
        ("Simon & Garfunkel", ["Simon", "Garfunkel"]),
    ):
        got = split(credit)
        if got != want:
            failures.append(f"split {credit!r}: {got!r}, want {want!r}")

    for credit, want in (
        ("Phoebe Bridgers • Phoebe Bridgers, Christian Lee Hutson", "phoebe bridgers"),
        ("Lana Del Rey - Nikki Lane", "lana del rey"),
        ("Jay-Z", "jay z"),
        ("AC/DC", "ac dc"),
        ("Simon & Garfunkel", "simon"),
    ):
        if key(credit) != want:
            failures.append(f"key {credit!r}: {key(credit)!r}, want {want!r}")

    # Consumers: a station row's `ak` and the hashed history key.
    bullet = hub._station_row({"id": "t1", "title": "Kyoto",
                               "artist": "Phoebe Bridgers • Phoebe Bridgers, Christian Lee Hutson"})
    plain = hub._station_row({"id": "t2", "title": "Kyoto", "artist": "Phoebe Bridgers"})
    if not bullet or not plain or bullet["ak"] != plain["ak"]:
        failures.append(f"station row ak differs: {bullet and bullet['ak']!r} vs "
                        f"{plain and plain['ak']!r}")
    if not bullet or not plain or bullet["tk"] != plain["tk"]:
        failures.append("the same song under a bullet credit has another history key")

    # The mood lists' per-artist cap counts the bullet credit as the same artist.
    rows = [plain, bullet, hub._station_row({"id": "t3", "title": "Other", "artist": "X"})]
    merged = hub._mood_merge([rows], cap=1)
    if [r["id"] for r in merged] != ["t2", "t3"]:
        failures.append(f"per-artist cap 1 kept {[r['id'] for r in merged]!r}, "
                        "want ['t2', 't3']")

    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - artist key: bullet-splits/spaced-dash-splits/unspaced-hyphen-kept/"
          "slash-kept/row-ak-matches/history-key-matches/mood-cap-counts-bullet-credit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
