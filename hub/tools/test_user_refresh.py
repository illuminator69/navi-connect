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
                                         his space is archived with its data (R37), and
                                         a new bob account signs in to an empty space
  test_non_admin_service_account_keeps_signin_flags
                                         HUB_ND_USER=carol (not an admin): /api/user
                                         answers 403; alice keeps the admin flag she
                                         signed in with; logged once
  test_failed_reads_back_off             (R10, review Important 1) while /auth/login
                                         answers 429, and again while Navidrome
                                         answers 500: a burst of hellos and lb calls
                                         inside one interval makes at most one login,
                                         and the next interval tries again
  test_partial_user_list_signs_nobody_out
                                         (R21) /api/user answering fewer rows than its
                                         X-Total-Count: bob, missing from it, stays
                                         signed in and is not archived (R37); alice,
                                         listed, is still demoted
  test_primary_user_never_archived       (R37) a complete list without the primary
                                         user: their space stays at the top level and
                                         their socket stays open

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

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

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
        # R37: archived, not left under `users` for the next bob.
        if "bob" in raw.get("users", {}):
            f.append("bob's space is still under `users` after his deletion")
        arch = {k: v for k, v in (raw.get("archivedUsers") or {}).items()
                if k.startswith("bob@")}
        entry = next(iter(arch.values()), {})
        q = [t.get("id") for t in (entry.get("space") or {}).get("session", {}).get("queue", [])]
        devs = json.dumps((entry.get("space") or {}).get("devices"))
        if len(arch) != 1 or q != ["BOBKEEP"] or bp["deviceId"] not in devs:
            f.append(f"bob's archive: {len(arch)} entr(y/ies), queue {q}, his device "
                     f"{'kept' if bp['deviceId'] in devs else 'missing'}")
        again = await Sock().hello(h.ws_url, {"id": bp["deviceId"], "name": "bob"},
                                   secret=bp["deviceSecret"])
        if await again.wait_closed() != 4005:
            f.append(f"bob's secret still connects after his deletion ({again.close_code})")
        # A new account named bob: a fresh space, nothing of the old one.
        nd.users["bob"] = {"password": "new-bob-pass-77", "admin": False}
        st, nb = await h.signin("bob", password="new-bob-pass-77", deviceId=bp["deviceId"])
        if st != 200:
            f.append(f"the new bob's sign-in answered {st} {nb}")
        else:
            s2 = await Sock().hello(h.ws_url, {"id": nb["deviceId"], "name": "new bob"},
                                    secret=nb["deviceSecret"])
            w = s2.first("welcome") or {}
            wq = [t.get("id") for t in (w.get("session") or {}).get("queue", [])]
            wd = [d.get("id") for d in w.get("devices", [])]
            if wq or wd != [nb["deviceId"]] or w.get("savedQueues") or w.get("mixes"):
                f.append(f"the new bob inherited the old one's space: queue {wq}, devices "
                         f"{wd}, savedQueues {len(w.get('savedQueues') or [])}")
            if nb["deviceId"] == bp["deviceId"]:
                f.append("the new bob was handed the old bob's device id")
            await s2.close()
            await asyncio.sleep(0.3)
            with open(h.state, encoding="utf-8") as fh:
                raw2 = json.load(fh)
            if not any(k.startswith("bob@") for k in raw2.get("archivedUsers") or {}):
                f.append("the old bob's archive did not survive the new bob's sign-in")
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


async def _burst(h: HubProc, bearers: list[str], seconds: float) -> int:
    """Hellos with the legacy token and lb calls with each bearer, for `seconds`:
    every one of them asks the hub to refresh flags that are stale. Returns how
    many were made."""
    made = 0
    end = time.monotonic() + seconds
    with ThreadPoolExecutor(4) as ex:
        while time.monotonic() < end:
            socks = await asyncio.gather(*(Sock().hello(
                h.ws_url, {"id": f"burst-{made}-{i}", "name": "Burst"}, token=TOKEN)
                for i in range(3)))
            futs = [ex.submit(h.http, "GET", "/lb/wishlist", None, b, None, 5)
                    for b in bearers for _ in range(3)]
            for fu in futs:
                fu.result()
            for s in socks:
                await s.close()
            made += len(socks) + len(futs)
    return made


