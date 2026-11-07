#!/usr/bin/env python3
"""
Worbus P6, lane H (H3): the Navidrome catalog entry in distribution/navidrome-catalog/worbus/.

  catalog_entry_shape   index.yaml has the required fields with the fixed values; `macos` is absent
                        (until someone has run it); at most 6 keywords; the thumbnail and gallery are
                        the three fixed WebP names; the name does not start with "Navidrome"; the
                        description is one or two sentences and carries the non-affiliation line

index.yaml is read with a small line reader, so the test needs only the standard library (PyYAML is not
in the hub's venv). The entry is deliberately flat: a mapping of scalars, one nested mapping per section
(`platforms`, `screenshots`), and lists written as `- item` lines. If PyYAML happens to be importable the
test also checks that it reads the same values, and says so.

The webp files themselves are placed by the screenshots step (C2), so this test does not require them.
Navidrome's own `npm run validate:app` (a scratch clone of navidrome/website) is the check for the rest,
and the 15-star gate.

Exits non-zero on failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[2] / "distribution" / "navidrome-catalog" / "worbus" / "index.yaml"
NON_AFFILIATION = "Worbus is not affiliated with Navidrome."


def unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_entry(text: str) -> dict:
    """Mapping of scalars / one-level mappings / `- item` lists; no anchors, flow style or block scalars."""
    root: dict = {}
    stack = [(-1, root)]
    last_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        if line.startswith("- "):
            owner, key = last_key
            if not isinstance(owner.get(key), list):
                owner[key] = []
            owner[key].append(unquote(line[2:]))
            continue
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not m:
            raise ValueError(f"cannot read line: {raw!r}")
        key, val = m.group(1), m.group(2)
        if val == "":
            container[key] = {}
            stack.append((indent, container[key]))
        else:
            container[key] = unquote(val)
        last_key = (container, key)
    return root


def catalog_entry_shape() -> list[str]:
    if not ENTRY.is_file():
        return [f"{ENTRY} does not exist"]
    text = ENTRY.read_text()
    e = read_entry(text)
    problems: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    for field in ("name", "url", "repoUrl", "platforms", "api", "description", "screenshots", "pricing"):
        need(field in e, f"missing field {field!r}")
    for field in ("name", "url", "repoUrl", "api", "description", "pricing"):
        if field in e and not isinstance(e[field], str):
            problems.append(f"field {field!r} is empty or not a plain value")
    if problems:
        return problems
    need(e["name"] == "Worbus", f"name is {e['name']!r}, not 'Worbus'")
    need(not e["name"].lower().startswith("navidrome"), "the name starts with 'Navidrome'")
    need(e["url"] == "https://github.com/illuminator69/worbus", f"url is {e['url']!r}")
    need(e["repoUrl"] == "https://github.com/illuminator69/worbus", f"repoUrl is {e['repoUrl']!r}")
    need(e["api"] == "OpenSubsonic", f"api is {e['api']!r}")
    need(e["pricing"] == "free", f"pricing is {e['pricing']!r}")

    plat = e["platforms"]
    need(isinstance(plat, dict), "platforms is not a mapping")
    if isinstance(plat, dict):
        need("macos" not in plat, "platforms has macos (not until someone has run it)")
        need(plat.get("linux") == "true", "platforms.linux is not true")
        need(plat.get("windows") == "true", "platforms.windows is not true")
        # android: store link
        android = plat.get("android")
        need(isinstance(android, dict)
             and android.get("store") == "https://github.com/illuminator69/worbus-android/releases/latest",
             "platforms.android.store is not the Android releases/latest link")
        need(set(plat) <= {"android", "linux", "windows"}, f"unexpected platforms {sorted(plat)}")

    kws = e.get("keywords", [])
    need(isinstance(kws, list), "keywords is not a list")
    if isinstance(kws, list):
        need(len(kws) <= 6, f"{len(kws)} keywords, at most 6")

    shots = e["screenshots"]
    need(isinstance(shots, dict), "screenshots is not a mapping")
    if isinstance(shots, dict):
        need(shots.get("thumbnail") == "thumbnail.webp", f"thumbnail is {shots.get('thumbnail')!r}")
        need(shots.get("gallery") == ["screen1.webp", "screen2.webp"], f"gallery is {shots.get('gallery')!r}")

    desc = e["description"]
    need(NON_AFFILIATION in desc, "the description lacks the non-affiliation line")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", desc.strip()) if s]
    need(1 <= len(sentences) <= 2, f"description is {len(sentences)} sentences, not one or two")
    need("fork" in desc.lower() and "Navic" in desc and "Feishin" in desc,
         "the description does not say it is a fork of Navic and Feishin")
    need("missing albums" in desc.lower() and "session" in desc.lower(),
         "the description does not say what it does (the missing albums, the shared session)")

    try:
        import yaml  # type: ignore
    except ImportError:
        print("note: PyYAML not importable here; the line reader alone was used")
    else:
        parsed = yaml.safe_load(text)
        need(parsed.get("name") == e["name"] and parsed.get("repoUrl") == e["repoUrl"]
             and parsed.get("screenshots", {}).get("gallery") == shots.get("gallery")
             and "macos" not in parsed.get("platforms", {})
             and parsed["platforms"].get("linux") is True and parsed["platforms"].get("windows") is True
             and parsed["platforms"]["android"]["store"] == plat["android"]["store"],
             "PyYAML and the line reader disagree")
        print(f"note: PyYAML {yaml.__version__} cross-checked the line reader")
    return problems


def main() -> int:
    problems = catalog_entry_shape()
    if problems:
        print("FAIL catalog_entry_shape")
        for p in problems:
            print(f"  {p}")
        return 1
    print("ok   catalog_entry_shape")
    print("PASS test_catalog_entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
