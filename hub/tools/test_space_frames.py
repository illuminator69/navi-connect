#!/usr/bin/env python3
"""
Worbus P2, H3: a user's frames and acts stay in their space.

Real hub subprocesses with the fake Navidrome (tools/fake_navidrome.py), a stub
AudioMuse that records what reaches it, and LBBOT_URL pointing at nothing (so
lb-bot starts out unavailable and an authenticated /lb/notify moves it to up).
Users: owner (admin, HUB_ND_USER), alice (admin), bob (not admin).

  test_session_frames_stay_in_space     owner and bob, two devices each: no frame
                                        of one names anything of the other's
  test_transfer_cannot_target_other_space
  test_library_index_lb_frames_reach_everyone
  test_fill_frames_reach_admins_only    (R8: a legacy socket is the primary user's)
  test_mirror_runs_for_primary_only     (the fake records savePlayQueue)
  test_fingerprint_and_your_mix_primary_only
  test_cast_bridge_lands_in_bridger_space

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
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_navidrome import FakeNavidrome  # noqa: E402
from test_signin import TOKEN, USERS, HubProc, Sock  # noqa: E402


class StubAudioMuse:
    """Answers every route 200; records the paths it was asked."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # noqa: D102
                pass

            def _ok(self, body: bytes = b"[]") -> None:
                stub.paths.append(self.path.partition("?")[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._ok(b'{"ready": true}' if "clap" in self.path else b"[]")

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                self._ok()

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _code(p) -> object:
    return p.get("code") if isinstance(p, dict) else None


def _text(sock: Sock) -> str:
    return "\n".join(json.dumps(f) for f in sock.frames)


async def _connect(h: HubProc, user: str, name: str, **dev) -> tuple[Sock, dict]:
    st, p = await h.signin(user)
    if st != 200:
        raise RuntimeError(f"{user}'s sign-in answered {st} {p}")
    s = await Sock().hello(h.ws_url, {"id": p["deviceId"], "name": name, **dev},
                           secret=p["deviceSecret"])
    return s, p


async def _act(s: Sock, **kw) -> None:
    await s.ws.send(json.dumps({"t": "act", **kw}))


async def t_session_frames(h: HubProc) -> list[str]:
    f = []
    o1, _ = await _connect(h, "owner", "OWNERDESK")
    o2, _ = await _connect(h, "owner", "OWNERPHONE")
    b1, bp = await _connect(h, "bob", "BOBDESK")
    b2, _ = await _connect(h, "bob", "BOBPHONE")
    await _act(o1, action="setQueue", tracks=[{"id": "OWNTRACK1", "title": "OwnTitle"},
                                              {"id": "OWNTRACK2", "title": "OwnTitle2"}],
               index=0, positionMs=0, play=True)
    await _act(b1, action="setQueue", tracks=[{"id": "BOBTRACK1", "title": "BobTitle"}],
               index=0, positionMs=0, play=True)
    await asyncio.sleep(0.3)
    await o2.ws.send(json.dumps({"t": "report", "index": 0, "positionMs": 5000,
                                 "isPlaying": True}))
    await b2.ws.send(json.dumps({"t": "report", "index": 0, "positionMs": 7000,
                                 "isPlaying": True}))
    await _act(o1, action="saveMix", name="OWNMIX", kind="genre", seedId="g1")
    await _act(b1, action="saveMix", name="BOBMIX", kind="genre", seedId="g2")
    await asyncio.sleep(0.8)
    for s, mine, theirs in ((o1, "OWN", "BOB"), (o2, "OWN", "BOB"),
                            (b1, "BOB", "OWN"), (b2, "BOB", "OWN")):
        text = _text(s)
        if theirs in text:
            kinds = sorted({fr.get("t") for fr in s.frames if theirs in json.dumps(fr)})
            f.append(f"a {mine} socket heard the other user's {kinds}")
        if f"{mine}TRACK1" not in text:
            f.append(f"a {mine} socket never heard its own session")
    if not any(fr.get("t") == "mixes" and any(m.get("name") == "OWNMIX" for m in fr["mixes"])
               for fr in o2.frames):
        f.append("owner's other device did not hear owner's new mix")
    for s in (o1, o2, b1, b2):
        await s.close()
    return f


async def t_transfer(h: HubProc) -> list[str]:
    f = []
    o, _ = await _connect(h, "owner", "Owner T")
    b, bp = await _connect(h, "bob", "Bob T")
    await _act(o, action="setQueue", tracks=[{"id": "T1", "title": "x"}], index=0,
               positionMs=0, play=True)
    await asyncio.sleep(0.3)
    await _act(o, action="transfer", target=bp["deviceId"], cid="c-x")
    await asyncio.sleep(1.0)
    if any(fr.get("t") == "do" for fr in b.frames):
        f.append(f"bob's device was sent {[fr.get('cmd') for fr in b.frames if fr.get('t') == 'do']}")
    ack = next((fr for fr in o.frames if fr.get("t") == "ack" and fr.get("cid") == "c-x"), None)
    err = [fr for fr in o.frames if fr.get("t") == "error"]
    if not ((ack and ack.get("ok") is False) or err):
        f.append(f"a transfer to another user's device was not refused: ack {ack}, errors {err}")
    await o.close()
    await b.close()
    return f


async def t_library_frames(h: HubProc) -> list[str]:
    f = []
    o = await Sock().hello(h.ws_url, {"id": "legacy-lib", "name": "Legacy"}, token=TOKEN)
    b, _ = await _connect(h, "bob", "Bob L")
    a, _ = await _connect(h, "alice", "Alice L")
    await asyncio.to_thread(h.http, "POST", "/lb/notify",
                            {"albums": [{"artist": "A", "album": "B", "ndAlbumIds": ["al1"]}]},
                            TOKEN)
    await asyncio.to_thread(h.http, "POST", "/lb/index", {"seq": 3, "epoch": "e1"}, TOKEN)
    await asyncio.sleep(0.6)
    for who, s in (("legacy owner", o), ("bob", b), ("alice", a)):
        kinds = {fr.get("t") for fr in s.frames}
        for t in ("library", "index", "lb"):
            if t not in kinds:
                f.append(f"{who} did not hear `{t}`")
    for s in (o, b, a):
        await s.close()
    return f


async def t_fill_frames(h: HubProc) -> list[str]:
    f = []
    o = await Sock().hello(h.ws_url, {"id": "legacy-fill", "name": "Legacy"}, token=TOKEN)
    b, _ = await _connect(h, "bob", "Bob F")
    a, _ = await _connect(h, "alice", "Alice F")
    st, p = await asyncio.to_thread(h.http, "POST", "/lb/fill",
                                    {"kind": "album", "key": "rg-1", "state": "searching",
                                     "artist": "A", "album": "B"}, TOKEN)
    if st != 200:
        f.append(f"the fill push answered {st} {p}")
    await asyncio.sleep(0.6)
    if not any(fr.get("t") == "fill" for fr in o.frames):
        f.append("the legacy (primary, admin) socket did not hear the fill")
    if not any(fr.get("t") == "fill" for fr in a.frames):
        f.append("alice (admin) did not hear the fill")
    if any(fr.get("t") == "fill" for fr in b.frames):
        f.append("bob (not an admin) heard a fill")
    for s in (o, b, a):
        await s.close()
    return f


async def t_cast_bridge(h: HubProc) -> list[str]:
    f = []
    b, bp = await _connect(h, "bob", "Bob C")
    cast = await Sock().hello(h.ws_url, {"id": "cast-den", "name": "Den speaker",
                                         "bridgedBy": bp["deviceId"], "caps": ["receiver"]},
                              secret=bp["deviceSecret"])
    o, _ = await _connect(h, "owner", "Owner C")
    await asyncio.sleep(0.4)
    if "cast-den" in _text(o):
        f.append("bob's bridged speaker shows in the owner's frames")
    if not any(fr.get("t") in ("devices", "welcome") and any(
            d.get("id") == "cast-den" for d in fr.get("devices", [])) for fr in b.frames):
        f.append("bob's bridged speaker is not in bob's device list")
    await _act(o, action="setQueue", tracks=[{"id": "C1", "title": "x"}], index=0,
               positionMs=0, play=True)
    await asyncio.sleep(0.2)
    await _act(o, action="transfer", target="cast-den", cid="c-c")
    await asyncio.sleep(0.8)
    if any(fr.get("t") == "do" for fr in cast.frames):
        f.append("the owner's transfer reached bob's speaker")
    for s in (b, cast, o):
        await s.close()
    return f


async def t_mirror(nd: FakeNavidrome) -> list[str]:
    f = []
    h = HubProc(nd.url, HUB_MIRROR_PLAYQUEUE="true")
    try:
        await h.up()
        o, _ = await _connect(h, "owner", "Owner M")
        b, _ = await _connect(h, "bob", "Bob M")
        await _act(o, action="setQueue", tracks=[{"id": "MIRROR_OWN", "title": "x"}],
                   index=0, positionMs=0, play=False)
        await _act(b, action="setQueue", tracks=[{"id": "MIRROR_BOB", "title": "y"}],
                   index=0, positionMs=0, play=False)
        await asyncio.sleep(3.0)
        ids = [i for q in nd.play_queues for i in q.get("ids", [])]
        users = {q.get("u") for q in nd.play_queues}
        if "MIRROR_OWN" not in ids:
            f.append(f"the owner's queue was not mirrored ({nd.play_queues})")
        if "MIRROR_BOB" in ids:
            f.append("bob's queue was written into the service account's play queue")
        if users - {"owner"}:
            f.append(f"the mirror wrote as {users}")
        await o.close()
        await b.close()
    finally:
        h.stop()
    return f


async def t_fingerprint(nd: FakeNavidrome) -> list[str]:
    f = []
    am = StubAudioMuse()
    h = HubProc(nd.url, AUDIOMUSE_URL=am.url, AUDIOMUSE_TOKEN="am-tok")
    try:
        await h.up()
        st, bob = await h.signin("bob")
        st, alice = await h.signin("alice")
        st, owner = await h.signin("owner")
        for who, sec in (("bob", bob["deviceSecret"]), ("alice", alice["deviceSecret"])):
            before = len(am.paths)
            st, p = await asyncio.to_thread(h.http, "GET", "/sonic/fingerprint?n=10", None, sec)
            if (st, _code(p)) != (403, "primary_only"):
                f.append(f"{who}'s /sonic/fingerprint answered {st} {p}")
            if any("fingerprint" in x for x in am.paths[before:]):
                f.append(f"{who}'s fingerprint call reached AudioMuse")
            st, p = await asyncio.to_thread(h.http, "POST", "/sonic/station/start",
                                            {"seed": {"kind": "yourMix"}}, sec)
            if (st, _code(p)) != (403, "primary_only"):
                f.append(f"{who}'s Your mix station answered {st} {p}")
        for n, (who, sec) in enumerate((("owner", owner["deviceSecret"]), ("legacy", TOKEN))):
            before = len(am.paths)
            # A different `n` each: the route is cached, and a cached answer would
            # not show whether this call was let through.
            st, p = await asyncio.to_thread(h.http, "GET", f"/sonic/fingerprint?n={20 + n}",
                                            None, sec)
            if st != 200 or not any("fingerprint" in x for x in am.paths[before:]):
                f.append(f"the {who} fingerprint did not pass through ({st} {p})")
            st, p = await asyncio.to_thread(h.http, "POST", "/sonic/station/start",
                                            {"seed": {"kind": "yourMix"}}, sec)
            if _code(p) == "primary_only":
                f.append(f"the {who} Your mix station was refused as not primary")
        # A library-wide route stays open to everyone.
        st, p = await asyncio.to_thread(h.http, "GET", "/sonic/clap/stats", None,
                                        bob["deviceSecret"])
        if st != 200:
            f.append(f"bob's /sonic/clap/stats answered {st}")
    finally:
        h.stop()
        am.stop()
    return f


async def main_async() -> list[str]:
    nd = FakeNavidrome(USERS).start()
    failures: list[str] = []
    h = HubProc(nd.url, LBBOT_URL="http://127.0.0.1:9")
    try:
        await h.up()
        for name, fn in (("test_session_frames_stay_in_space", t_session_frames),
                         ("test_transfer_cannot_target_other_space", t_transfer),
                         ("test_library_index_lb_frames_reach_everyone", t_library_frames),
                         ("test_fill_frames_reach_admins_only", t_fill_frames),
                         ("test_cast_bridge_lands_in_bridger_space", t_cast_bridge)):
            try:
                got = await fn(h)
            except Exception:  # noqa: BLE001
                got = [f"raised:\n{traceback.format_exc()}"]
            failures += [f"{name}: {x}" for x in got]
    finally:
        h.stop()
    for name, fn in (("test_mirror_runs_for_primary_only", t_mirror),
                     ("test_fingerprint_and_your_mix_primary_only", t_fingerprint)):
        try:
            got = await fn(nd)
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        failures += [f"{name}: {x}" for x in got]
    nd.stop()
    return failures


def main() -> int:
    failures = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - space frames: session-frames-stay-in-space/transfer-not-across-spaces/"
          "library-index-lb-reach-everyone/fill-admins-only/mirror-primary-only/"
          "fingerprint-and-your-mix-primary-only/cast-bridge-in-bridger-space")
    return 0


if __name__ == "__main__":
    sys.exit(main())
