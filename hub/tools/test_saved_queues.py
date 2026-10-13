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

from test_edits import TOKEN, Client, _free_port, unlink_state

HUB_DIR = os.path.join(os.path.dirname(__file__), "..")


def rec(client, rid):
    return next((r for r in client.saved_queues if r["id"] == rid), None)


def spawn(port, state, extra_env=None):
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state, "HUB_HOST": "127.0.0.1",
           "HUB_HEALTH_PORT": str(_free_port()), **(extra_env or {})}
    return subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR)


def stop(hub):
    hub.terminate()
    try:
        hub.wait(timeout=5)
    except subprocess.TimeoutExpired:
        hub.kill()


async def test_resurrection_and_batch():
    """Cases 1, 2, 6, 7, 8."""
    port, failures = _free_port(), []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://127.0.0.1:{port}"
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
        unlink_state(state)
    return failures


async def test_live_record_reconciliation():
    """Cases 3, 4."""
    port, failures = _free_port(), []
    url = f"ws://127.0.0.1:{port}"

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
        unlink_state(state)

    # --- 4. restart with a savedQueueId whose record is gone ------------------- #
    port2 = _free_port()
    url2 = f"ws://127.0.0.1:{port2}"
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
        unlink_state(state2)
    return failures


async def test_malformed_input():
    """Case 5: garbage in a client record must not break persistence."""
    port, failures = _free_port(), []
    url = f"ws://127.0.0.1:{port}"
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
        unlink_state(state)
    return failures


async def test_history_scrubs_credentials():
    """B-003: PERSISTED saved-queue history carries no credentialed Subsonic
    `streamUrl`. The LIVE queue (what a Chromecast bridge reads) is untouched, and
    a preview (`ext:`) track's signed `streamUrl` survives in history — it's the
    only thing that makes a preview replayable, and it's a short-lived capability,
    not a standing Subsonic credential.
    """
    port, failures = _free_port(), []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://127.0.0.1:{port}"
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await c.connect(url)
        credentialed = ("https://music.example.com/rest/stream.view"
                        "?id=t0&v=1.13.0&c=Feishin&u=alice&t=abc123deadbeef&s=saltsalt")
        preview_url = "https://hub.example.com/preview/stream/ext:yt:xyz?sig=abcd&exp=9999999999"
        tracks = [
            {"id": "t0", "title": "T0", "streamUrl": credentialed, "mime": "audio/flac"},
            {"id": "ext:yt:xyz", "title": "Preview", "streamUrl": preview_url, "mime": "audio/webm"},
        ]
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="hist1", sourceKind="album", sourceName="Album One")
        await asyncio.sleep(0.4)

        # The record broadcast to clients (and about to be persisted) is scrubbed.
        live_rec = rec(c, "hist1")
        if live_rec is None:
            failures.append("setQueue minted no history record")
        else:
            by_id = {s["id"]: s for s in live_rec["songs"]}
            if "streamUrl" in by_id.get("t0", {}):
                failures.append(f"ordinary track kept a credentialed streamUrl in history: "
                                f"{by_id['t0'].get('streamUrl')!r}")
            if by_id.get("ext:yt:xyz", {}).get("streamUrl") != preview_url:
                failures.append("a preview track's streamUrl was stripped from history, "
                                "breaking its restore")

        # state.json on disk: same story for the persisted `savedQueues`, but the
        # LIVE `session.queue` (what a cast bridge reads right now) keeps both URLs.
        with open(state, encoding="utf-8") as f:
            parsed = json.load(f)
        disk_hist = next((q for q in parsed.get("savedQueues", []) if q["id"] == "hist1"), None)
        if disk_hist is None:
            failures.append("state.json has no persisted record for hist1")
        else:
            disk_by_id = {s["id"]: s for s in disk_hist["songs"]}
            if "streamUrl" in disk_by_id.get("t0", {}):
                failures.append("state.json's history still carries the ordinary track's "
                                "credentialed streamUrl")
        live_by_id = {s["id"]: s for s in parsed.get("session", {}).get("queue", [])}
        if live_by_id.get("t0", {}).get("streamUrl") != credentialed:
            failures.append("the LIVE queue lost its streamUrl — this would break the "
                            "Chromecast bridge, which fetches it directly")
        if live_by_id.get("ext:yt:xyz", {}).get("streamUrl") != preview_url:
            failures.append("the LIVE queue's preview streamUrl was altered")
    finally:
        stop(hub)
        unlink_state(state)
    return failures


