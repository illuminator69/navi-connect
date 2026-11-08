#!/usr/bin/env bash
# Worbus P7 (H1): the front door's words. The launch path (README.md, INSTALL.md, site/) must not match the
# banned-word regex (design 9 #5), code blocks and image paths included. site/ may not exist yet.
# Exits 1 on any match and prints file:line.
set -u
cd "$(dirname "$0")/.."
RE='soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)'
targets=(README.md INSTALL.md)
[ -d site ] && targets+=(site)
if grep -rniE --exclude-dir=img -- "$RE" "${targets[@]}"; then
  echo "FAIL check_front_door: banned word on the launch path (above)" >&2
  exit 1
fi
echo "ok   check_front_door (${targets[*]})"
