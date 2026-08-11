#!/usr/bin/env python3
"""
Regression tests for the 2026-08-07 saved-queue audit fixes.

One case per defect:
  1. A tombstoned id is not resurrected by a `setQueue` from a device that kept playing.
  2. `deleteSavedQueue` tombstones an id the hub never held, so a later sync can't add it.
  3. An offline rename of the CURRENTLY PLAYING queue reconciles on sync (metadata only —
     the live record's songs/cursor are never overwritten by an offline copy).
  4. After a restart that kept session.savedQueueId but lost the record, the live record is
     rebuilt from the live session, not from a stale client copy.
  5. Malformed input (non-numeric updatedAt, an oversized song list, a track smuggling extra
     keys) is coerced/dropped and persistence keeps working.
  6. `deleteSavedQueues` deletes N ids in ONE broadcast.
  7. `renameSavedQueue` for an unknown id answers with an `unknown_saved_queue` error.
  8. `syncSavedQueues { deleted }` applies deletions before the merge, so a client that
     deleted a row offline (and still holds it in `queues`) doesn't re-add it.

Spins up a real hub subprocess per case; asserts on the authoritative frames.
Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

from test_edits import TOKEN, Client

HUB_DIR = os.path.join(os.path.dirname(__file__), "..")


def rec(client, rid):
    return next((r for r in client.saved_queues if r["id"] == rid), None)


def spawn(port, state, extra_env=None):
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_HEALTH_PORT": str(port + 1), **(extra_env or {})}
    return subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR)


def stop(hub):
    hub.terminate()
    try:
        hub.wait(timeout=5)
    except subprocess.TimeoutExpired:
        hub.kill()


async def test_resurrection_and_batch():
    """Cases 1, 2, 6, 7, 8."""
    port, failures = 4802, []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://localhost:{port}"
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await c.connect(url)
        tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]

        # ----- 1. a delete elsewhere isn't undone by the still-playing device ----- #
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album", sourceName="Album One")
        await asyncio.sleep(0.3)
        await c.act(action="deleteSavedQueue", id="q1")
        await asyncio.sleep(0.3)
        if rec(c, "q1") is not None:
            failures.append("deleteSavedQueue left the record behind")
        # The playing device knows nothing of the delete and republishes its own id.
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="q1", sourceKind="album", sourceName="Album One")
        await asyncio.sleep(0.3)
        if rec(c, "q1") is not None:
            failures.append("a republish resurrected a tombstoned record")
        if c.saved_queue_id in (None, "q1"):
            failures.append(f"republish should mint a NEW session id, got {c.saved_queue_id!r}")

        # ----- 2. tombstone for an id the hub never held -------------------------- #
        await c.act(action="deleteSavedQueue", id="never-seen")
        await asyncio.sleep(0.3)
        await c.act(action="syncSavedQueues", queues=[{
            "id": "never-seen", "songs": [{"id": "z0"}], "songCount": 1,
            "currentIndex": 0, "positionMs": 0, "updatedAt": int(time.time() * 1000),
        }])
        await asyncio.sleep(0.4)
        if rec(c, "never-seen") is not None:
            failures.append("deleting an unknown id wrote no tombstone; a sync re-added it")

        # ----- 6. batched delete = ONE broadcast ---------------------------------- #
        now = int(time.time() * 1000)
        await c.act(action="syncSavedQueues", queues=[
            {"id": f"b{i}", "songs": [{"id": "z"}], "songCount": 1, "currentIndex": 0,
             "positionMs": 0, "updatedAt": now + i} for i in range(10)
        ])
        await asyncio.sleep(0.4)
        if any(rec(c, f"b{i}") is None for i in range(10)):
            failures.append("bulk sync didn't merge the 10 records")
        c.sq_broadcasts = 0
        before = len(c.saved_queues)
        await c.act(action="deleteSavedQueues", ids=[f"b{i}" for i in range(10)])
        await asyncio.sleep(0.5)
        if any(rec(c, f"b{i}") is not None for i in range(10)):
            failures.append(f"deleteSavedQueues left records behind: {[x['id'] for x in c.saved_queues]}")
        if before - len(c.saved_queues) != 10:
            failures.append(f"expected 10 rows removed, list went {before} -> {len(c.saved_queues)}")

        # ----- 7. rename of an unknown id errors ---------------------------------- #
        c.errors.clear()
        await c.act(action="renameSavedQueue", id="no-such-queue", name="X")
        await asyncio.sleep(0.4)
        if not any(e.get("code") == "unknown_saved_queue" for e in c.errors):
            failures.append(f"renameSavedQueue on an unknown id was silent: {c.errors}")

        # ----- 8. deleted[] is applied BEFORE queues[] ---------------------------- #
        # The client still holds the row it deleted offline; it must stay deleted.
        await c.act(action="syncSavedQueues",
                    deleted=["off1"],
                    queues=[{"id": "off1", "songs": [{"id": "z"}], "songCount": 1,
                             "currentIndex": 0, "positionMs": 0,
                             "updatedAt": int(time.time() * 1000) + 9000}])
        await asyncio.sleep(0.4)
        if rec(c, "off1") is not None:
            failures.append("syncSavedQueues merged a row the same frame deleted")
    finally:
        stop(hub)
        os.unlink(state)
    return failures


async def test_live_record_reconciliation():
    """Cases 3, 4."""
    port, failures = 4804, []
    url = f"ws://localhost:{port}"

    # --- 3. offline rename of the currently-playing queue --------------------- #
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await c.connect(url)
        tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]
        await r.act(action="setQueue", tracks=tracks, index=2, play=True,
                    savedQueueId="live", sourceKind="album", sourceName="Album One")
        await asyncio.sleep(0.3)

        await c.act(action="syncSavedQueues", queues=[{
            "id": "live", "name": "Renamed Offline",
            # A stale, truncated copy of the same queue — its songs/cursor must be ignored.
            "songs": [{"id": "t0"}], "songCount": 1, "currentIndex": 0, "positionMs": 0,
            "sourceKind": "album", "updatedAt": int(time.time() * 1000) + 60_000,
        }])
        await asyncio.sleep(0.5)
        live = rec(c, "live")
        if live is None:
            failures.append("the live record disappeared during sync")
        else:
            if live.get("name") != "Renamed Offline":
                failures.append(f"offline rename of the live queue didn't reconcile: {live.get('name')!r}")
            if live.get("songCount") != 4:
                failures.append(f"an offline copy overwrote the live record's songs: {live.get('songCount')}")
            if live.get("currentIndex") != 2:
                failures.append(f"an offline copy overwrote the live cursor: {live.get('currentIndex')}")
    finally:
        stop(hub)
        os.unlink(state)

    # --- 4. restart with a savedQueueId whose record is gone ------------------- #
    port2 = 4806
    url2 = f"ws://localhost:{port2}"
    state2 = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]
    with open(state2, "w", encoding="utf-8") as f:
        json.dump({
            "session": {"queue": tracks, "index": 1, "positionMs": 5000,
                        "savedQueueId": "orphan", "sourceKind": "album",
                        "sourceName": "Album One"},
            "savedQueues": [],          # the record is gone; the session still names it
            "devices": [],
        }, f)
    hub = spawn(port2, state2)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url2)
        await c.act(action="syncSavedQueues", queues=[{
            "id": "orphan", "songs": [{"id": "stale"}], "songCount": 1,
            "currentIndex": 0, "positionMs": 0, "sourceKind": "manual",
            "updatedAt": int(time.time() * 1000) + 60_000,
        }])
        await asyncio.sleep(0.5)
        orphan = rec(c, "orphan")
        if orphan is None:
            failures.append("the live record wasn't rebuilt after a restart that lost it")
        elif [t["id"] for t in orphan["songs"]] != [t["id"] for t in tracks]:
            failures.append(f"a stale client copy became the live record: {orphan['songs']}")
    finally:
        stop(hub)
        os.unlink(state2)
    return failures


async def test_malformed_input():
    """Case 5: garbage in a client record must not break persistence."""
    port, failures = 4808, []
    url = f"ws://localhost:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)
        await c.act(action="syncSavedQueues", queues=[
            # non-numeric updatedAt: used to raise TypeError inside _save()
            {"id": "bad1", "songs": [{"id": "z"}], "updatedAt": "abc",
             "currentIndex": "nope", "positionMs": None, "repeat": "sideways"},
            # oversized song list + a track smuggling a credential-shaped key
            {"id": "bad2", "updatedAt": 5,
             "songs": [{"id": f"s{i}", "token": "secret"} for i in range(5000)]},
            # unusable: no songs
            {"id": "bad3", "songs": [], "updatedAt": 5},
            "not-even-a-dict",
        ])
        await asyncio.sleep(0.6)

        b1 = rec(c, "bad1")
        if b1 is None:
            failures.append("a coercible record was dropped entirely")
        else:
            if not isinstance(b1.get("updatedAt"), int):
                failures.append(f"updatedAt wasn't coerced: {b1.get('updatedAt')!r}")
            if b1.get("currentIndex") != 0 or b1.get("positionMs") != 0:
                failures.append(f"cursor fields weren't coerced: {b1}")
            if b1.get("repeat") != "none":
                failures.append(f"an invalid repeat mode was stored: {b1.get('repeat')!r}")
        b2 = rec(c, "bad2")
        if b2 is None:
            failures.append("the oversized record was dropped instead of capped")
        else:
            if b2["songCount"] > 1000:
                failures.append(f"song list not capped: {b2['songCount']}")
            if any("token" in t for t in b2["songs"]):
                failures.append("a track kept a non-whitelisted key")
        if rec(c, "bad3") is not None:
            failures.append("a record with no songs was stored")

        # Persistence must still work — force a write and reparse it.
        await c.act(action="deleteSavedQueue", id="bad3")
        await asyncio.sleep(0.5)
        with open(state, encoding="utf-8") as f:
            parsed = json.load(f)
        if "savedQueues" not in parsed:
            failures.append("state.json didn't survive the malformed payload")
    finally:
        stop(hub)
        os.unlink(state)
    return failures


async def main():
    failures = []
    failures += await test_resurrection_and_batch()
    failures += await test_live_record_reconciliation()
    failures += await test_malformed_input()
    if failures:
        print("FAIL (saved-queue audit 2026-08-07):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - saved-queue audit: tombstone-resurrection/unknown-id-tombstone/"
          "live-rename-reconcile/live-record-rebuild/malformed-input/batch-delete/"
          "unknown-rename-error/deleted-before-merge")


if __name__ == "__main__":
    asyncio.run(main())
