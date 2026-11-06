"""
The shared redaction rule v2 (Worbus P4, ruling R17), for the launcher. Standard library only.

The launcher applies `redact` to every line it relays from the hub and lb-bot, so
`docker logs worbus-server` carries no password, token or Subsonic login (design §4).
Navic and Feishin hold the same rule; all three are tested against the same vectors
(`tools/redaction-vectors.json`, 27 cases). Per line, in this order:

  a'  a sensitive field inside escaped JSON: \\"name\\" : \\"value\\"
  a   a quoted JSON field:                    "name" : "value"
  b   an unquoted field:                      name=value, name: value, name="value"
  c   a bare bearer:                          Bearer <cred>
  d   Subsonic query parameters:              ?u= &t= &s= &p=
  e   userinfo in a URL:                      scheme://user:pass@

A sensitive name contains (any case) password, secret, token, apikey, key or
authorization. The mark is `[removed]`. A credential split across two lines, or a bare
value printed with no name beside it, is not caught by a per-line rule.
"""
from __future__ import annotations

import re

MARK = "[removed]"
_W = r"(?:password|secret|token|apikey|key|authorization)"
_NAME = r"[A-Za-z0-9_.\-]*" + _W + r"[A-Za-z0-9_.\-]*"
_A2 = re.compile(r'\\"(' + _NAME + r')\\"(\s*:\s*)\\"(?:(?!\\").)*\\"', re.I)
_A = re.compile(r'"(' + _NAME + r')"(\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
_B = re.compile(r'(?<![A-Za-z0-9_.\-"])(' + _NAME + r')(\s*[:=]\s*)'
                r'(?:"(?:[^"\\]|\\.)*"|(?:(?:bearer|basic)\s+)?[^\s&,;"\']+)', re.I)
_C = re.compile(r'\b(bearer)\s+[A-Za-z0-9._~+/=\-]+', re.I)
_D = re.compile(r'([?&])([utsp])=([^&\s"\'\\#<>]+)')
_E = re.compile(r'(\b[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@"]+:[^/\s@"]+@')


def _b(m: "re.Match[str]") -> str:
    quoted = m.group(0)[len(m.group(1)) + len(m.group(2)):].startswith('"')
    return m.group(1) + m.group(2) + (f'"{MARK}"' if quoted else MARK)


def redact(line: str) -> str:
    s = _A2.sub(lambda m: '\\"%s\\"%s\\"%s\\"' % (m.group(1), m.group(2), MARK), line)
    s = _A.sub(lambda m: '"%s"%s"%s"' % (m.group(1), m.group(2), MARK), s)
    s = _B.sub(_b, s)
    s = _C.sub(lambda m: m.group(1) + " " + MARK, s)
    s = _D.sub(lambda m: m.group(1) + m.group(2) + "=" + MARK, s)
    s = _E.sub(lambda m: m.group(1) + MARK + "@", s)
    return s
