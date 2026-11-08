#!/usr/bin/env python3
"""Worbus P7, lane H (H1): front_door_has_no_banned_words. Standard library only."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)", re.I)


def front_door_has_no_banned_words() -> list[str]:
    problems = []
    files = [ROOT / "README.md", ROOT / "INSTALL.md"]
    site = ROOT / "site"
    if site.is_dir():
        files += [p for p in sorted(site.rglob("*")) if p.is_file() and p.suffix in {".html", ".css", ".svg", ".txt", ".md", ".yml"}]
    for f in files:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if BANNED.search(line):
                problems.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    run = subprocess.run([str(ROOT / "tools/check_front_door.sh")], capture_output=True, text=True)
    if (run.returncode == 0) != (not problems):
        problems.append(f"check_front_door.sh (exit {run.returncode}) disagrees with the python scan")
    return problems


def main() -> int:
    problems = front_door_has_no_banned_words()
    if problems:
        print("FAIL front_door_has_no_banned_words")
        for p in problems:
            print("  " + p)
        return 1
    print("ok   front_door_has_no_banned_words")
    print("PASS test_front_door")
    return 0


if __name__ == "__main__":
    sys.exit(main())
