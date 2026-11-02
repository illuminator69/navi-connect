#!/usr/bin/env python3
"""
Worbus P2, H4: user flags stay fresh, and an idle socket hears about it.

Real hub subprocesses with HUB_USER_REFRESH_SECS=2 (the suites' override; 600 s in
production) against the fake Navidrome (tools/fake_navidrome.py), which models the
native API the refresh uses (R10): `POST /auth/login` as HUB_ND_USER, then
`GET /api/user` with `x-nd-authorization`, which answers 403 to a non-admin's JWT
and 401 to one it did not issue.

  test_idle_socket_hears_demotion        alice is demoted in Navidrome; her idle
                                         socket gets {"t":"user", admin:false};
                                         she then hears no fill; one login serves
                                         every refresh; a revoked JWT logs in once more
  test_deleted_user_gets_4005            bob is deleted: while Navidrome answers 500
                                         nothing happens; once it answers, his socket
                                         gets signed_out + 4005, his credentials go,
                                         his space's data stays (R16)
  test_non_admin_service_account_keeps_signin_flags
                                         HUB_ND_USER=carol (not an admin): /api/user
                                         answers 403; alice keeps the admin flag she
                                         signed in with; logged once

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_navidrome import FakeNavidrome  # noqa: E402
from test_signin import PASSWORDS, TOKEN, USERS, HubProc, Sock  # noqa: E402

FAST = {"HUB_USER_REFRESH_SECS": "2"}


async def _until(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def _connect(h: HubProc, user: str) -> tuple[Sock, dict]:
    st, p = await h.signin(user)
    if st != 200:
        raise RuntimeError(f"{user}'s sign-in answered {st} {p}")
    s = await Sock().hello(h.ws_url, {"id": p["deviceId"], "name": f"{user} phone"},
                           secret=p["deviceSecret"])
    return s, p


async def t_demotion(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, **FAST)
    try:
        await h.up()
        alice, _ = await _connect(h, "alice")
        legacy = await Sock().hello(h.ws_url, {"id": "legacy-r", "name": "Legacy"}, token=TOKEN)
        await asyncio.sleep(2.5)       # a refresh or two with nothing changed
        if any(fr.get("t") == "user" for fr in alice.frames):
            f.append("a refresh with nothing changed sent a `user` frame")
        nd.users["alice"]["admin"] = False
        got = await _until(lambda: any(fr.get("t") == "user" for fr in alice.frames), 6)
        frame = next((fr for fr in alice.frames if fr.get("t") == "user"), None)
        if not got or frame != {"t": "user", "user": {"name": "alice", "admin": False,
                                                       "primary": False}}:
            f.append(f"alice's idle socket heard {frame} within 6 s of her demotion")
        if any(fr.get("t") == "user" for fr in legacy.frames):
            f.append("the owner's socket heard alice's flag change")
        await asyncio.to_thread(h.http, "POST", "/lb/fill",
                                {"kind": "album", "key": "rg-r", "state": "searching"}, TOKEN)
        await asyncio.sleep(0.5)
        if any(fr.get("t") == "fill" for fr in alice.frames):
            f.append("alice heard a fill after her demotion")
        if not any(fr.get("t") == "fill" for fr in legacy.frames):
            f.append("the owner did not hear the fill")
        if nd.login_calls != 1:
            f.append(f"the service account logged in {nd.login_calls} times; the JWT is "
                     "cached and a login happens only on a 401")
        calls = nd.login_calls
        nd.revoke_tokens()
        await _until(lambda: nd.login_calls > calls, 6)
        await asyncio.sleep(2.5)
        if nd.login_calls != calls + 1:
            f.append(f"after a 401 the hub logged in {nd.login_calls - calls} times, not once")
        nd.users["alice"]["admin"] = True
        got = await _until(lambda: sum(fr.get("t") == "user" for fr in alice.frames) >= 2, 6)
        if not got:
            f.append("alice's promotion back was not heard")
        await alice.close()
        await legacy.close()
        with open(h.log_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if any(t in text for t in ("jwt.", PASSWORDS["owner"])):
            f.append("the hub log carries the JWT or the service account's password")
    finally:
        h.stop()
    return f


async def t_deleted(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, **FAST)
    try:
        await h.up()
        bob, bp = await _connect(h, "bob")
        await bob.ws.send(json.dumps({"t": "act", "action": "setQueue",
                                      "tracks": [{"id": "BOBKEEP", "title": "x"}],
                                      "index": 0, "positionMs": 0, "play": False}))
        # A refresh has read the list once, so the JWT is held and a 500 below hits
        # `/api/user` itself (not the login).
        if not await _until(lambda: nd.user_list_calls >= 1, 6):
            f.append("no refresh read the user list within 6 s")
        saved = nd.users.pop("bob")
        nd.mode = "500"
        await asyncio.sleep(5)
        if bob.close_code is not None:
            f.append(f"Navidrome answering 500 signed bob out ({bob.close_code})")
        nd.mode = "ok"
        await bob.wait_closed(6)
        if bob.close_code != 4005 or not any(fr.get("code") == "signed_out"
                                             for fr in bob.frames):
            f.append(f"deleted bob: close {bob.close_code}, last frames {bob.frames[-2:]}")
        await asyncio.sleep(0.3)
        with open(h.state, encoding="utf-8") as fh:
            raw = json.load(fh)
        if any(r.get("user") == "bob" for r in raw.get("deviceAuth", {}).values()):
            f.append("bob's credentials are still in deviceAuth")
        q = [t.get("id") for t in raw.get("users", {}).get("bob", {}).get("session", {})
             .get("queue", [])]
        if q != ["BOBKEEP"]:
            f.append(f"bob's space data was not kept (R16): queue {q}")
        again = await Sock().hello(h.ws_url, {"id": bp["deviceId"], "name": "bob"},
                                   secret=bp["deviceSecret"])
        if await again.wait_closed() != 4005:
            f.append(f"bob's secret still connects after his deletion ({again.close_code})")
        nd.users["bob"] = saved
    finally:
        nd.mode = "ok"
        h.stop()
    return f


async def t_service_not_admin(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, HUB_ND_USER="carol", HUB_ND_PASS=PASSWORDS["carol"], **FAST)
    try:
        await h.up()
        alice, _ = await _connect(h, "alice")
        nd.users["alice"]["admin"] = False
        await asyncio.sleep(6)
        if any(fr.get("t") == "user" for fr in alice.frames):
            f.append("a refused service account still changed alice's flags")
        if alice.close_code is not None:
            f.append(f"a refused service account signed alice out ({alice.close_code})")
        st, p = await h.signin("alice")   # Navidrome's own word at sign-in still counts
        if p.get("user", {}).get("admin") is not False:
            f.append(f"a sign-in did not take Navidrome's flag: {p.get('user')}")
        await alice.close()
        with open(h.log_path, encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh if "refuses HUB_ND_USER's user list" in ln]
        if len(lines) != 1:
            f.append(f"the refused service account was logged {len(lines)} times, not once")
        nd.users["alice"]["admin"] = True
    finally:
        h.stop()
    return f


async def main_async() -> list[str]:
    failures: list[str] = []
    for name, fn in (("test_idle_socket_hears_demotion", t_demotion),
                     ("test_deleted_user_gets_4005", t_deleted),
                     ("test_non_admin_service_account_keeps_signin_flags", t_service_not_admin)):
        nd = FakeNavidrome({k: (v[0], v[1]) for k, v in USERS.items()}).start()
        try:
            got = await fn(nd)
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        finally:
            nd.stop()
        failures += [f"{name}: {x}" for x in got]
    return failures


def main() -> int:
    failures = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - user refresh: idle-socket-hears-demotion+no-fill-after/one-login-jwt-cached/"
          "relogin-once-on-401/deleted-user-4005-after-500s-change-nothing/"
          "deleted-user-space-kept/non-admin-service-account-keeps-flags-logged-once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
