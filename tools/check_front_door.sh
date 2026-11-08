#!/usr/bin/env bash
# Worbus P7 (H1): the front door's words. The launch path (README.md, INSTALL.md, site/) must not match the
# banned-word regex (design 9 #5), code blocks and image paths included. site/ may not exist yet.
# Prints file:line and exits 1 on a match. It fails closed: a missing README.md or INSTALL.md, or a grep that
# itself fails (exit above 1), exits 2, never "clean".
# Also keep "acquisition" and "acquire" in any form off these pages: the regex's `acquir` does not match
# "acquisition", so tools/test_front_door.py checks `acqui` as well.
set -u
cd "$(dirname "$0")/.."
RE='soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)'
for f in README.md INSTALL.md; do
  if [ ! -s "$f" ]; then
    echo "FAIL check_front_door: $f is missing or empty, so it was not checked" >&2
    exit 2
  fi
done
targets=(README.md INSTALL.md)
# Every text file under site/ is scanned whatever its suffix; the one exclusion is the favicon, by its exact path:
# it is path data and the SVG paint property `fill` (CSS `fill:`, `fill-rule`), which matches \bfill\b and is not
# copy; it holds no words. -I skips binary files (the PNGs in img/).
if [ -d site ]; then
  while IFS= read -r -d '' f; do targets+=("$f"); done < <(find site -type f ! -path site/favicon.svg -print0 | sort -z)
fi
grep -rniEI -- "$RE" "${targets[@]}"
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "FAIL check_front_door: banned word on the launch path (above)" >&2
  exit 1
elif [ "$rc" -ne 1 ]; then
  echo "FAIL check_front_door: grep failed (exit $rc), nothing was checked" >&2
  exit 2
fi
echo "ok   check_front_door (README.md INSTALL.md and $(( ${#targets[@]} - 2 )) files under site/)"