async def t_backoff(nd: FakeNavidrome) -> list[str]:
    f = []
    # An lb-bot nobody answers at: every lb call still reads the caller's flags first.
    env = {"HUB_USER_REFRESH_SECS": "4", "LBBOT_URL": "http://127.0.0.1:9"}
    for case in ("login answering 429", "Navidrome answering 500"):
        h = HubProc(nd.url, **env)
        try:
            await h.up()
            st, alice = await h.signin("alice")
            if st != 200:
                return [f"alice's sign-in answered {st} {alice}"]
            nd.login_calls = 0
            if case == "login answering 429":
                nd.login_limit = 0
            else:
                nd.mode = "500"
            t0 = time.monotonic()
            made = await _burst(h, [TOKEN, alice["deviceSecret"]], 2.5)
            within = nd.login_calls
            if within > 1:
                f.append(f"{case}: {made} hellos and lb calls inside one interval made "
                         f"{within} logins, not at most 1")
            if within < 1:
                f.append(f"{case}: no login was tried at all (the burst never asked)")
            # The next interval tries again: not stuck after a failure.
            await asyncio.sleep(max(0.0, 4.3 - (time.monotonic() - t0)))
            await asyncio.to_thread(h.http, "GET", "/lb/wishlist", None, TOKEN, None, 5)
            if not await _until(lambda: nd.login_calls > within, 3):
                f.append(f"{case}: no login was tried in the next interval")
            if nd.login_calls > within + 1:
                f.append(f"{case}: the next interval made {nd.login_calls - within} logins")
        finally:
            nd.login_limit = None
            nd.mode = "ok"
            h.stop()
    return f


async def t_partial_list(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, **FAST)
    try:
        await h.up()
        bob, _ = await _connect(h, "bob")
        alice, _ = await _connect(h, "alice")
        if not await _until(lambda: nd.user_list_calls >= 1, 6):
            f.append("no refresh read the user list within 6 s")
        # owner, alice, bob, carol: two rows of four, bob cut off.
        nd.list_limit = 2
        nd.users["alice"]["admin"] = False
        calls = nd.user_list_calls
        await _until(lambda: nd.user_list_calls >= calls + 2, 8)
        await asyncio.sleep(0.5)
        if nd.user_list_calls < calls + 2:
            f.append("the partial list was not read twice within 8 s")
        if bob.close_code is not None or any(fr.get("code") == "signed_out"
                                             for fr in bob.frames):
            f.append(f"a partial /api/user list signed bob out ({bob.close_code})")
        with open(h.state, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not any(r.get("user") == "bob" for r in raw.get("deviceAuth", {}).values()):
            f.append("a partial /api/user list dropped bob's credentials")
        if raw.get("archivedUsers") or "bob" not in raw.get("users", {}):
            f.append("a partial /api/user list archived bob's space (R37)")
        frame = next((fr for fr in alice.frames if fr.get("t") == "user"), None)
        if frame != {"t": "user", "user": {"name": "alice", "admin": False, "primary": False}}:
            f.append(f"alice, listed in the partial answer, heard {frame} (flags not updated)")
        await bob.close()
        await alice.close()
    finally:
        nd.list_limit = None
        nd.users["alice"]["admin"] = True
        h.stop()
    return f


async def t_primary_not_archived(nd: FakeNavidrome) -> list[str]:
    """R37: the primary user is never archived, even by a complete list that does
    not name them (the service account's own user)."""
    f = []
    h = HubProc(nd.url, **FAST)
    try:
        await h.up()
        owner, _ = await _connect(h, "owner")
        await owner.ws.send(json.dumps({"t": "act", "action": "setQueue",
                                        "tracks": [{"id": "OWNKEEP", "title": "x"}],
                                        "index": 0, "positionMs": 0, "play": False}))
        if not await _until(lambda: nd.user_list_calls >= 1, 6):
            f.append("no refresh read the user list within 6 s")
        nd.unlisted = {"owner"}
        calls = nd.user_list_calls
        await _until(lambda: nd.user_list_calls >= calls + 2, 8)
        await asyncio.sleep(0.5)
        if nd.user_list_calls < calls + 2:
            f.append("the list without owner was not read twice within 8 s")
        if owner.close_code is not None:
            f.append(f"the primary user was signed out ({owner.close_code})")
        with open(h.state, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("archivedUsers"):
            f.append(f"archived: {sorted(raw['archivedUsers'])}")
        q = [t.get("id") for t in raw.get("session", {}).get("queue", [])]
        if q != ["OWNKEEP"]:
            f.append(f"the primary user's top-level queue is {q}")
        await owner.close()
    finally:
        nd.unlisted = set()
        h.stop()
    return f


async def main_async() -> list[str]:
    failures: list[str] = []
    for name, fn in (("test_idle_socket_hears_demotion", t_demotion),
                     ("test_deleted_user_gets_4005", t_deleted),
                     ("test_non_admin_service_account_keeps_signin_flags", t_service_not_admin),
                     ("test_failed_reads_back_off", t_backoff),
                     ("test_partial_user_list_signs_nobody_out", t_partial_list),
                     ("test_primary_user_never_archived", t_primary_not_archived)):
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
          "deleted-user-space-archived-new-bob-empty/non-admin-service-account-keeps-flags-logged-once/"
          "failed-reads-back-off(429,500)-one-login-per-interval/"
          "partial-user-list-signs-nobody-out-archives-nothing/primary-user-never-archived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