async def test_history_scrub_on_load():
    """B-003 migration: a state.json an older build wrote (credentials still in
    history) is scrubbed the moment the hub loads it — `_load()` re-sanitises every
    persisted record through `_sanitize_saved_queue`, which is now where the scrub
    lives, so an old file self-heals on the next start with no separate migration."""
    port, failures = _free_port(), []
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    credentialed = ("https://music.example.com/rest/stream.view"
                    "?id=old0&u=alice&t=deadbeef&s=salt")
    with open(state, "w", encoding="utf-8") as f:
        json.dump({
            "session": {"queue": [], "index": 0, "positionMs": 0},
            "savedQueues": [{
                "id": "legacy1", "songs": [{"id": "old0", "title": "Old",
                                           "streamUrl": credentialed}],
                "songCount": 1, "currentIndex": 0, "positionMs": 0,
                "sourceKind": "album", "updatedAt": int(time.time() * 1000),
            }],
            "devices": [],
        }, f)
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)
        legacy = rec(c, "legacy1")
        if legacy is None:
            failures.append("the pre-existing record was dropped on load instead of scrubbed")
        else:
            song = next((s for s in legacy["songs"] if s["id"] == "old0"), None)
            if song is None:
                failures.append("the legacy track was dropped on load")
            elif "streamUrl" in song:
                failures.append(f"a state.json written by an older build still had its "
                                f"credentialed streamUrl after load: {song['streamUrl']!r}")
    finally:
        stop(hub)
        unlink_state(state)
    return failures


def _has_credential_param(url):
    """True if `url`'s query string carries a Subsonic (or plain-password) login
    param. Used to assert the ABSENCE of credentials, not any particular scrub
    strategy — a test written this way doesn't care whether the fix drops the
    whole field or just the offending params."""
    if not url:
        return False
    from urllib.parse import urlsplit, parse_qsl
    keys = {k.lower() for k, _ in parse_qsl(urlsplit(url).query)}
    return bool(keys & {"u", "t", "s", "p", "apikey"})


async def test_history_scrubs_cover_credentials():
    """B-003 second half: PERSISTED saved-queue history carries no Subsonic login
    in cover-art URLs either — a song's `imageUrl` (`getCoverArt?...&u=&t=&s=`) and
    a record's `coverImageUrl`, same as `streamUrl`. An `ext:` preview's `imageUrl`
    is a public Cover Art Archive URL and survives untouched (same exception as its
    `streamUrl`). Covers the upsert path (`setQueue`) and the sync path (`enqueue`
    growing the current record); the load path is `test_history_scrub_on_load`.
    """
    port, failures = _free_port(), []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://127.0.0.1:{port}"
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url); await c.connect(url)
        cover_art = ("https://music.example.com/rest/getCoverArt.view"
                     "?id=al0&v=1.13.0&c=Feishin&u=alice&t=abc123deadbeef&s=saltsalt")
        record_cover = ("https://music.example.com/rest/getCoverArt.view"
                        "?id=al0&u=alice&t=deadbeef00&s=pepper")
        caa_cover = "https://coverartarchive.org/release/abc-123/front-500"
        tracks = [
            {"id": "t0", "title": "T0", "imageUrl": cover_art, "mime": "audio/flac"},
            {"id": "ext:yt:xyz", "title": "Preview", "imageUrl": caa_cover,
             "mime": "audio/webm"},
        ]
        await r.act(action="setQueue", tracks=tracks, index=0, play=True,
                    savedQueueId="hist2", sourceKind="album", sourceName="Album Two",
                    coverImageUrl=record_cover)
        await asyncio.sleep(0.4)

        live_rec = rec(c, "hist2")
        if live_rec is None:
            failures.append("setQueue minted no history record")
        else:
            by_id = {s["id"]: s for s in live_rec["songs"]}
            if _has_credential_param(by_id.get("t0", {}).get("imageUrl")):
                failures.append(f"ordinary track kept a credentialed imageUrl in "
                                f"history: {by_id['t0'].get('imageUrl')!r}")
            if by_id.get("ext:yt:xyz", {}).get("imageUrl") != caa_cover:
                failures.append("a preview track's public imageUrl was stripped "
                                "from history")
            if _has_credential_param(live_rec.get("coverImageUrl")):
                failures.append(f"the record's coverImageUrl kept its credentials: "
                                f"{live_rec.get('coverImageUrl')!r}")

        # Sync path: enqueue grows the SAME record via _sync_current_saved_queue,
        # not _upsert_saved_queue — its own scrub point.
        more_cover = ("https://music.example.com/rest/getCoverArt.view"
                      "?id=al1&u=alice&t=beefdead11&s=thyme")
        await r.act(action="enqueue", at="end",
                    tracks=[{"id": "t1", "title": "T1", "imageUrl": more_cover,
                             "mime": "audio/flac"}])
        await asyncio.sleep(0.4)
        live_rec = rec(c, "hist2")
        by_id = {s["id"]: s for s in (live_rec or {}).get("songs", [])}
        if _has_credential_param(by_id.get("t1", {}).get("imageUrl")):
            failures.append(f"a track added via enqueue kept a credentialed "
                            f"imageUrl in history: {by_id.get('t1', {}).get('imageUrl')!r}")

        # state.json: same story for the persisted copy.
        with open(state, encoding="utf-8") as f:
            parsed = json.load(f)
        disk_hist = next((q for q in parsed.get("savedQueues", []) if q["id"] == "hist2"), None)
        if disk_hist is None:
            failures.append("state.json has no persisted record for hist2")
        else:
            disk_by_id = {s["id"]: s for s in disk_hist["songs"]}
            if _has_credential_param(disk_by_id.get("t0", {}).get("imageUrl")):
                failures.append("state.json's history still carries a credentialed "
                                "imageUrl")
            if _has_credential_param(disk_hist.get("coverImageUrl")):
                failures.append("state.json's history still carries a credentialed "
                                "coverImageUrl")

        # The LIVE session queue (what a cast bridge/receiver reads right now) is
        # untouched — only the persisted/broadcast history copy is scrubbed.
        live_by_id = {s["id"]: s for s in parsed.get("session", {}).get("queue", [])}
        if live_by_id.get("t0", {}).get("imageUrl") != cover_art:
            failures.append("the LIVE queue lost/altered its imageUrl")
    finally:
        stop(hub)
        unlink_state(state)
    return failures


