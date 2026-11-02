#!/usr/bin/env python3
"""
Worbus P2, H2: sign in with the Navidrome account; per-device secrets.

Real hub subprocesses against a fake Navidrome (tools/fake_navidrome.py) that
checks `t == md5(password + s)`, answers code 40 for a wrong pair or an unknown
user and code 50 for anyone reading another user (R10), and can be told to hang
or answer 500. Users: owner (admin, the hub's HUB_ND_USER), alice (admin), bob,
carol (not admins).

  test_signin_ok_returns_secret_and_user
  test_signin_wrong_pair_is_navidrome_refused
  test_signin_navidrome_down_is_unreachable_and_not_counted
  test_signin_rate_limit                         (R1)
  test_signin_keeps_own_device_id_and_mints_for_foreign   (R4)
  test_hello_with_secret_binds_space             (+ welcome.user, R2)
  test_unknown_secret_gets_signed_out_4005       (+ no credential 4005, bad token 4001)
  test_secret_decides_the_id
  test_cast_id_needs_own_bridgedBy
  test_legacy_token_is_primary                   (hello and HTTP bearer)
  test_legacy_token_refused_when_switch_off      (R3: lb-bot's callbacks still pass)
  test_signout_forgets_secret_keeps_device
  test_resignin_replaces_secret
  test_sweep_removes_device_auth                 (in process; R5)
  test_no_secret_in_state_log_or_frames

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

import websockets

os.environ.setdefault("HUB_TOKEN", "test-token")
os.environ.setdefault("NAVIDROME_URL", "http://127.0.0.1:9")
os.environ.setdefault("HUB_ND_USER", "owner")
os.environ.setdefault("HUB_ND_PASS", "owner-pass-71")
HERE = os.path.dirname(os.path.abspath(__file__))
HUB_DIR = os.path.dirname(HERE)
sys.path.insert(0, HUB_DIR)
sys.path.insert(0, HERE)

from fake_navidrome import FakeNavidrome, subsonic_pair  # noqa: E402

TOKEN = "signin-test-token-1234567890ab"
PASSWORDS = {"owner": "owner-pass-71", "alice": "alice-pass-82", "bob": "bob-pass-93",
             "carol": "carol-pass-04"}
USERS = {"owner": (PASSWORDS["owner"], True), "alice": (PASSWORDS["alice"], True),
         "bob": (PASSWORDS["bob"], False), "carol": (PASSWORDS["carol"], False)}

# Everything secret this run handles, for the last test's grep.
SEEN_SECRETS: set[str] = set(PASSWORDS.values())
ALL_FRAMES: list[str] = []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HubProc:
    def __init__(self, nd_url: str, **env_over: str) -> None:
        self.tmp = tempfile.mkdtemp(prefix="signin-")
        self.state = os.path.join(self.tmp, "state.json")
        self.log_path = os.path.join(self.tmp, "hub.log")
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PREVIEW_", "AUDIOMUSE_", "LBBOT_"))}
        env.update({"HUB_TOKEN": TOKEN, "HUB_PORT": str(self.port), "HUB_HOST": "127.0.0.1",
                    "HUB_HEALTH_PORT": str(_free_port()), "HUB_STATE": self.state,
                    "HUB_MIRROR_PLAYQUEUE": "false", "NAVIDROME_URL": nd_url,
                    "HUB_ND_USER": "owner", "HUB_ND_PASS": PASSWORDS["owner"],
                    "HUB_DEBUG": "true"})
        env.update(env_over)
        self.logf = open(self.log_path, "wb")
        self.proc = subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                                     stdout=self.logf, stderr=subprocess.STDOUT)

    async def up(self) -> None:
        end = time.time() + 10
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError("hub did not come up")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.logf.close()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/connect"

    def http(self, method: str, path: str, body=None, bearer: str | None = None,
             raw: bytes | None = None, timeout: float = 20) -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if bearer is not None:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode()
                status = r.status
        except urllib.error.HTTPError as e:
            text, status = e.read().decode(), e.code
        ALL_FRAMES.append(text)
        try:
            return status, json.loads(text)
        except Exception:  # noqa: BLE001
            return status, {"_raw": text}

    async def signin(self, user: str, password: str | None = None, typed: str | None = None,
                     **extra) -> tuple[int, dict]:
        salt, token = subsonic_pair(password if password is not None else PASSWORDS[user])
        SEEN_SECRETS.update({salt, token})
        body = {"username": typed or user, "salt": salt, "token": token,
                "name": f"{user}'s test device", "platform": "test", **extra}
        status, payload = await asyncio.to_thread(self.http, "POST", "/hub/signin", body)
        if isinstance(payload.get("deviceSecret"), str):
            SEEN_SECRETS.add(payload["deviceSecret"])
        return status, payload


class Sock:
    """A raw client: records every frame and the close code."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.ws = None
        self.close_code: int | None = None
        self._task = None

    async def hello(self, url: str, device: dict, secret: str | None = None,
                    token: str | None = None) -> "Sock":
        self.ws = await websockets.connect(url)
        msg: dict = {"t": "hello", "device": {"platform": "test",
                                               "caps": ["controller", "receiver"], **device}}
        if secret is not None:
            msg["auth"] = {"deviceSecret": secret}
        if token is not None:
            msg["token"] = token
        await self.ws.send(json.dumps(msg))
        self._task = asyncio.create_task(self._loop())
        await asyncio.sleep(0.3)
        return self

    async def _loop(self) -> None:
        try:
            async for raw in self.ws:
                ALL_FRAMES.append(raw)
                self.frames.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_code = self.ws.close_code

    def first(self, t: str) -> dict | None:
        return next((f for f in self.frames if f.get("t") == t), None)

    async def wait_closed(self, timeout: float = 3.0) -> int | None:
        end = time.time() + timeout
        while self.close_code is None and time.time() < end:
            await asyncio.sleep(0.05)
        return self.close_code

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


