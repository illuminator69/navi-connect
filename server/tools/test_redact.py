#!/usr/bin/env python3
"""
Worbus P4, lane H fix round (ruling R17): `server/worbus_redact.py`, the launcher's copy
of the shared redaction rule v2 (Navic's and Feishin's are the other two).

  vectors_exact      every case of `redaction-vectors.json` (beside this file, copied
                     verbatim from the round's contract, 27 cases): redact(input) == output
  idempotent         redacting an output again changes nothing
  lbbot_nd_error     lb-bot's `Navidrome error: {e}` line for a refused connection (the
                     form `requests` prints, review I1): u, t and s are gone
  same_as_reference  (fix round 2, R18) 30 000 seeded random lines built from the rule's
                     own tricky pieces, plus 20 000 URL-shaped ones: the linear-time rule
                     gives exactly what the reference regexes (redact_ref.py's, copied
                     below) give
  linear_1mb         (fix round 2, R18) 1 MB lines of each adversarial shape (dotted and
                     dashed words, `a.b.c-`, runs of `key`, a `"key…`/`\"key…` start, a long
                     `x://aaaa…`, all of them mixed, with real secrets at the end): each is
                     redacted in under a second, in a child process with a deadline, and
                     the secrets at the end are still removed

Standard library only. Exits non-zero on failure.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
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


# redact_ref.py's patterns, verbatim: the behaviour the linear-time module must keep.
_M = "[removed]"
_W = r"(?:password|secret|token|apikey|key|authorization)"
_NAME = r"[A-Za-z0-9_.\-]*" + _W + r"[A-Za-z0-9_.\-]*"
_RA2 = re.compile(r'\\"(' + _NAME + r')\\"(\s*:\s*)\\"(?:(?!\\").)*\\"', re.I)
_RA = re.compile(r'"(' + _NAME + r')"(\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
_RB = re.compile(r'(?<![A-Za-z0-9_.\-"])(' + _NAME + r')(\s*[:=]\s*)(?:"(?:[^"\\]|\\.)*"|(?:(?:bearer|basic)\s+)?[^\s&,;"\']+)', re.I)
_RC = re.compile(r'\b(bearer)\s+[A-Za-z0-9._~+/=\-]+', re.I)
_RD = re.compile(r'([?&])([utsp])=([^&\s"\'\\#<>]+)')
_RE = re.compile(r'(\b[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@"]+:[^/\s@"]+@')


def reference(s: str) -> str:
    s = _RA2.sub(lambda m: '\\"%s\\"%s\\"%s\\"' % (m.group(1), m.group(2), _M), s)
    s = _RA.sub(lambda m: '"%s"%s"%s"' % (m.group(1), m.group(2), _M), s)

    def b(m):
        quoted = m.group(0)[len(m.group(1)) + len(m.group(2)):].startswith('"')
        return m.group(1) + m.group(2) + ('"%s"' % _M if quoted else _M)
    s = _RB.sub(b, s)
    s = _RC.sub(lambda m: m.group(1) + " " + _M, s)
    s = _RD.sub(lambda m: m.group(1) + m.group(2) + "=" + _M, s)
    s = _RE.sub(lambda m: m.group(1) + _M + "@", s)
    return s


PIECES = ("token", "Token", "key", "apiKey", "password", "secret", "authorization", "bearer ",
          "Basic ", "=", ":", " : ", '"', '\\"', "\\", "&", "?", "u", "t", "s", "p", "x", "a",
          "9", "-", ".", "_", "+", "/", "//", "://", "@", " ", "  ", "\t", "'", ",", ";", "#",
          "<", ">", "é", "日", "https", "nd.example", "u=", "&t=", "?s=", "{", "}", "[removed]")


def t_same_as_reference() -> list[str]:
    rng = random.Random(20261106)
    f: list[str] = []
    for i in range(30000):
        line = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 40)))
        if wr.redact(line) != reference(line):
            f.append(f"case {i}: {line!r} -> {wr.redact(line)!r}, the reference gives "
                     f"{reference(line)!r}")
            if len(f) >= 5:
                break
    # URL-shaped lines, for rule (e)'s scheme start: what precedes it, what it holds.
    for i in range(20000):
        line = (rng.choice(("", " ", "_", "é", "日", "9", "-", ".", "x", '"', "/", "a.b-"))
                + "".join(rng.choice(("a", "h", "ttps", "9", "+", ".", "-", "_", "é"))
                          for _ in range(rng.randint(0, 4)))
                + rng.choice(("://", ":/", "://", "//"))
                + "".join(rng.choice(("bob", "", ":", "a:b", "é", "/", " ", "@", "x", '"'))
                          for _ in range(rng.randint(0, 4)))
                + rng.choice(("@", "@h", "", " @"))
                + rng.choice(("", " x://u:p@h", "/p", "&t=1")))
        if wr.redact(line) != reference(line):
            f.append(f"url case {i}: {line!r} -> {wr.redact(line)!r}, the reference gives "
                     f"{reference(line)!r}")
            if len(f) >= 5:
                break
    for c in VECTORS["cases"]:
        if reference(c["input"]) != c["output"]:
            f.append(f"{c['name']}: the copied reference disagrees with the vectors (fix the copy)")
    return f


MB = 1 << 20
SHAPES = {
    "dashed words": "ab-",
    "dotted words": "a.",
    "a.b.c-": "a.b.c-",
    "runs of key": "key",
    "token. runs": "token.",
    "-key-": "-key-",
    "quote then key": None,        # '"' + key...
    "escaped quote then key": None,
    "x:// then a": None,
    "mixed": None,
}
TAIL = " ?u=bob&t=PLANTtail1&s=PLANTtail2 https://bob:PLANTtail3@nd.example/x"


def shape(name: str) -> str:
    unit = SHAPES[name]
    if unit is not None:
        return unit * (MB // len(unit))
    if name == "quote then key":
        return '"' + "key" * (MB // 3)
    if name == "escaped quote then key":
        return '\\"' + "key" * (MB // 3)
    if name == "x:// then a":
        return "x://" + "a" * MB
    parts = [u * (MB // 10 // len(u)) for u in ("ab-", "a.", "a.b.c-", "key", "-key-")]
    parts += ['"' + "key" * (MB // 30), '\\"' + "key" * (MB // 30), "x://" + "a" * (MB // 10),
              "token:" + " " * (MB // 20) + "x"]
    return "".join(parts) + " " + " ".join(parts)


def t_linear_1mb() -> list[str]:
    # A child process per shape, with a deadline: the old rule would hold this one for
    # minutes (re keeps the GIL), so it must not run in-process.
    code = ("import sys,time;sys.path.insert(0,sys.argv[1]);import worbus_redact as wr;"
            "sys.path.insert(0,sys.argv[2]);import test_redact as t;"
            "line=t.shape(sys.argv[3])+t.TAIL;s=time.perf_counter();out=wr.redact(line);"
            "e=time.perf_counter()-s;bad=[v for v in ('PLANTtail1','PLANTtail2','PLANTtail3') if v in out];"
            "print(f'{e:.3f} {len(line)} {\",\".join(bad) or \"-\"}')")
    f: list[str] = []
    for name in SHAPES:
        try:
            r = subprocess.run([sys.executable, "-c", code, str(HERE.parent), str(HERE), name],
                               capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            f.append(f"{name}: not done within 10 s (super-linear)")
            continue
        if r.returncode != 0:
            f.append(f"{name}: the child failed: {r.stderr.strip()[-300:]}")
            continue
        secs, length, bad = r.stdout.split()
        TIMES[name] = float(secs)
        if int(length) < MB:
            f.append(f"{name}: the line is {length} characters, not 1 MB")
        if float(secs) >= 1.0:
            f.append(f"{name}: {secs} s for 1 MB, expected well under a second")
        if bad != "-":
            f.append(f"{name}: secrets at the end survived: {bad}")
    return f


TIMES: dict[str, float] = {}

TESTS = (("vectors_exact", t_vectors_exact), ("idempotent", t_idempotent),
         ("lbbot_nd_error", t_lbbot_nd_error), ("same_as_reference", t_same_as_reference),
         ("linear_1mb", t_linear_1mb))


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
    slowest = max(TIMES.items(), key=lambda kv: kv[1]) if TIMES else ("-", 0.0)
    print(f"PASS - worbus redaction: {len(VECTORS['cases'])} vectors; "
          + "/".join(n for n, _ in TESTS)
          + f"; 1 MB lines: slowest {slowest[0]} {slowest[1]:.3f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
