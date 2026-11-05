#!/usr/bin/env python3
"""
Worbus P3, lane H (H1, H2): the server's identity, an open `GET /hub/info`, and
`welcome.server`.

Real hub subprocesses against the fake Navidrome (tools/fake_navidrome.py), on the
pattern of test_signin.py. The configuration rules (HUB_NAME, HUB_PUBLIC_URL) are
tested pure in test_startup_config.py; this file proves what a running hub says.

H1 (identity and /hub/info):
  info_shape_without_auth            200 JSON, the four keys and nothing else
  info_same_bytes_with_bearer        a device secret, the legacy token, garbage: same bytes
  info_has_no_address                with HUB_PUBLIC_URL set, no url/ws/http in the body
  info_other_methods_405             POST/PUT/DELETE/PATCH/OPTIONS/HEAD, bodies drained
  server_id_survives_restart
  server_id_survives_project2_rewrite
  bad_server_id_file_refuses_start   named file, never replaced, nothing else written
  server_id_never_replaces_a_racing_file   a file that appears mid-mint wins (os.link)
  info_never_touches_navidrome       20 requests, zero Navidrome calls; HUB_DEBUG log line
H2 (welcome.server):
  welcome_carries_server_id_and_name
  welcome_public_url_when_set        + https_public_url_becomes_wss, trailing_connect_and_slash_dropped,
                                       path_kept (at the welcome; the normaliser is pure-tested too)
  welcome_no_public_url_key_when_unset
  server_field_same_for_every_user

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
HUB_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from fake_navidrome import FakeNavidrome, subsonic_pair  # noqa: E402

TOKEN = "hubinfo-test-token-1234567890ab"
PASSWORDS = {"owner": "owner-pass-71", "alice": "alice-pass-82", "bob": "bob-pass-93"}
USERS = {"owner": (PASSWORDS["owner"], True), "alice": (PASSWORDS["alice"], True),
         "bob": (PASSWORDS["bob"], False)}
HEX32 = re.compile(r"[0-9a-f]{32}")


_HANDED_OUT: set[int] = set()  # every port this run has chosen, so none is chosen twice


def _free_port() -> int:
    """A free loopback port from 4600-4789: clear of the services and of the round's
    reserved ranges (4790, 4797-4799, 48xx-49xx). A port is never returned twice in
    one run: a probe socket is closed before the hub binds, so a second probe could
    otherwise land on a port a hub (or its health server) is about to take."""
    for _ in range(400):
        port = random.randint(4600, 4789)
        if port in _HANDED_OUT:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            _HANDED_OUT.add(port)
            return port
    raise RuntimeError("no free port in 4600-4789")


class HubProc:
    """One `hub.py` subprocess. `tmp` may be passed to restart on the same state."""

    def __init__(self, nd_url: str, tmp: str | None = None, **env_over: str) -> None:
        self.own_tmp = tmp is None
        self.tmp = tmp or tempfile.mkdtemp(prefix="hubinfo-")
        self.state = os.path.join(self.tmp, "state.json")
        self.id_file = self.state + ".server-id"
        self.log_path = os.path.join(self.tmp, f"hub-{int(time.time() * 1000)}.log")
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HUB_", "PREVIEW_", "AUDIOMUSE_", "LBBOT_", "NAVIDROME_"))}
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
            if self.proc.poll() is not None:
                raise RuntimeError(f"hub exited {self.proc.returncode}: {self.log()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError("hub did not come up")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.logf.close()

    def cleanup(self) -> None:
        self.stop()
        if self.own_tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self) -> str:
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/connect"

    def raw(self, method: str, path: str, body: bytes | None = None,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
        """Any method, the raw answer: (status, lower-cased headers, body bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def info(self, bearer: str | None = None) -> tuple[int, dict, bytes]:
        return self.raw("GET", "/hub/info",
                        headers={"Authorization": f"Bearer {bearer}"} if bearer else None)

    def signin(self, user: str) -> str:
        salt, token = subsonic_pair(PASSWORDS[user])
        body = json.dumps({"username": user, "salt": salt, "token": token,
                           "name": f"{user}'s test device", "platform": "test"}).encode()
        status, _h, raw = self.raw("POST", "/hub/signin", body,
                                   {"Content-Type": "application/json"})
        if status != 200:
            raise RuntimeError(f"sign-in of {user} answered {status}: {raw!r}")
        return json.loads(raw)["deviceSecret"]

    async def welcome(self, secret: str | None = None, token: str | None = None,
                      device_id: str = "dev-1") -> dict:
        """Open a socket, say hello, return the welcome frame, close."""
        async with websockets.connect(self.ws_url) as ws:
            msg: dict = {"t": "hello", "device": {"id": device_id, "name": "probe",
                                                   "platform": "test",
                                                   "caps": ["controller", "receiver"]}}
            if secret is not None:
                msg["auth"] = {"deviceSecret": secret}
            if token is not None:
                msg["token"] = token
            await ws.send(json.dumps(msg))
            for _ in range(20):
                frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if frame.get("t") == "welcome":
                    return frame
        raise RuntimeError("no welcome")


def _fail_if(cond: bool, msg: str, out: list[str]) -> None:
    if cond:
        out.append(msg)


# --------------------------------------------------------------------------- #
# H1
# --------------------------------------------------------------------------- #
async def t_info_shape_without_auth(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    status, headers, raw = h.info()
    _fail_if(status != 200, f"status {status}", f)
    _fail_if(not headers.get("content-type", "").startswith("application/json"),
             f"content-type {headers.get('content-type')!r}", f)
    body = json.loads(raw)
    _fail_if(set(body) != {"service", "serverId", "name", "signin"},
             f"keys {sorted(body)}", f)
    _fail_if(body.get("service") != "worbus", f"service {body.get('service')!r}", f)
    _fail_if(not HEX32.fullmatch(str(body.get("serverId"))),
             f"serverId {body.get('serverId')!r} is not 32 lower-case hex", f)
    _fail_if(body.get("name") != "Worbus Server", f"name {body.get('name')!r}", f)
    _fail_if(body.get("signin") is not True, f"signin {body.get('signin')!r}", f)
    # The query string and a trailing slash are ignored, as for the other /hub/ routes.
    for path in ("/hub/info/", "/hub/info?x=1"):
        s2, _h2, raw2 = h.raw("GET", path)
        _fail_if(s2 != 200 or raw2 != raw, f"{path}: {s2}", f)
    return f


async def t_info_same_bytes_with_bearer(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    secret = h.signin("bob")
    plain = h.info()
    _fail_if(plain[0] != 200, f"the unauthenticated answer is {plain[0]}, not 200", f)
    for label, bearer in (("device secret", secret), ("legacy token", TOKEN),
                          ("garbage", "not-a-credential"), ("wrong-length", "x" * 400)):
        got = h.info(bearer)
        _fail_if(got[0] != plain[0] or got[2] != plain[2],
                 f"{label}: {got[0]} {got[2]!r} differs from the unauthenticated "
                 f"{plain[0]} {plain[2]!r}", f)
        _fail_if(got[1].get("content-type") != plain[1].get("content-type"),
                 f"{label}: content-type differs", f)
    return f


async def t_info_has_no_address(h: HubProc, nd: FakeNavidrome) -> list[str]:
    """`h` runs with HUB_PUBLIC_URL set."""
    f: list[str] = []
    w = await h.welcome(token=TOKEN)
    _fail_if((w.get("server") or {}).get("publicUrl") != "wss://hub.test.example",
             f"precondition: HUB_PUBLIC_URL is not active ({w.get('server')})", f)
    _status, _h, raw = h.info()
    text = raw.decode()
    for needle in ("url", "Url", "URL", "publicUrl", "ws://", "wss://", "http", "hub.test.example",
                   "127.0.0.1", "://"):
        _fail_if(needle in text, f"the body contains {needle!r}: {text}", f)
    return f


async def t_info_other_methods_405(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    _fail_if(h.info()[0] != 200, "precondition: GET /hub/info is not 200", f)
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        body = b'{"a": 1}' if method in ("POST", "PUT", "PATCH", "DELETE") else None
        headers = {"Content-Type": "application/json"} if body else {}
        status, hd, raw = h.raw(method, "/hub/info", body, headers)
        _fail_if(status != 405, f"{method}: status {status}", f)
        if method != "HEAD":
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                parsed = raw
            _fail_if(parsed != {"error": "method not allowed"}, f"{method}: body {raw!r}", f)
            _fail_if(not hd.get("content-type", "").startswith("application/json"),
                     f"{method}: content-type {hd.get('content-type')!r}", f)
    # A POST with a body is drained, and the hub still answers afterwards.
    _fail_if(h.info()[0] != 200, "GET after the 405s did not answer 200", f)
    # A method with a body, bigger than a header read, on the same path.
    status, _hd, _raw = h.raw("POST", "/hub/info", b"x" * 70000,
                              {"Content-Type": "application/octet-stream"})
    _fail_if(status not in (405, 413), f"large POST: status {status}", f)
    _fail_if(h.info()[0] != 200, "GET after the large POST did not answer 200", f)
    return f


async def t_server_id_survives_restart(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    h1 = HubProc(nd.url)
    tmp = h1.tmp
    try:
        await h1.up()
        id1 = json.loads(h1.info()[2])["serverId"]
        first = open(h1.id_file, encoding="utf-8").read()
        _fail_if(first != id1 + "\n", f"side file holds {first!r}, not the id and a newline", f)
        h1.stop()
        h2 = HubProc(nd.url, tmp=tmp)
        try:
            await h2.up()
            id2 = json.loads(h2.info()[2])["serverId"]
            _fail_if(id2 != id1, f"id changed over a restart: {id1} -> {id2}", f)
            w = await h2.welcome(token=TOKEN)
            _fail_if((w.get("server") or {}).get("id") != id1,
                     f"welcome.server.id {w.get('server')} differs", f)
        finally:
            h2.stop()
        # The id is not in state.json: that is the file a rollback rewrites.
        if os.path.exists(h1.state):
            _fail_if(id1 in open(h1.state, encoding="utf-8").read(),
                     "the id is inside state.json", f)
        # Deleting the whole state makes a new server, which is true.
        os.unlink(h1.id_file)
        h3 = HubProc(nd.url, tmp=tmp)
        try:
            await h3.up()
            id3 = json.loads(h3.info()[2])["serverId"]
            _fail_if(id3 == id1 or not HEX32.fullmatch(id3),
                     f"a deleted identity file did not mint a new id ({id3})", f)
        finally:
            h3.stop()
    finally:
        h1.cleanup()
    return f


async def t_server_id_survives_project2_rewrite(nd: FakeNavidrome) -> list[str]:
    """The project 2 hub knows nothing of the id file and rewrites state.json (and
    its backup) as it always did. Doing exactly that between two starts keeps the
    id: it lives beside the state, not in it."""
    f: list[str] = []
    h1 = HubProc(nd.url)
    tmp = h1.tmp
    try:
        await h1.up()
        await h1.welcome(token=TOKEN)    # a registered device puts something in state
        id1 = json.loads(h1.info()[2])["serverId"]
        h1.stop()
        _fail_if(not os.path.exists(h1.state), "precondition: no state.json was written", f)
        with open(h1.state, encoding="utf-8") as fh:
            state = json.load(fh)
        for key in list(state):               # a state file with no id inside, as P2 writes
            _fail_if(key.lower().startswith("server"), f"state.json has a {key!r} key", f)
        for suffix in ("", ".bak"):
            if os.path.exists(h1.state + suffix) or suffix == "":
                tmpf = h1.state + suffix + ".p2tmp"
                with open(tmpf, "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
                os.replace(tmpf, h1.state + suffix)
        h2 = HubProc(nd.url, tmp=tmp)
        try:
            await h2.up()
            id2 = json.loads(h2.info()[2])["serverId"]
            _fail_if(id2 != id1, f"id changed over a rewrite of state.json: {id1} -> {id2}", f)
        finally:
            h2.stop()
        # And with state.json gone entirely (a P2 hub that lost its state): the same.
        for suffix in ("", ".bak"):
            if os.path.exists(h1.state + suffix):
                os.unlink(h1.state + suffix)
        h3 = HubProc(nd.url, tmp=tmp)
        try:
            await h3.up()
            _fail_if(json.loads(h3.info()[2])["serverId"] != id1,
                     "id changed with state.json gone", f)
        finally:
            h3.stop()
    finally:
        h1.cleanup()
    return f


async def t_bad_server_id_file_refuses_start(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    good = "0123456789abcdef0123456789abcdef"
    variants = {
        "empty": b"",
        "31 characters": good[:-1].encode() + b"\n",
        "33 characters": good.encode() + b"0\n",
        "upper case": good.upper().encode() + b"\n",
        "not hex": b"z" * 32 + b"\n",
        "two lines": good.encode() + b"\n" + good.encode() + b"\n",
        "padded": b" " + good.encode() + b"\n",
        "binary": b"\xff\xfe\x00" * 11,
    }
    for label, content in variants.items():
        tmp = tempfile.mkdtemp(prefix="hubinfo-bad-")
        try:
            state = os.path.join(tmp, "state.json")
            idf = state + ".server-id"
            with open(idf, "wb") as fh:
                fh.write(content)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("HUB_", "PREVIEW_", "AUDIOMUSE_", "LBBOT_", "NAVIDROME_"))}
            env.update(HUB_TOKEN=TOKEN, HUB_HOST="127.0.0.1", HUB_PORT=str(_free_port()),
                       HUB_HEALTH_PORT=str(_free_port()), HUB_STATE=state,
                       HUB_MIRROR_PLAYQUEUE="false", NAVIDROME_URL=nd.url,
                       HUB_ND_USER="owner", HUB_ND_PASS=PASSWORDS["owner"])
            try:
                proc = subprocess.run([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                                      capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                f.append(f"{label}: still running after 10 s, it started instead of refusing")
                continue
            out = proc.stdout + proc.stderr
            _fail_if(proc.returncode == 0, f"{label}: exit 0, must be non-zero", f)
            _fail_if(idf not in out, f"{label}: the message does not name {idf}: {out!r}", f)
            _fail_if(open(idf, "rb").read() != content, f"{label}: the file was replaced", f)
            written = sorted(set(os.listdir(tmp)) - {"state.json.server-id"})
            _fail_if(written, f"{label}: a refused start wrote {written}", f)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return f


async def t_server_id_never_replaces_a_racing_file(nd: FakeNavidrome) -> list[str]:
    """Two starts on one state that both found no file: the one that publishes second
    must serve the first one's id and leave the file alone (os.link, not os.replace)."""
    f: list[str] = []
    if HUB_DIR not in sys.path:
        sys.path.insert(0, HUB_DIR)
    import hub as hubmod  # noqa: PLC0415
    tmp = tempfile.mkdtemp(prefix="hubinfo-race-")
    try:
        state = os.path.join(tmp, "state.json")
        path = state + ".server-id"
        other = "fedcba9876543210fedcba9876543210"
        real_link = os.link

        def racing_link(src, dst, *a, **kw):
            with open(dst, "w", encoding="utf-8") as fh:  # the other start wins the race
                fh.write(other + "\n")
            return real_link(src, dst, *a, **kw)  # now raises FileExistsError

        hubmod.os.link = racing_link
        try:
            got = hubmod.load_or_mint_server_id(state)
        finally:
            hubmod.os.link = real_link
        _fail_if(got != other, f"served {got}, not the id already on disk", f)
        with open(path, encoding="utf-8") as fh:
            _fail_if(fh.read() != other + "\n", "the winner's file was replaced", f)
        _fail_if(sorted(os.listdir(tmp)) != ["state.json.server-id"],
                 f"stray files: {sorted(os.listdir(tmp))}", f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


async def t_info_never_touches_navidrome(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    await asyncio.sleep(0.5)          # whatever the hub does at start is over
    before = nd.requests
    log_before = h.log().count("hub/info from ")
    for _ in range(20):
        status, _h, _raw = h.info()
        if status != 200:
            f.append(f"info answered {status}")
            break
    await asyncio.sleep(0.3)
    _fail_if(nd.requests != before,
             f"Navidrome saw {nd.requests - before} request(s) during 20 info requests", f)
    lines = [ln for ln in h.log().splitlines() if "hub/info from " in ln]
    _fail_if(len(lines) - log_before != 20,
             f"{len(lines) - log_before} 'hub/info from' log lines for 20 requests", f)
    _fail_if(not lines or not lines[-1].rstrip().endswith("hub/info from 127.0.0.1"),
             f"log line is {lines[-1:]!r}", f)
    return f


# --------------------------------------------------------------------------- #
# H2
# --------------------------------------------------------------------------- #
async def t_welcome_carries_server_id_and_name(h: HubProc, nd: FakeNavidrome) -> list[str]:
    """`h` runs with HUB_NAME=" Home Hub " (trimmed on the way out)."""
    f: list[str] = []
    info = json.loads(h.info()[2])
    for label, w in (("legacy token", await h.welcome(token=TOKEN)),
                     ("device secret", await h.welcome(secret=h.signin("bob"),
                                                       device_id="dev-bob"))):
        server = w.get("server")
        _fail_if(not isinstance(server, dict), f"{label}: no server object ({server!r})", f)
        if isinstance(server, dict):
            _fail_if(server.get("id") != info["serverId"],
                     f"{label}: server.id {server.get('id')!r} != info {info['serverId']!r}", f)
            _fail_if(server.get("name") != "Home Hub",
                     f"{label}: server.name {server.get('name')!r}", f)
    _fail_if(info["name"] != "Home Hub", f"info.name {info['name']!r}", f)
    return f


async def t_welcome_public_url(h: HubProc, nd: FakeNavidrome) -> list[str]:
    """`h` runs with HUB_PUBLIC_URL=https://Hub.Test.Example:8443/worbus/connect/ : https
    stored as wss, a trailing / and /connect dropped, the path kept."""
    f: list[str] = []
    w = await h.welcome(token=TOKEN)
    server = w.get("server") or {}
    _fail_if(server.get("publicUrl") != "wss://hub.test.example:8443/worbus",
             f"publicUrl {server.get('publicUrl')!r}", f)
    _fail_if(set(server) != {"id", "name", "publicUrl"}, f"server keys {sorted(server)}", f)
    return f


async def t_welcome_no_public_url_key_when_unset(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    w = await h.welcome(token=TOKEN)
    server = w.get("server")
    _fail_if(not isinstance(server, dict), f"no server object ({server!r})", f)
    if isinstance(server, dict):
        _fail_if("publicUrl" in server, f"publicUrl present: {server}", f)
        _fail_if(set(server) != {"id", "name"}, f"server keys {sorted(server)}", f)
    return f


async def t_server_field_same_for_every_user(h: HubProc, nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    servers = {}
    servers["bob"] = (await h.welcome(secret=h.signin("bob"), device_id="dev-bob")).get("server")
    servers["alice"] = (await h.welcome(secret=h.signin("alice"),
                                        device_id="dev-alice")).get("server")
    servers["owner (legacy token)"] = (await h.welcome(token=TOKEN)).get("server")
    _fail_if(any(s is None for s in servers.values()), f"a welcome without server: {servers}", f)
    _fail_if(len({json.dumps(s, sort_keys=True) for s in servers.values()}) != 1,
             f"the server field differs per user: {servers}", f)
    return f


# --------------------------------------------------------------------------- #
async def _with_hub(nd: FakeNavidrome, fn, **env) -> list[str]:
    h = HubProc(nd.url, **env)
    try:
        await h.up()
        return await fn(h, nd)
    except Exception:  # noqa: BLE001
        return [f"raised:\n{traceback.format_exc()}"]
    finally:
        h.cleanup()


async def _bare(nd: FakeNavidrome, fn) -> list[str]:
    try:
        return await fn(nd)
    except Exception:  # noqa: BLE001
        return [f"raised:\n{traceback.format_exc()}"]


async def main_async() -> tuple[list[str], list[str]]:
    nd = FakeNavidrome(USERS).start()
    failures: list[str] = []
    names: list[str] = []

    def record(name: str, got: list[str]) -> None:
        names.append(name)
        failures.extend(f"{name}: {x}" for x in got)

    try:
        plain = HubProc(nd.url)
        try:
            await plain.up()
            for name, fn in (("info_shape_without_auth", t_info_shape_without_auth),
                             ("info_same_bytes_with_bearer", t_info_same_bytes_with_bearer),
                             ("info_other_methods_405", t_info_other_methods_405),
                             ("info_never_touches_navidrome", t_info_never_touches_navidrome),
                             ("welcome_no_public_url_key_when_unset",
                              t_welcome_no_public_url_key_when_unset),
                             ("server_field_same_for_every_user",
                              t_server_field_same_for_every_user)):
                try:
                    got = await fn(plain, nd)
                except Exception:  # noqa: BLE001
                    got = [f"raised:\n{traceback.format_exc()}"]
                record(name, got)
        finally:
            plain.cleanup()

        record("info_has_no_address", await _with_hub(
            nd, t_info_has_no_address, HUB_PUBLIC_URL="https://hub.test.example"))
        record("welcome_carries_server_id_and_name", await _with_hub(
            nd, t_welcome_carries_server_id_and_name, HUB_NAME=" Home Hub "))
        record("welcome_public_url_when_set/https_public_url_becomes_wss/"
               "trailing_connect_and_slash_dropped/path_kept", await _with_hub(
                   nd, t_welcome_public_url,
                   HUB_PUBLIC_URL="https://Hub.Test.Example:8443/worbus/connect/"))
        record("server_id_survives_restart", await _bare(nd, t_server_id_survives_restart))
        record("server_id_survives_project2_rewrite",
               await _bare(nd, t_server_id_survives_project2_rewrite))
        record("bad_server_id_file_refuses_start",
               await _bare(nd, t_bad_server_id_file_refuses_start))
        record("server_id_never_replaces_a_racing_file",
               await _bare(nd, t_server_id_never_replaces_a_racing_file))
    finally:
        nd.stop()
    return failures, names


def main() -> int:
    failures, names = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - hub info: " + "/".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
