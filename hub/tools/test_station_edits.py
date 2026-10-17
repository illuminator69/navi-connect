#!/usr/bin/env python3
"""
Tests for the hub editing the bound station's queue itself (PROTOCOL §18.2,
`stationEdits`): a `character` or `stop` signal with `apply: true`.

Real WebSocket clients and a real hub subprocess, against the fake AudioMuse and
Navidrome of test_station_engine.

  1. a signal without `apply` edits nothing (the old behaviour, exactly);
  2. a controller's `character` + apply, more than 20 s left: every station row
     from the next song on is replaced, the user's own row stays, the playing
     device gets `queueChanged` and never `load`, the saved-queue record follows;
  3. under 20 s left the next song is kept;
  4. `stop` + apply removes the station rows from the cut and inserts nothing;
  5. `character` after a stop, on a paused session, appends a batch;
  6. under shuffle the cut and the insertion follow PLAY order;
  7. two applies at once do not double-insert;
  8. a transfer in flight: `edited: false, reason: "transfer"`; after it, the new
     playing device is the one sent `queueChanged`;
  9. inserted rows: getSong's shape with Tier 1 (albumId, durationMs, coverArtId,
     the queue's serverId), the AudioMuse row's without it;
 10. `apply` on a station the session is not bound to answers as before.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_edits import Client, _free_port  # noqa: E402
from test_station_engine import (LIB, ND, AudioMuse, Navidrome, acall, serve,  # noqa: E402
                                 spawn, stop, unlink_all, wait_up)

DUR = 200_000
FULL_KEYS = {"id", "title", "artist", "album", "albumId", "durationMs", "coverArtId",
             "serverId"}


class EClient(Client):
    """test_edits.Client, keeping the session, the saved queues and every `do`."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.session: dict = {}
        self.features: list = []
        self.station_frames: list = []

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("t")
                if t == "welcome":
                    self.session = msg.get("session", {})
                    self.features = msg.get("features", [])
                    self.saved_queues = msg.get("savedQueues", [])
                elif t == "session":
                    self.session = msg
                elif t == "savedQueues":
                    self.saved_queues = msg.get("queues", [])
                elif t == "station":
                    self.station_frames.append(msg)
                elif t == "do":
                    self.dos.append(msg)
        except websockets.ConnectionClosed:
            pass

    def ids(self):
        return [t.get("id") for t in self.session.get("queue", [])]

    def cmds(self):
        return [d.get("cmd") for d in self.dos]


async def settle(t=0.4):
    await asyncio.sleep(t)


def row(tid, server="nd-main"):
    t = LIB[tid]
    return {"id": tid, "title": t["title"], "artist": t["artist"], "album": t["album"],
            "durationMs": DUR, "serverId": server}


async def signal(port, sid, kind, value=None, apply=None):
    body = {"stationId": sid, "kind": kind}
    if value is not None:
        body["value"] = value
    if apply is not None:
        body["apply"] = apply
    return (await acall(port, "POST", "/sonic/station/signal", body))[1]


