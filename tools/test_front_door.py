#!/usr/bin/env python3
"""Worbus P7, lane H (H1): front_door_has_no_banned_words. Standard library only."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACQUI = re.compile(r"acqui", re.I)  # not in the fixed regex (it misses "acquisition"); kept off the pages too
BANNED = re.compile(r"soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)", re.I)


def front_door_has_no_banned_words() -> list[str]:
    problems = []
    files = [ROOT / "README.md", ROOT / "INSTALL.md"]
    site = ROOT / "site"
    if site.is_dir():
        # every text file under site/ whatever its suffix; the one exclusion is the favicon by path (its path data and
        # the SVG paint property `fill` are not copy). Binary files (the PNGs) are skipped, as grep -I does.
        files += [p for p in sorted(site.rglob("*")) if p.is_file() and p.relative_to(ROOT).as_posix() != "site/favicon.svg"]
    for f in files:
        raw = f.read_bytes()
        if b"\0" in raw:
            continue
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if BANNED.search(line) or ACQUI.search(line):
                problems.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    run = subprocess.run([str(ROOT / "tools/check_front_door.sh")], capture_output=True, text=True)
    if (run.returncode == 0) != (not any(BANNED.search(p) for p in problems)):
        problems.append(f"check_front_door.sh (exit {run.returncode}) disagrees with the python scan")
    return problems


def front_door_check_fails_closed() -> list[str]:
    """A missing README.md must not read as clean: run the script against a copy of the tree without it."""
    import shutil
    import tempfile
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "tools").mkdir()
        shutil.copy(ROOT / "tools/check_front_door.sh", Path(tmp) / "tools")
        (Path(tmp) / "INSTALL.md").write_text("clean\n")
        run = subprocess.run([str(Path(tmp) / "tools/check_front_door.sh")], capture_output=True, text=True)
        if run.returncode == 0:
            problems.append("exit 0 with README.md missing")
        (Path(tmp) / "README.md").write_text("clean\n")
        run = subprocess.run([str(Path(tmp) / "tools/check_front_door.sh")], capture_output=True, text=True)
        if run.returncode != 0:
            problems.append(f"exit {run.returncode} on two clean files")
        (Path(tmp) / "README.md").write_text("it can acquire it\n")
        run = subprocess.run([str(Path(tmp) / "tools/check_front_door.sh")], capture_output=True, text=True)
        if run.returncode != 1:
            problems.append(f"exit {run.returncode} on a banned word, want 1")
    return problems


def main() -> int:
    problems = front_door_has_no_banned_words() + front_door_check_fails_closed()
    if problems:
        print("FAIL front_door_has_no_banned_words or front_door_check_fails_closed")
        for p in problems:
            print("  " + p)
        return 1
    print("ok   front_door_has_no_banned_words\nok   front_door_check_fails_closed")
    print("PASS test_front_door")
    return 0


if __name__ == "__main__":
    sys.exit(main())
