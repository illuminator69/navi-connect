#!/usr/bin/env python3
"""
The front door says what licence Worbus is under (backlog B-425, 2026-11-14).

  licence_file            LICENSE exists at the root and is the GNU GPL, version 3 (GitHub reads this
                          file and nothing else to show a repo's licence)
  readme_names_licence    the README's Licence section names GPL-3.0-or-later for the hub, the server
                          and lb-bot, and carries the one copyright line
  site_covers_server      the site no longer says only "The two apps are GPL-3.0"
  install_names_licence   INSTALL.md says which licence the server is under
  one_copyright_line      README.md, INSTALL.md and the site carry no other copyright line (so no
                          person's name is put into a public file by accident)

Standard library only. Exits non-zero on failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPDX = "GPL-3.0-or-later"
COPYRIGHT = "Copyright (C) 2026 Worbus contributors"


def _licence_section() -> str:
    text = (ROOT / "README.md").read_text()
    m = re.search(r"(?ms)^## Licence\n(.*?)(?=^## |\Z)", text)
    return m.group(1) if m else ""


def licence_file() -> list[str]:
    path = ROOT / "LICENSE"
    if not path.is_file():
        return ["there is no LICENSE file at the root"]
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    want = ["GNU GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"]
    return [] if lines[:2] == want else [f"LICENSE opens with {lines[:2]!r}, not {want!r}"]


def readme_names_licence() -> list[str]:
    section = _licence_section()
    if not section:
        return ["README.md has no '## Licence' section"]
    problems = [f"the Licence section does not say {word!r}"
                for word in (SPDX, "hub", "server", "lb-bot", COPYRIGHT) if word not in section]
    return problems


def site_covers_server() -> list[str]:
    html = (ROOT / "site" / "index.html").read_text()
    problems = []
    if "The two apps are GPL-3.0" in html:
        problems.append("site/index.html still says only 'The two apps are GPL-3.0'")
    if not re.search(r"GPL, version 3: the server and\s+both apps", html):
        problems.append("site/index.html does not say the server and both apps are under the GPL, version 3")
    return problems


def install_names_licence() -> list[str]:
    return [] if SPDX in (ROOT / "INSTALL.md").read_text() else [f"INSTALL.md does not say {SPDX}"]


def one_copyright_line() -> list[str]:
    problems = []
    for rel in ("README.md", "INSTALL.md", "site/index.html"):
        for line in (ROOT / rel).read_text().splitlines():
            if re.search(r"(?i)copyright\s*(\(c\)|©)", line) and COPYRIGHT not in line:
                problems.append(f"{rel} carries another copyright line: {line.strip()[:80]!r}")
    return problems


def main() -> int:
    failed = 0
    for check in (licence_file, readme_names_licence, site_covers_server, install_names_licence,
                  one_copyright_line):
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
    print("PASS test_licence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