# --------------------------------------------------------------------------- #
async def t_signin_ok(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, p = await h.signin("bob", typed="BoB")
    if st != 200:
        return [f"bob's sign-in answered {st} {p}"]
    if set(p) != {"deviceId", "deviceSecret", "user"}:
        f.append(f"the answer's keys are {sorted(p)}")
    if not isinstance(p.get("deviceSecret"), str) or len(p["deviceSecret"]) < 43:
        f.append("deviceSecret is not token_urlsafe(32)")
    if p.get("user") != {"name": "bob", "admin": False, "primary": False}:
        f.append(f"bob's user is {p.get('user')} (typed 'BoB': the name is Navidrome's)")
    if not isinstance(p.get("deviceId"), str) or not p["deviceId"]:
        f.append("no deviceId")
    st, p = await h.signin("owner")
    if st != 200 or p.get("user") != {"name": "owner", "admin": True, "primary": True}:
        f.append(f"owner's sign-in: {st} {p.get('user')}")
    st, p = await h.signin("alice")
    if st != 200 or p.get("user") != {"name": "alice", "admin": True, "primary": False}:
        f.append(f"alice's sign-in: {st} {p.get('user')}")
    return f


async def t_wrong_pair(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, p = await h.signin("alice", password="not-alices")
    if (st, p) != (401, {"code": "navidrome_refused"}):
        f.append(f"a wrong pair answered {st} {p}")
    st, p = await h.signin("nobody", password="x")
    if (st, p) != (401, {"code": "navidrome_refused"}):
        f.append(f"an unknown user answered {st} {p}")
    for bad in ({"username": "bob"}, {"username": "bob", "salt": "abc", "token": "0" * 32,
                                      "name": "x", "platform": "y"},
                {"username": "bob", "salt": "abcdef", "token": "XYZ", "name": "x",
                 "platform": "y"}):
        st, p = await asyncio.to_thread(h.http, "POST", "/hub/signin", bad)
        if (st, p) != (400, {"code": "bad_request"}):
            f.append(f"a malformed body {sorted(bad)} answered {st} {p}")
    st, p = await asyncio.to_thread(h.http, "POST", "/hub/signin", raw=b"{not json")
    if (st, p) != (400, {"code": "bad_request"}):
        f.append(f"a non-JSON body answered {st} {p}")
    return f


async def t_unreachable(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    before = nd.get_user_calls
    nd.mode = "500"
    for _ in range(6):
        st, p = await h.signin("carol")
        if (st, p) != (503, {"code": "navidrome_unreachable"}):
            f.append(f"Navidrome 500: {st} {p}")
            break
    nd.mode = "hang"
    t0 = time.monotonic()
    st, p = await h.signin("carol")
    took = time.monotonic() - t0
    if (st, p) != (503, {"code": "navidrome_unreachable"}) or not 7.0 < took < 11.0:
        f.append(f"Navidrome hanging: {st} {p} after {took:.1f}s (budget 8 s)")
    nd.mode = "ok"
    st, p = await h.signin("carol")
    if st != 200:
        f.append(f"after 7 unreachable attempts carol was refused ({st} {p}): they were counted")
    if nd.get_user_calls - before != 8:
        f.append(f"Navidrome was asked {nd.get_user_calls - before} times, not 8")
    return f


async def t_rate_limit(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    for i in range(5):
        st, p = await h.signin("carol", password=f"wrong-{i}")
        if st != 401:
            f.append(f"refusal {i + 1} answered {st} {p}")
    calls = nd.get_user_calls
    st, p = await h.signin("carol")
    if (st, p) != (429, {"code": "too_many_attempts"}):
        f.append(f"the 6th attempt (a right pair) answered {st} {p}")
    if nd.get_user_calls != calls:
        f.append("the 6th attempt still asked Navidrome")
    st, p = await h.signin("carol", typed="CAROL")
    if st != 429:
        f.append(f"the limit is not by lower-cased username: {st}")
    st, p = await h.signin("alice")
    if st != 200:
        f.append(f"another user from the same address was limited too: {st}")
    for _ in range(6):   # successes do not count (R1)
        st, p = await h.signin("alice")
    if st != 200:
        f.append(f"six good sign-ins in a row hit the limit: {st}")
    return f


async def t_device_ids(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    legacy = await Sock().hello(h.ws_url, {"id": "owner-phone", "name": "Owner phone"},
                                token=TOKEN)
    st, p = await h.signin("bob", deviceId="bob-new-device")
    if st != 200 or p["deviceId"] == "bob-new-device":
        f.append(f"an unknown deviceId was kept: {p.get('deviceId')}")
    bob_id = p["deviceId"]
    st, p = await h.signin("bob", deviceId=bob_id)
    if p.get("deviceId") != bob_id:
        f.append(f"bob's own id was not kept: {p.get('deviceId')} != {bob_id}")
    st, p = await h.signin("bob", deviceId="owner-phone")
    if p.get("deviceId") in ("owner-phone", None):
        f.append("bob was given the owner's device id")
    st, p = await h.signin("owner", deviceId="owner-phone")
    if p.get("deviceId") != "owner-phone":
        f.append(f"the owner's upgraded phone lost its id: {p.get('deviceId')}")
    await legacy.close()
    return f


async def t_hello_binds(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, p = await h.signin("bob")
    s = await Sock().hello(h.ws_url, {"id": p["deviceId"], "name": "Bob phone"},
                           secret=p["deviceSecret"])
    w = s.first("welcome")
    if w is None:
        return [f"no welcome; frames {s.frames}, close {s.close_code}"]
    if w.get("user") != {"name": "bob", "admin": False, "primary": False}:
        f.append(f"welcome.user {w.get('user')}")
    if w.get("deviceId") != p["deviceId"]:
        f.append(f"welcome.deviceId {w.get('deviceId')}")
    ids = {d["id"] for d in w.get("devices", [])}
    if "owner-phone" in ids:
        f.append(f"bob's welcome lists the owner's devices: {ids}")
    await s.close()
    await asyncio.sleep(0.3)
    with open(h.state, encoding="utf-8") as fh:
        raw = json.load(fh)
    if p["deviceId"] not in {d["id"] for d in raw.get("users", {}).get("bob", {}).get("devices", [])}:
        f.append("bob's device is not in users.bob")
    if p["deviceId"] in {d["id"] for d in raw.get("devices", [])}:
        f.append("bob's device reached the owner's top-level devices")
    return f


async def t_signed_out(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    s = await Sock().hello(h.ws_url, {"id": "x1", "name": "X"}, secret="not-a-secret-" * 3)
    code = await s.wait_closed()
    e = s.first("error")
    if code != 4005 or e != {"t": "error", "code": "signed_out",
                             "message": "Signed out of the Worbus Server."}:
        f.append(f"an unknown secret: close {code}, error {e}")
    if s.first("welcome"):
        f.append("an unknown secret got a welcome")
    s = await Sock().hello(h.ws_url, {"id": "x2", "name": "Y"})
    code = await s.wait_closed()
    if code != 4005 or (s.first("error") or {}).get("code") != "signed_out":
        f.append(f"no credential: close {code}, frames {s.frames}")
    s = await Sock().hello(h.ws_url, {"id": "x3", "name": "Z"}, token="wrong-token")
    code = await s.wait_closed()
    if code != 4001 or (s.first("error") or {}).get("code") != "auth":
        f.append(f"a bad legacy token: close {code}, frames {s.frames}")
    return f


async def t_secret_decides_id(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    owner = await Sock().hello(h.ws_url, {"id": "owner-desk", "name": "Owner desk"}, token=TOKEN)
    st, p = await h.signin("bob")
    bob = await Sock().hello(h.ws_url, {"id": "owner-desk", "name": "Bob, claiming"},
                             secret=p["deviceSecret"])
    w = bob.first("welcome") or {}
    if w.get("deviceId") != p["deviceId"]:
        f.append(f"bob's hello claiming owner-desk registered as {w.get('deviceId')}")
    await asyncio.sleep(0.4)
    if owner.close_code is not None:
        f.append(f"the owner's socket was evicted ({owner.close_code})")
    if any(fr.get("t") == "devices" and any(d.get("name") == "Bob, claiming"
                                            for d in fr.get("devices", []))
           for fr in owner.frames):
        f.append("bob's device appeared in the owner's device list")
    await bob.close()
    await owner.close()
    return f


async def t_cast_id(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, p = await h.signin("bob")
    bid, sec = p["deviceId"], p["deviceSecret"]
    main = await Sock().hello(h.ws_url, {"id": bid, "name": "Bob phone"}, secret=sec)
    cast = await Sock().hello(h.ws_url, {"id": "cast-kitchen", "name": "Kitchen",
                                         "bridgedBy": bid, "caps": ["receiver"]}, secret=sec)
    if (cast.first("welcome") or {}).get("deviceId") != "cast-kitchen":
        f.append(f"an own bridged speaker registered as {(cast.first('welcome') or {}).get('deviceId')}")
    await asyncio.sleep(0.3)
    if main.close_code is not None:
        f.append(f"bob's own bridge evicted his main socket ({main.close_code})")
    st, o = await h.signin("owner")
    foreign = await Sock().hello(h.ws_url, {"id": "cast-livingroom", "name": "Living room",
                                            "bridgedBy": o["deviceId"]}, secret=sec)
    got = (foreign.first("welcome") or {}).get("deviceId")
    if got != bid:
        f.append(f"a cast id with a foreign bridgedBy registered as {got}, not bob's id")
    owner = await Sock().hello(h.ws_url, {"id": o["deviceId"], "name": "Owner"},
                               secret=o["deviceSecret"])
    ids = {d["id"] for d in (owner.first("welcome") or {}).get("devices", [])}
    if "cast-livingroom" in ids or "cast-kitchen" in ids:
        f.append(f"bob's bridge put a speaker in the owner's space: {ids}")
    for s in (main, cast, foreign, owner):
        await s.close()
    return f


async def t_legacy_primary(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    s = await Sock().hello(h.ws_url, {"id": "legacy-1", "name": "Old client"}, token=TOKEN)
    w = s.first("welcome") or {}
    if w.get("user") != {"name": "owner", "admin": True, "primary": True}:
        f.append(f"a legacy hello's welcome.user is {w.get('user')}")
    await s.close()
    st, p = await asyncio.to_thread(h.http, "GET", "/sonic/station/characters", None, TOKEN)
    if st != 200:
        f.append(f"a legacy bearer on a client route answered {st}")
    st, p = await asyncio.to_thread(h.http, "GET", "/sonic/station/characters", None, "nope")
    if st != 401:
        f.append(f"a bad bearer answered {st}")
    st, b = await h.signin("bob")
    st, p = await asyncio.to_thread(h.http, "GET", "/sonic/station/characters", None,
                                    b["deviceSecret"])
    if st != 200:
        f.append(f"a device secret as the bearer answered {st}")
    return f


async def t_switch_off(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, HUB_ACCEPT_SHARED_TOKEN="false")
    try:
        await h.up()
        s = await Sock().hello(h.ws_url, {"id": "legacy-2", "name": "Old"}, token=TOKEN)
        code = await s.wait_closed()
        if code != 4001 or (s.first("error") or {}).get("code") != "auth":
            f.append(f"switch off, legacy hello: close {code}, {s.frames}")
        st, p = await asyncio.to_thread(h.http, "GET", "/sonic/station/characters", None, TOKEN)
        if st != 401:
            f.append(f"switch off, legacy bearer on a client route: {st}")
        st, p = await asyncio.to_thread(h.http, "POST", "/lb/notify", {"albums": []}, TOKEN)
        if st == 401:
            f.append("switch off refused lb-bot's /lb/notify callback (R3: HUB_TOKEN stays)")
        st, b = await h.signin("bob")
        s = await Sock().hello(h.ws_url, {"id": b.get("deviceId"), "name": "Bob"},
                               secret=b.get("deviceSecret"))
        if st != 200 or not s.first("welcome"):
            f.append("switch off, a signed-in device could not connect")
        await s.close()
    finally:
        h.stop()
    return f


async def t_signout(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, p = await h.signin("bob")
    bid, sec = p["deviceId"], p["deviceSecret"]
    s = await Sock().hello(h.ws_url, {"id": bid, "name": "Bob phone"}, secret=sec)
    st, out = await asyncio.to_thread(h.http, "POST", "/hub/signout", {}, sec)
    if (st, out) != (200, {}):
        f.append(f"sign-out answered {st} {out}")
    if await s.wait_closed() != 4005:
        f.append(f"the signed-out device's open socket: close {s.close_code}")
    s2 = await Sock().hello(h.ws_url, {"id": bid, "name": "Bob phone"}, secret=sec)
    if await s2.wait_closed() != 4005:
        f.append(f"a hello with a signed-out secret: close {s2.close_code}")
    st, out = await asyncio.to_thread(h.http, "POST", "/hub/signout", {}, sec)
    if (st, out) != (401, {"code": "signed_out"}):
        f.append(f"a second sign-out answered {st} {out}")
    await asyncio.sleep(0.3)
    with open(h.state, encoding="utf-8") as fh:
        raw = json.load(fh)
    if bid not in {d["id"] for d in raw.get("users", {}).get("bob", {}).get("devices", [])}:
        f.append("sign-out removed the device row")
    st, p = await h.signin("bob", deviceId=bid)
    if p.get("deviceId") != bid:
        f.append(f"after sign-out, a sign-in did not keep the device's id: {p.get('deviceId')}")
    return f


async def t_resignin(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f = []
    st, a = await h.signin("alice")
    st, b = await h.signin("alice", deviceId=a["deviceId"])
    if b.get("deviceId") != a["deviceId"] or b.get("deviceSecret") == a.get("deviceSecret"):
        f.append("the re-sign-in did not keep the id with a new secret")
    old = await Sock().hello(h.ws_url, {"id": a["deviceId"], "name": "A"}, secret=a["deviceSecret"])
    if await old.wait_closed() != 4005:
        f.append(f"the replaced secret still connects ({old.close_code})")
    new = await Sock().hello(h.ws_url, {"id": a["deviceId"], "name": "A"}, secret=b["deviceSecret"])
    if not new.first("welcome"):
        f.append("the new secret does not connect")
    await new.close()
    await asyncio.sleep(0.3)
    with open(h.state, encoding="utf-8") as fh:
        rows = [r for r in json.load(fh).get("deviceAuth", {}).values()
                if r.get("user") == "alice" and r.get("deviceId") == a["deviceId"]]
    if len(rows) != 1:
        f.append(f"{len(rows)} credentials for one (alice, device)")
    return f


def t_sweep() -> list[str]:
    f = []
    import hub as hubmod
    tmp = tempfile.mkdtemp(prefix="signin-sweep-")
    hubmod.STATE_PATH = os.path.join(tmp, "state.json")
    hubmod.ND_USER = "owner"
    h = hubmod.Hub()
    now = int(time.time() * 1000)
    day = 86_400_000
    bob = h.space_for("bob")
    bob.devices["old-dev"] = hubmod.Device(id="old-dev", name="old", last_seen=now - 9 * day)
    bob.devices["live-dev"] = hubmod.Device(id="live-dev", name="live", last_seen=now - day)
    rows = {"a" * 64: ("old-dev", now - 9 * day),      # device swept: goes
            "b" * 64: ("live-dev", now - 30 * day),    # device still there: stays
            "c" * 64: ("never-hello", now - day),      # no device, 1 day old: stays (R5)
            "d" * 64: ("never-hello-2", now - 8 * day)}  # no device, 8 days: goes (R5)
    for k, (did, at) in rows.items():
        h.device_auth[k] = {"user": "bob", "deviceId": did, "createdAt": at}
    asyncio.run(h._sweep_once())  # noqa: SLF001
    if "old-dev" in bob.devices:
        f.append("the sweep kept a 9-day-old device")
    if set(h.device_auth) != {"b" * 64, "c" * 64}:
        f.append(f"after the sweep the credentials are {sorted(k[0] for k in h.device_auth)}")
    with open(hubmod.STATE_PATH, encoding="utf-8") as fh:
        if set(json.load(fh).get("deviceAuth", {})) != {"b" * 64, "c" * 64}:
            f.append("the sweep's credential removal was not saved")
    # Minor 1: a new credential reaches the fsynced backup.
    h.device_auth["e" * 64] = {"user": "bob", "deviceId": "live-dev", "createdAt": now}
    h._save()  # noqa: SLF001
    with open(hubmod.STATE_PATH + ".bak", encoding="utf-8") as fh:
        if "e" * 64 not in json.load(fh).get("deviceAuth", {}):
            f.append("a new credential did not reach the backup")
    return f


def t_no_secrets(h: HubProc) -> list[str]:
    f = []
    texts = {}
    for p in (h.state, h.state + ".bak", h.log_path):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[os.path.basename(p)] = fh.read()
        except FileNotFoundError:
            pass
    texts["frames"] = "\n".join(ALL_FRAMES)
    # The frames include sign-in ANSWERS, which carry the device's own secret by
    # design: those are the one place a secret may appear.
    answers = [x for x in ALL_FRAMES if '"deviceSecret"' in x]
    texts["frames"] = "\n".join(x for x in ALL_FRAMES if '"deviceSecret"' not in x)
    for where, text in texts.items():
        leaked = [s for s in SEEN_SECRETS if s and s in text]
        if leaked:
            f.append(f"{len(leaked)} secret value(s) in {where} (e.g. a {len(leaked[0])}-char one)")
    if not any("secret length" in ln for ln in texts.get("hub.log", "").splitlines()):
        f.append("the hub log does not name a sign-in's secret length")
    if not answers:
        f.append("no sign-in answer was seen (the grep had nothing to check)")
    if len(SEEN_SECRETS) < 20:
        f.append(f"only {len(SEEN_SECRETS)} secrets were collected")
    return f


async def main_async() -> list[str]:
    nd = FakeNavidrome(USERS).start()
    failures: list[str] = []
    h = HubProc(nd.url)
    try:
        await h.up()
        for name, fn in (("test_signin_ok_returns_secret_and_user", t_signin_ok),
                         ("test_signin_wrong_pair_is_navidrome_refused", t_wrong_pair),
                         ("test_signin_navidrome_down_is_unreachable_and_not_counted",
                          t_unreachable),
                         ("test_signin_rate_limit", t_rate_limit),
                         ("test_signin_keeps_own_device_id_and_mints_for_foreign", t_device_ids),
                         ("test_hello_with_secret_binds_space", t_hello_binds),
                         ("test_unknown_secret_gets_signed_out_4005", t_signed_out),
                         ("test_secret_decides_the_id", t_secret_decides_id),
                         ("test_cast_id_needs_own_bridgedBy", t_cast_id),
                         ("test_legacy_token_is_primary", t_legacy_primary),
                         ("test_signout_forgets_secret_keeps_device", t_signout),
                         ("test_resignin_replaces_secret", t_resignin)):
            try:
                got = await fn(h, nd)
            except Exception:  # noqa: BLE001
                got = [f"raised:\n{traceback.format_exc()}"]
            failures += [f"{name}: {x}" for x in got]
        try:
            got = await t_switch_off(nd)
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        failures += [f"test_legacy_token_refused_when_switch_off: {x}" for x in got]
    finally:
        h.stop()
        nd.stop()
    failures += [f"test_no_secret_in_state_log_or_frames: {x}" for x in t_no_secrets(h)]
    return failures


def main() -> int:
    failures = asyncio.run(main_async())
    try:
        failures += [f"test_sweep_removes_device_auth: {x}" for x in t_sweep()]
    except Exception:  # noqa: BLE001
        failures.append(f"test_sweep_removes_device_auth: raised:\n{traceback.format_exc()}")
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - signin: ok-secret-and-user/wrong-pair-refused/unreachable-not-counted/"
          "rate-limit-5-per-addr-user/own-id-kept-foreign-minted/hello-binds-space/"
          "unknown-secret-4005+none-4005+bad-token-4001/secret-decides-id/"
          "cast-id-needs-own-bridgedBy/legacy-token-primary/switch-off-refuses-legacy/"
          "signout-keeps-device/resignin-replaces/sweep-removes-device-auth/"
          "no-secret-in-state-log-frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