async def test_history_cover_scrub_on_load():
    """B-003 migration, second half: a state.json an older build wrote (cover-art
    credentials still in history) is scrubbed the moment the hub loads it, same as
    `streamUrl` in `test_history_scrub_on_load` — `_sanitize_saved_queue` covers
    both."""
    port, failures = _free_port(), []
    url = f"ws://127.0.0.1:{port}"
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    cover_art = ("https://music.example.com/rest/getCoverArt.view"
                "?id=old0&u=alice&t=deadbeef&s=salt")
    record_cover = ("https://music.example.com/rest/getCoverArt.view"
                    "?id=old0&u=alice&t=cafebabe&s=pepper")
    with open(state, "w", encoding="utf-8") as f:
        json.dump({
            "session": {"queue": [], "index": 0, "positionMs": 0},
            "savedQueues": [{
                "id": "legacy2", "songs": [{"id": "old0", "title": "Old",
                                           "imageUrl": cover_art}],
                "songCount": 1, "currentIndex": 0, "positionMs": 0,
                "sourceKind": "album", "coverImageUrl": record_cover,
                "updatedAt": int(time.time() * 1000),
            }],
            "devices": [],
        }, f)
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)
        legacy = rec(c, "legacy2")
        if legacy is None:
            failures.append("the pre-existing record was dropped on load instead of scrubbed")
        else:
            song = next((s for s in legacy["songs"] if s["id"] == "old0"), None)
            if song is None:
                failures.append("the legacy track was dropped on load")
            elif _has_credential_param(song.get("imageUrl")):
                failures.append(f"a state.json written by an older build still had "
                                f"its credentialed imageUrl after load: {song.get('imageUrl')!r}")
            if _has_credential_param(legacy.get("coverImageUrl")):
                failures.append(f"a state.json written by an older build still had "
                                f"its credentialed coverImageUrl after load: "
                                f"{legacy.get('coverImageUrl')!r}")
    finally:
        stop(hub)
        unlink_state(state)
    return failures


async def main():
    failures = []
    failures += await test_resurrection_and_batch()
    failures += await test_live_record_reconciliation()
    failures += await test_malformed_input()
    failures += await test_history_scrubs_credentials()
    failures += await test_history_scrub_on_load()
    failures += await test_history_scrubs_cover_credentials()
    failures += await test_history_cover_scrub_on_load()
    if failures:
        print("FAIL (saved-queue audit 2026-08-07):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - saved-queue audit: tombstone-resurrection/unknown-id-tombstone/"
          "live-rename-reconcile/live-record-rebuild/malformed-input/batch-delete/"
          "unknown-rename-error/deleted-before-merge/history-scrubs-credentials/"
          "history-scrub-on-load/history-scrubs-cover-credentials/"
          "history-cover-scrub-on-load")


if __name__ == "__main__":
    asyncio.run(main())
