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
# Linear time (fix round 2, ruling R18). The reference spells a name as
# `[name chars]* W [name chars]*`; every place it is used is preceded and followed by
# a character outside the name class, so a match is always one whole run of name
# characters that contains W. Spelled that way here, a lookahead finds W once and a
# possessive run takes the rest, instead of trying every split of a long run (which is
# quadratic on `key-key-key…` or `a.b.c-…`). Same matches, same output.
_NAME = r"(?=[A-Za-z0-9_.\-]*?" + _W + r")[A-Za-z0-9_.\-]++"
_A2 = re.compile(r'\\"(' + _NAME + r')\\"(\s*:\s*)\\"(?:(?!\\").)*\\"', re.I)
_A = re.compile(r'"(' + _NAME + r')"(\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
_B = re.compile(r'(?<![A-Za-z0-9_.\-"])(' + _NAME + r')(\s*[:=]\s*)'
                r'(?:"(?:[^"\\]|\\.)*"|(?:(?:bearer|basic)\s+)?[^\s&,;"\']+)', re.I)
_C = re.compile(r'\b(bearer)\s+[A-Za-z0-9._~+/=\-]+', re.I)
_D = re.compile(r'([?&])([utsp])=([^&\s"\'\\#<>]+)')
# Rule (e), the reference's `(\b[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@"]+:[^/\s@"]+@`.
# As one regex it retries the scheme from every word start inside a long dotted or
# dashed run; here the tail after `://` is found first and the scheme is checked by
# walking back over its run once. The scheme text is kept as it is either way, so only
# whether some start in that run is valid matters: an ASCII letter at a word boundary.
_E_TAIL = re.compile(r'://[^/\s:@"]++:[^/\s@"]++@')
_SCHEME = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_LETTER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_WORD = re.compile(r"\w")


def _e(s: str) -> str:
    out = []
    last = pos = 0
    while True:
        m = _E_TAIL.search(s, pos)
        if m is None:
            break
        i = m.start()
        j = i
        while j > last and s[j - 1] in _SCHEME:
            j -= 1
        if any(s[k] in _LETTER and (k == 0 or not _WORD.match(s[k - 1]))
               for k in range(j, i)):
            out.append(s[last:i] + "://" + MARK + "@")
            last = pos = m.end()
        else:
            pos = i + 1
    out.append(s[last:])
    return "".join(out)


def _b(m: "re.Match[str]") -> str:
    quoted = m.group(0)[len(m.group(1)) + len(m.group(2)):].startswith('"')
    return m.group(1) + m.group(2) + (f'"{MARK}"' if quoted else MARK)


def redact(line: str) -> str:
    s = _A2.sub(lambda m: '\\"%s\\"%s\\"%s\\"' % (m.group(1), m.group(2), MARK), line)
    s = _A.sub(lambda m: '"%s"%s"%s"' % (m.group(1), m.group(2), MARK), s)
    s = _B.sub(_b, s)
    s = _C.sub(lambda m: m.group(1) + " " + MARK, s)
    s = _D.sub(lambda m: m.group(1) + m.group(2) + "=" + MARK, s)
    return _e(s)
