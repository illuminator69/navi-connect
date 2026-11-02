#!/usr/bin/env python3
"""
Worbus P2, H5: lb-bot learns the caller; the hub caches by role class; an older
lb-bot is closed to everyone but the primary user.

Real hub subprocesses, the fake Navidrome (tools/fake_navidrome.py) for sign-in,
and a stub lb-bot as strict as test_lb_upstream_auth's (a call without
`Bearer <HUB_TOKEN>` is 401) that records every request's X-Worbus-* headers as
the raw bytes on the wire, and shapes its answers by them the way lb-bot does:
`addedBy` on wishlist rows for admins only, Fresh's `artistPlays` for the primary
user only, fills naming peers for admins only. Its /api/health says `identity: 1`
or, as an older lb-bot, says nothing.

  test_headers_carry_the_caller       owner, alice, bob, the legacy token (as the
                                      primary user), and a non-Latin name sent as
                                      UTF-8 bytes (R11); all three headers always
  test_cache_key_carries_role_class   /lb/wishlist and /lb/fresh-releases (cached)
                                      and /lb/fills (coalesced): an admin's answer
                                      never reaches a non-admin, nor a primary
                                      user's a non-primary; one class still shares
  test_old_lbbot_refuses_all_but_primary
  test_identity_advertised_in_welcome

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_navidrome import FakeNavidrome  # noqa: E402
from test_signin import PASSWORDS, TOKEN, HubProc, Sock  # noqa: E402

CYR = "жанна"   # a non-Latin user (Navidrome stores it lower-cased)
USERS = {"owner": (PASSWORDS["owner"], True), "alice": (PASSWORDS["alice"], True),
         "bob": (PASSWORDS["bob"], False), CYR: ("zhanna-pass-5", False)}
PASSWORDS = {**PASSWORDS, CYR: "zhanna-pass-5"}


class StubLbBot:
    def __init__(self, identity: bool) -> None:
        self.identity = identity
        self.slow_s = 0.0
        self.seen: list[dict] = []
        self.lock = threading.Lock()
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # noqa: D102
                pass

            def _send(self, status: int, payload) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _answer(self, method: str) -> None:
                path = self.path.partition("?")[0]
                if method == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                # http.server decodes header bytes as latin-1: re-encode to get them back.
                raw_user = self.headers.get("X-Worbus-User")
                row = {"method": method, "path": path,
                       "user": None if raw_user is None else raw_user.encode("latin-1"),
                       "admin": self.headers.get("X-Worbus-Admin"),
                       "primary": self.headers.get("X-Worbus-Primary")}
                with stub.lock:
                    stub.seen.append(row)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"code": "signin_required"})
                    return
                admin = row["admin"] != "0"      # no headers: today's admin service caller
                primary = row["primary"] != "0"
                if path == "/api/health":
                    body = {"ok": True, "epoch": "1-a", "headSeq": 3}
                    if stub.identity:
                        body["identity"] = 1
                    self._send(200, body)
                    return
                if stub.slow_s and path in ("/api/wishlist", "/api/fills"):
                    time.sleep(stub.slow_s)
                if path == "/api/wishlist":
                    item = {"rgid": "r1", "artist": "A", "album": "B"}
                    if admin:
                        item["addedBy"] = "alice"
                    self._send(200, {"wishlist": [item]})
                elif path == "/api/fresh-releases":
                    rel = {"rgid": "f1", "title": "New"}
                    if primary:
                        rel["artistPlays"] = 42
                    self._send(200, {"releases": [rel]})
                elif path == "/api/fills":
                    self._send(200, {"albums": {"m1": {"state": "searching",
                                                       **({"peer": "peer-x"} if admin else {})}},
                                     "gaps": {}, "serverTime": 1})
                else:
                    self._send(404, {"error": "not found"})

            def do_GET(self):  # noqa: N802
                self._answer("GET")

            def do_POST(self):  # noqa: N802
                self._answer("POST")

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def calls(self, path: str) -> list[dict]:
        with self.lock:
            return [r for r in self.seen if r["path"] == path]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def _secrets(h: HubProc) -> dict:
    out = {}
    for u in ("owner", "alice", "bob", CYR):
        st, p = await h.signin(u, password=PASSWORDS[u])
        if st != 200:
            raise RuntimeError(f"{u}'s sign-in answered {st} {p}")
        out[u] = p["deviceSecret"]
    out["legacy"] = TOKEN
    return out


def _get(h: HubProc, path: str, bearer: str) -> tuple[int, object]:
    return h.http("GET", path, None, bearer)


async def t_headers(h: HubProc, lb: StubLbBot, sec: dict) -> list[str]:
    f = []
    want = {"owner": (b"owner", "1", "1"), "alice": (b"alice", "1", "0"),
            "bob": (b"bob", "0", "0"), "legacy": (b"owner", "1", "1"),
            CYR: (CYR.encode("utf-8"), "0", "0")}
    for i, (who, (user, admin, primary)) in enumerate(want.items()):
        n = len(lb.calls("/api/fresh-releases"))
        # A distinct query per caller, so each goes upstream rather than to the cache.
        st, body = await asyncio.to_thread(_get, h, f"/lb/fresh-releases?days={11 + i}"
                                           f"&limit={21 + i}", sec[who])
        rows = lb.calls("/api/fresh-releases")[n:]
        if st != 200 or not rows:
            f.append(f"{who}: {st} {body}, {len(rows)} upstream call(s)")
            continue
        got = (rows[-1]["user"], rows[-1]["admin"], rows[-1]["primary"])
        if got != (user, admin, primary):
            f.append(f"{who}'s call carried {got}, not {(user, admin, primary)}")
    if any(r["user"] is None or r["admin"] is None or r["primary"] is None
           for r in lb.seen if r["path"] != "/api/health"):
        f.append("a proxied call went without all three X-Worbus-* headers")
    return f


async def t_cache_roles(h: HubProc, lb: StubLbBot, sec: dict) -> list[str]:
    f = []
    # /lb/wishlist: alice (admin) first, then bob (not) — bob must not get addedBy.
    st, a = await asyncio.to_thread(_get, h, "/lb/wishlist", sec["alice"])
    st, b = await asyncio.to_thread(_get, h, "/lb/wishlist", sec["bob"])
    if "addedBy" not in json.dumps(a):
        f.append(f"alice's wishlist has no addedBy: {a}")
    if "addedBy" in json.dumps(b):
        f.append("bob was served alice's cached wishlist (addedBy)")
    n = len(lb.calls("/api/wishlist"))
    st, c = await asyncio.to_thread(_get, h, "/lb/wishlist", sec["owner"])   # admin+primary: own class
    st, d = await asyncio.to_thread(_get, h, "/lb/wishlist", sec["legacy"])  # same class as owner
    if len(lb.calls("/api/wishlist")) != n + 1:
        f.append(f"owner and the legacy token (one class) made "
                 f"{len(lb.calls('/api/wishlist')) - n} upstream calls, not 1 (cache unused)")
    # /lb/fresh-releases: the primary user's artistPlays never reaches alice.
    st, o = await asyncio.to_thread(_get, h, "/lb/fresh-releases?days=30&limit=5", sec["owner"])
    st, al = await asyncio.to_thread(_get, h, "/lb/fresh-releases?days=30&limit=5", sec["alice"])
    if "artistPlays" not in json.dumps(o):
        f.append(f"the owner's Fresh lacks artistPlays: {o}")
    if "artistPlays" in json.dumps(al):
        f.append("alice (admin, not primary) was served the owner's artistPlays")
    # /lb/fills is coalesced, not cached: two classes at once are two calls.
    lb.slow_s = 1.0
    n = len(lb.calls("/api/fills"))
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(_get, h, "/lb/fills", sec[w]) for w in ("alice", "bob", "alice")]
        res = [x.result() for x in futs]
    lb.slow_s = 0.0
    calls = len(lb.calls("/api/fills")) - n
    if calls != 2:
        f.append(f"alice, bob, alice at once made {calls} upstream fills calls, not 2")
    bob_body = json.dumps(res[1][1])
    if "peer" in bob_body:
        f.append("bob was handed alice's coalesced fills (peer names)")
    if "peer" not in json.dumps(res[0][1]) or "peer" not in json.dumps(res[2][1]):
        f.append("an admin's fills lost the peer field")
    return f


async def t_old_lbbot(nd: FakeNavidrome) -> list[str]:
    f = []
    lb = StubLbBot(identity=False)
    h = HubProc(nd.url, LBBOT_URL=lb.url)
    try:
        await h.up()
        sec = await _secrets(h)
        await asyncio.sleep(0.5)
        for who in ("alice", "bob", CYR):
            n = len(lb.seen)
            st, p = await asyncio.to_thread(_get, h, "/lb/wishlist", sec[who])
            if (st, p) != (503, {"code": "lbbot_too_old",
                                 "error": "This Worbus Server's lb-bot is too old for "
                                          "signed-in users."}):
                f.append(f"{who} against an older lb-bot: {st} {p}")
            if any(r["path"] != "/api/health" for r in lb.seen[n:]):
                f.append(f"{who}'s call reached the older lb-bot")
        for who in ("owner", "legacy"):
            st, p = await asyncio.to_thread(_get, h, "/lb/wishlist", sec[who])
            if st != 200:
                f.append(f"{who} against an older lb-bot: {st} {p}")
        s = await Sock().hello(h.ws_url, {"id": "w-old", "name": "W"}, token=TOKEN)
        lbw = (s.first("welcome") or {}).get("lb") or {}
        if "identity" in lbw:
            f.append(f"an older lb-bot's welcome.lb carries identity: {lbw}")
        if lbw.get("available") is not True:
            f.append(f"the older lb-bot reads as unavailable: {lbw}")
        await s.close()
    finally:
        h.stop()
        lb.stop()
    return f


async def t_welcome_identity(h: HubProc) -> list[str]:
    f = []
    s = await Sock().hello(h.ws_url, {"id": "w-new", "name": "W"}, token=TOKEN)
    lbw = (s.first("welcome") or {}).get("lb") or {}
    if lbw.get("identity") is not True:
        f.append(f"welcome.lb lacks identity: {lbw}")
    await s.close()
    return f


async def main_async() -> list[str]:
    nd = FakeNavidrome(USERS).start()
    lb = StubLbBot(identity=True)
    failures: list[str] = []
    h = HubProc(nd.url, LBBOT_URL=lb.url)
    try:
        await h.up()
        sec = await _secrets(h)
        await asyncio.sleep(0.5)
        for name, fn in (("test_headers_carry_the_caller", t_headers),
                         ("test_cache_key_carries_role_class", t_cache_roles)):
            try:
                got = await fn(h, lb, sec)
            except Exception:  # noqa: BLE001
                got = [f"raised:\n{traceback.format_exc()}"]
            failures += [f"{name}: {x}" for x in got]
        try:
            got = await t_welcome_identity(h)
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        failures += [f"test_identity_advertised_in_welcome: {x}" for x in got]
    finally:
        h.stop()
        lb.stop()
    try:
        got = await t_old_lbbot(nd)
    except Exception:  # noqa: BLE001
        got = [f"raised:\n{traceback.format_exc()}"]
    failures += [f"test_old_lbbot_refuses_all_but_primary: {x}" for x in got]
    nd.stop()
    return failures


def main() -> int:
    failures = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - lb identity: headers-carry-the-caller(+legacy-as-primary,+utf8-name)/"
          "cache-and-coalesce-by-role-class(wishlist,fresh,fills)/"
          "old-lbbot-refuses-all-but-primary/identity-in-welcome")
    return 0


if __name__ == "__main__":
    sys.exit(main())