async def main_suite(failures):
    am_srv, am_url = serve(AudioMuse)
    nd_srv, nd_url = serve(Navidrome)
    port = _free_port()
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, am_url, nd_url)
    try:
        await asyncio.to_thread(wait_up, port)
        r = EClient("Recv", device_id="recv")
        c = EClient("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url)
        await c.connect(url)
        if "stationEdits" not in c.features:
            failures.append(f"welcome.features lacks stationEdits: {c.features}")

        seed = "L14_0"
        st_, a = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": [seed]}, "character": "balanced", "count": 8})
        sid = a["stationId"]
        served = [t["id"] for t in a["tracks"]]
        user = next(i for i in LIB if i.startswith("L") and i not in served and i != seed)
        queue = [row(seed)] + [row(i) for i in served[:3]] + [row(user)] + \
            [row(i) for i in served[3:]]
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_edit", stationId=sid)
        await settle()
        await r.report(index=0, positionMs=10_000, isPlaying=True)
        await settle()

        # ----- 1. no apply: nothing is edited -------------------------------- #
        before = c.ids()
        r.dos.clear()
        ans = await signal(port, sid, "character", "close")
        await settle()
        if "edited" in ans or c.ids() != before or "queueChanged" in r.cmds():
            failures.append(f"a signal without apply edited the queue: {ans}")

        # ----- 2. character + apply, > 20 s left ---------------------------- #
        r.dos.clear()
        ans = await signal(port, sid, "character", "explore", apply=True)
        await settle()
        ed = ans.get("edited") or {}
        now = c.ids()
        if ed.get("removed") != 8 or ed.get("added") != 8 or ed.get("from") != 1:
            failures.append(f"character + apply: {ans}")
        if now[0] != seed or c.session.get("index") != 0:
            failures.append(f"the playing track moved: {now[:2]} index "
                            f"{c.session.get('index')}")
        if set(now[1:9]) & set(served) or user not in now:
            failures.append(f"the station rows were not all replaced, or the user's row "
                            f"went: {now}")
        if "queueChanged" not in r.cmds() or "load" in r.cmds():
            failures.append(f"the playing device should get queueChanged, never load: "
                            f"{r.cmds()}")
        qc = next((d for d in r.dos if d.get("cmd") == "queueChanged"), {})
        if [t.get("id") for t in qc.get("tracks", [])] != now or qc.get("index") != 0:
            failures.append("queueChanged did not carry the edited queue and index")
        new_rows = c.session["queue"][1:9]
        if any(set(t) != FULL_KEYS for t in new_rows):
            failures.append(f"inserted rows are not getSong-shaped: "
                            f"{sorted(set(new_rows[0]))}")
        elif any(t["durationMs"] != 200_000 or t["serverId"] != "nd-main"
                 or not t["albumId"].startswith("al-") for t in new_rows):
            failures.append(f"inserted rows carry the wrong values: {new_rows[0]}")
        rec = next((q for q in c.saved_queues if q.get("id") == "q_edit"), {})
        if rec.get("songCount") != len(now):
            failures.append(f"the saved-queue record did not follow the edit: "
                            f"{rec.get('songCount')} != {len(now)}")
        frames = [f for f in c.station_frames if f.get("stationId") == sid]
        if not frames or frames[-1].get("seq") != 16:
            failures.append(f"the station frame did not follow the batch: "
                            f"{frames[-1:] if frames else None}")

        # ----- 3. under 20 s left the next song is kept -------------------- #
        await r.report(index=0, positionMs=185_000, isPlaying=True)
        await settle()
        keep_next = c.ids()[1]
        ans = await signal(port, sid, "character", "balanced", apply=True)
        await settle()
        ed = ans.get("edited") or {}
        if c.ids()[1] != keep_next or ed.get("from") != 2 or ed.get("removed") != 7:
            failures.append(f"under 20 s left, the next song must stay: {ans} "
                            f"{c.ids()[:3]}")

        # ----- 4. stop + apply removes the station rows from the cut ------- #
        await r.report(index=0, positionMs=10_000, isPlaying=True)
        await settle()
        ans = await signal(port, sid, "stop", apply=True)
        await settle()
        ed = ans.get("edited") or {}
        if c.ids() != [seed, user] or ed.get("added") != 0 or not ed.get("removed"):
            failures.append(f"stop + apply: {ans} {c.ids()}")

        # ----- 5. character after the stop, on a paused session ------------ #
        await r.report(index=0, positionMs=10_000, isPlaying=False)
        await settle()
        ans = await signal(port, sid, "character", "close", apply=True)
        await settle()
        ed = ans.get("edited") or {}
        if ed.get("removed") != 0 or ed.get("added") != 5 or c.ids()[:2] != [seed, user] \
                or len(c.ids()) != 7:
            failures.append(f"character after a stop on a paused session: {ans} {c.ids()}")
        await r.report(index=0, positionMs=10_000, isPlaying=True)
        await settle()

        # ----- 6. shuffle: play order ---------------------------------------- #
        await c.act(action="shuffle", on=True)
        await settle()
        order0 = list(c.session.get("order") or [])
        q0 = c.ids()
        ans = await signal(port, sid, "character", "explore", apply=True)
        await settle()
        ed = ans.get("edited") or {}
        q1, order1 = c.ids(), c.session.get("order") or []
        inserted = [i for i in q1 if i not in q0]
        if not order0 or order0[0] != 0 or not order1 or order1[0] != 0:
            failures.append(f"shuffle: the playing track must stay first in play order: "
                            f"{order0} {order1}")
        elif [q1[i] for i in order1[1:1 + len(inserted)]] != inserted \
                or ed.get("from") != 1 or len(inserted) != ed.get("added"):
            failures.append(f"shuffle: the batch must follow the cut in play order: "
                            f"{ans} order {order1} queue {q1}")
        if user not in q1 or sorted(order1) != list(range(len(q1))):
            failures.append(f"shuffle: the order is not a permutation of the queue, or the "
                            f"user's row went: {order1}")
        await c.act(action="shuffle", on=False)
        await settle()

        # ----- 7. two applies at once do not double-insert ------------------ #
        base = [i for i in c.ids() if i in (seed, user)]
        res = await asyncio.gather(signal(port, sid, "character", "close", apply=True),
                                   signal(port, sid, "character", "balanced", apply=True))
        await settle()
        q = c.ids()
        rows_now = [i for i in q if i not in base]
        if len(q) != len(set(q)):
            failures.append(f"two applies duplicated a track: {q}")
        if len(rows_now) not in [(x.get("edited") or {}).get("added") for x in res]:
            failures.append(f"two applies at once left {len(rows_now)} station rows: "
                            f"{[x.get('edited') for x in res]}")

        # ...and when they overlap (a slow getSong) while the user adds a song as
        # "play next" in the middle: the second plans against the first's result,
        # and each plans again once its batch is ready, so the user's song stays
        # and no earlier station row survives.
        old_rows = set(rows_now)
        ND["slow_song"] = 0.6
        two = asyncio.gather(signal(port, sid, "character", "explore", apply=True),
                             signal(port, sid, "character", "close", apply=True))
        await asyncio.sleep(0.3)
        mine = row(next(i for i in LIB if i.startswith("L") and i not in c.ids()
                        and i not in served))
        await c.act(action="enqueue", tracks=[mine], at="next")
        res = await two
        ND["slow_song"] = 0.0
        await settle()
        q = c.ids()
        if mine["id"] not in q or len(q) != len(set(q)) or old_rows & set(q):
            failures.append(f"overlapping applies with an enqueue: {q} "
                            f"{[x.get('edited') for x in res]}")

        # ----- 8. a transfer in flight, and after it ------------------------ #
        r2 = EClient("Recv2", device_id="recv2")
        await r2.connect(url)
        await settle()
        await c.act(action="transfer", target="recv2")   # recv never answers release
        await asyncio.sleep(0.2)
        ans = await signal(port, sid, "character", "explore", apply=True)
        if ans.get("edited") is not False or ans.get("reason") != "transfer" \
                or not ans.get("applied"):
            failures.append(f"an apply during a transfer: {ans}")
        await asyncio.sleep(2.5)                          # RELEASE_TIMEOUT, then the load
        if c.session.get("activeDeviceId") != "recv2":
            failures.append(f"the transfer did not complete: {c.session.get('activeDeviceId')}")
        r.dos.clear()
        r2.dos.clear()
        ans = await signal(port, sid, "character", "close", apply=True)
        await settle()
        if not ans.get("edited") or "queueChanged" not in r2.cmds() \
                or "load" in r2.cmds() or r.dos:
            failures.append(f"after the transfer the new device must get queueChanged: "
                            f"{ans} recv2 {r2.cmds()} recv {r.cmds()}")

        # ----- 10. a station the session is not bound to -------------------- #
        st_, other = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L15_0"]}, "count": 3})
        before = c.ids()
        ans = await signal(port, other["stationId"], "character", "explore", apply=True)
        await settle()
        if "edited" in ans or c.ids() != before:
            failures.append(f"apply on an unbound station edited the session: {ans}")
        for cl in (r, r2, c):
            await cl.ws.close()
    finally:
        stop(proc)
        am_srv.shutdown()
        nd_srv.shutdown()
        unlink_all(state)


async def no_tier1_suite(failures):
    """9b: without Navidrome the inserted rows are AudioMuse's."""
    am_srv, am_url = serve(AudioMuse)
    port = _free_port()
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    proc = spawn(port, state, am_url, None)
    try:
        await asyncio.to_thread(wait_up, port)
        r = EClient("Recv", device_id="recv")
        await r.connect(url)
        st_, a = await acall(port, "POST", "/sonic/station/start", {
            "seed": {"kind": "track", "ids": ["L16_0"]}, "count": 5})
        sid = a["stationId"]
        queue = [row("L16_0", server=None)] + [row(t["id"], server=None)
                                              for t in a["tracks"]]
        for t in queue:
            t.pop("serverId")
        await r.act(action="setQueue", tracks=queue, index=0, play=True,
                    savedQueueId="q_rows", stationId=sid)
        await settle()
        await r.report(index=0, positionMs=10_000, isPlaying=True)
        await settle()
        ans = await signal(port, sid, "character", "close", apply=True)
        await settle()
        new_rows = r.session.get("queue", [])[1:]
        if not (ans.get("edited") or {}).get("added") or not new_rows \
                or any(set(t) != {"id", "title", "artist", "album"} for t in new_rows):
            failures.append(f"without Tier 1 the rows must be AudioMuse's: {ans} "
                            f"{sorted(set(new_rows[0])) if new_rows else None}")
        await r.ws.close()
    finally:
        stop(proc)
        am_srv.shutdown()
        unlink_all(state)


async def main():
    failures: list[str] = []
    await main_suite(failures)
    await no_tier1_suite(failures)
    if failures:
        print("FAIL (station edits):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - station edits: no-apply-edits-nothing/character-apply-replaces-from-the-"
          "next-song/user-rows-stay/queueChanged-never-load/saved-queue-follows/under-20s-"
          "next-kept/stop-apply-removes/character-after-stop-paused-appends/shuffle-play-"
          "order/concurrent-applies-no-double/transfer-in-flight-not-edited/after-transfer-"
          "new-device-edited/unbound-station-as-before/getSong-shape/rows-without-tier1")


if __name__ == "__main__":
    asyncio.run(main())
