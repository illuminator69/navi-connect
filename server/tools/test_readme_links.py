#!/usr/bin/env python3
"""
Worbus P6, lane H (H1): the umbrella's README and INSTALL.md point at images that exist.

  readme_images_exist     every local image path in README.md and INSTALL.md exists in the tree,
                          except docs/screenshots/0.6/*, which may instead be listed in
                          docs/screenshots/0.6/EXPECTED (a plain list, one path per line, that the
                          screenshots step deletes when it adds the files; the test then requires them)
  readme_header_picture   the README opens with a <picture>: dark scheme -> docs/brand/worbus-dark.svg,
                          default -> docs/brand/worbus-light.svg
  readme_uses_fixed_shots the README shows all eight fixed screenshot names and no other local shot

Standard library only. Exits non-zero on failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("README.md", "INSTALL.md")
SHOT_DIR = "docs/screenshots/0.6"
EXPECTED = ROOT / SHOT_DIR / "EXPECTED"
FIXED = [f"{SHOT_DIR}/{app}-{n}.png" for app in ("android", "desktop") for n in (1, 2, 3, 4)]

IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I | re.S)
SOURCE_SRCSET = re.compile(r"""<source\b[^>]*?\bsrcset\s*=\s*["']([^"']+)["']""", re.I | re.S)
MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")


def local_images(text: str) -> list[str]:
    found = IMG_SRC.findall(text) + SOURCE_SRCSET.findall(text) + MD_IMAGE.findall(text)
    out = []
    for ref in found:
        ref = ref.strip().split("#", 1)[0].split("?", 1)[0]
        if not ref or re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.I) or ref.startswith("//"):
            continue  # a URL, not a path in this tree
        out.append(ref.lstrip("./") if ref.startswith("./") else ref)
    return out


def expected_list() -> set[str]:
    if not EXPECTED.is_file():
        return set()
    return {ln.strip() for ln in EXPECTED.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")}


def readme_images_exist() -> list[str]:
    problems = []
    expected = expected_list()
    for name in DOCS:
        for ref in local_images((ROOT / name).read_text()):
            if (ROOT / ref).is_file():
                continue
            if ref.startswith(SHOT_DIR + "/") and ref in expected:
                continue  # listed as still to come
            problems.append(f"{name}: image {ref!r} does not exist"
                            + (f" and is not in {SHOT_DIR}/EXPECTED" if ref.startswith(SHOT_DIR + "/") else ""))
    for ref in sorted(expected):
        if not ref.startswith(SHOT_DIR + "/"):
            problems.append(f"EXPECTED lists {ref!r}, outside {SHOT_DIR}/")
    return problems


def readme_header_picture() -> list[str]:
    text = (ROOT / "README.md").read_text()
    pic = re.search(r"<picture>.*?</picture>", text, re.S | re.I)
    if not pic:
        return ["README.md has no <picture>"]
    if text.find("<picture>") > text.find("\n# "):
        return ["the <picture> is not above the title"]
    block = pic.group(0)
    problems = []
    src = re.search(r"<source\b[^>]*>", block, re.I | re.S)
    if not src or "prefers-color-scheme: dark" not in src.group(0) \
            or "docs/brand/worbus-dark.svg" not in src.group(0):
        problems.append("the <source> is not (prefers-color-scheme: dark) -> docs/brand/worbus-dark.svg")
    img = re.search(r"<img\b[^>]*>", block, re.I | re.S)
    if not img or "docs/brand/worbus-light.svg" not in img.group(0):
        problems.append("the default <img> is not docs/brand/worbus-light.svg")
    if not img or not re.search(r'width\s*=\s*["\']?96\b', img.group(0)):
        problems.append("the default <img> is not width 96")
    return problems


def readme_uses_fixed_shots() -> list[str]:
    refs = local_images((ROOT / "README.md").read_text())
    problems = [f"README.md does not show {p}" for p in FIXED if p not in refs]
    for ref in refs:
        if ref.startswith("docs/screenshots/") and ref not in FIXED:
            problems.append(f"README.md shows {ref!r}, not one of the eight fixed names")
    return problems


def main() -> int:
    failed = 0
    for check in (readme_images_exist, readme_header_picture, readme_uses_fixed_shots):
        problems = check()
        if problems:
            failed += 1
            print(f"FAIL {check.__name__}")
            for p in problems:
                print(f"  {p}")
        else:
            print(f"ok   {check.__name__}")
    if failed:
        return 1
    print("PASS test_readme_links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
