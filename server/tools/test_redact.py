#!/usr/bin/env python3
"""
Worbus P4, lane H fix round (ruling R17): `server/worbus_redact.py`, the launcher's copy
of the shared redaction rule v2 (Navic's and Feishin's are the other two).

  vectors_exact      every case of `redaction-vectors.json` (beside this file, copied
                     verbatim from the round's contract, 27 cases): redact(input) == output
  idempotent         redacting an output again changes nothing
  lbbot_nd_error     lb-bot's `Navidrome error: {e}` line for a refused connection (the
                     form `requests` prints, review I1): u, t and s are gone

Standard library only. Exits non-zero on failure.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import worbus_redact as wr  # noqa: E402

VECTORS = json.loads((HERE / "redaction-vectors.json").read_text(encoding="utf-8"))


def t_vectors_exact() -> list[str]:
    f: list[str] = []
    if VECTORS.get("mark") != wr.MARK:
        f.append(f"mark {wr.MARK!r}, the contract says {VECTORS.get('mark')!r}")
    cases = VECTORS["cases"]
    if len(cases) != 27:
        f.append(f"{len(cases)} vectors, expected the contract's 27")
    for c in cases:
        got = wr.redact(c["input"])
        if got != c["output"]:
            f.append(f"{c['name']}: got {got!r}, expected {c['output']!r}")
    return f


def t_idempotent() -> list[str]:
    return [f"{c['name']}: a second pass changed {c['output']!r}"
            for c in VECTORS["cases"] if wr.redact(c["output"]) != c["output"]]


def t_lbbot_nd_error() -> list[str]:
    line = ("  Navidrome error: HTTPConnectionPool(host='127.0.0.1', port=9): Max retries "
            "exceeded with url: /rest/search3?u=owner&t=5f2b9c0e1a7d&s=q81zxw&v=1.16.1"
            "&c=lbbot&f=json&query=x (Caused by NewConnectionError('refused'))")
    got = wr.redact(line)
    f = [f"{v!r} survived: {got!r}" for v in ("owner", "5f2b9c0e1a7d", "q81zxw") if v in got]
    if "v=1.16.1" not in got or "query=x" not in got:
        f.append(f"a harmless parameter was removed: {got!r}")
    return f


TESTS = (("vectors_exact", t_vectors_exact), ("idempotent", t_idempotent),
         ("lbbot_nd_error", t_lbbot_nd_error))


def main() -> int:
    failures: list[str] = []
    for name, fn in TESTS:
        try:
            got = fn()
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        failures.extend(f"{name}: {x}" for x in got)
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print(f"PASS - worbus redaction: {len(VECTORS['cases'])} vectors; "
          + "/".join(n for n, _ in TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
