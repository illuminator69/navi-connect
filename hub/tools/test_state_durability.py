#!/usr/bin/env python3
"""
Tests for state-file durability: the fsynced backup and the defensive load.

state.json is rewritten whole, unsynced, on every session broadcast. Nearly all
of it is recoverable — clients re-push their saved queues, the session and the
device registry rebuild themselves — but two collections exist nowhere else:
the "Mixed for You" recipes and the saved-queue tombstones. Before this, a
state.json the hub could not parse was logged and then overwritten by the next
save, and one unreadable section aborted every section after it.

In process (the cost side — a backup must not put an fsync on the hot path):
  1. 50 bookkeeping saves (position, index, a new device) cause 0 fsyncs and no
     backup file.
  2. A new mix causes at least one fsync and the backup then holds it; so does a
     tombstone; bookkeeping saves after that are free again.
  3. A burst of changes is written by one worker at a time, latest payload last.
  4. A directory fsync that is refused (a FUSE share) does not fail the backup.

Against a real hub subprocess (the recovery side):
  5. A session of `setQueue` + reports alone leaves no backup file.
  6. state.json truncated to zero bytes, backup present -> the mix is back in
     `welcome`, state.json is rewritten at once, and nothing is set aside.
  7. Garbage in state.json -> a `.corrupt-<UTC>` file holds the exact original
     bytes and the mix is restored from the backup.
  8. Garbage and NO backup -> a fresh start, and the bad bytes are still on disk
     after a later act (they used to be overwritten by it).
  9. `"mixes": null` beside a tombstone, and a device row with no `id` ahead of
     a good one -> the tombstone still refuses a `syncSavedQueues` of that id and
     the good device row still loads.
 10. A good state.json wins over a stale backup, and the backup is brought in
     line with it.

In process again (what boot does to the files):
 11. A state.json that already holds a tombstone, and no backup — the first boot
     on this build — gets its fsynced backup at boot.
 12. A backup whose mixes and tombstones already match is not rewritten.
 13. state.json missing, backup present -> restored, and state.json put back.
 14. Both unparseable -> both set aside with their bytes, and a fresh start.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import errno
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

from test_edits import TOKEN, Client, _free_port

HUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GARBAGE = b'\x00\xff{"session": {"rev": 7, "queue": [tru'   # not UTF-8, not JSON


def spawn(port, state):
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state,
           "HUB_HOST": "127.0.0.1", "HUB_HEALTH_PORT": str(_free_port())}
    return subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR)


async def stop(hub):
    """SIGTERM, then wait OFF the loop: the hub's graceful stop closes every socket
    and waits for the close handshake, which the clients here can only answer while
    this loop is running. A blocking wait starves them and ends in the 5 s kill."""
    hub.terminate()
    try:
        await asyncio.to_thread(hub.wait, 5)
    except subprocess.TimeoutExpired:
        hub.kill()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def mix_names(path):
    """Names of the mixes in a state file, or None if it is absent/unreadable."""
    try:
        return sorted(m["name"] for m in read_json(path).get("mixes") or [])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def set_asides(state):
    return sorted(glob.glob(state + ".corrupt-*"))


async def until(cond, timeout=3.0):
    """Poll `cond` instead of sleeping a fixed time; returns its final truth."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return bool(cond())


# --------------------------------------------------------------------------- #
# In process: what a save costs
# --------------------------------------------------------------------------- #
async def test_fsync_only_for_the_irreplaceable():
    """Cases 1-4."""
    import hub as hubmod
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="hub-durability-")
    state = os.path.join(tmpdir, "state.json")
    bak = state + ".bak"
    saved_path, real_fsync = hubmod.STATE_PATH, os.fsync
    real_writer = getattr(hubmod, "_write_state_backup_blocking", None)
    fsyncs = {"n": 0, "refuse_dirs": False}
    writer = {"calls": 0, "active": 0, "max_active": 0}
    lock = threading.Lock()

    def counting_fsync(fd):
        fsyncs["n"] += 1
        if fsyncs["refuse_dirs"] and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "fsync on a directory refused")
        return real_fsync(fd)

    def tracking_writer(path, payload):
        with lock:
            writer["calls"] += 1
            writer["active"] += 1
            writer["max_active"] = max(writer["max_active"], writer["active"])
        try:
            time.sleep(0.03)  # widen the window two writers would overlap in
            return real_writer(path, payload)
        finally:
            with lock:
                writer["active"] -= 1

    async def drained(h):
        return await until(lambda: getattr(h, "_backup_task", None) is None
                           and not getattr(h, "_backup_pending", False))

    hubmod.STATE_PATH = state
    os.fsync = counting_fsync  # hub.py calls os.fsync, i.e. this attribute
    if real_writer is not None:
        hubmod._write_state_backup_blocking = tracking_writer
    try:
        h = hubmod.Hub()
        now = int(time.time() * 1000)

        # ----- 1. bookkeeping saves are free -------------------------------- #
        h.session.queue = [{"id": f"t{i}", "title": f"T{i}"} for i in range(5)]
        for i in range(50):
            h.session.index = i % 5
            h.session.position_ms = i * 1000
            h.session.is_playing = bool(i % 2)
            h.session.bump()
            h.devices[f"dev{i}"] = hubmod.Device(id=f"dev{i}", name=f"Dev {i}",
                                                 last_seen=now)
            h._save()
        await asyncio.sleep(0.15)  # a wrongly queued worker would have run by now
        await drained(h)
        if fsyncs["n"] != 0:
            failures.append(f"50 bookkeeping saves cost {fsyncs['n']} fsync(s) — "
                            "that is an fsync on the session-broadcast path")
        if os.path.exists(bak):
            failures.append("bookkeeping saves created a backup file with nothing "
                            "irreplaceable in it")
        try:
            main_ok = read_json(state)["session"]["positionMs"] == 49000 \
                and len(read_json(state)["devices"]) == 50
        except (OSError, ValueError, KeyError):
            main_ok = False
        if not main_ok:
            failures.append("state.json does not hold the last bookkeeping save")

        # ----- 2. a mix is fsynced into the backup; so is a tombstone ------- #
        h._save_mix({"name": "Late Night", "kind": "genre", "seedId": "g1"})
        h._save()
        await drained(h)
        if fsyncs["n"] < 1:
            failures.append("adding a mix caused no fsync — the only copy of it "
                            "sits in an unsynced file")
        if mix_names(bak) != ["Late Night"]:
            failures.append(f"the backup does not hold the new mix: {mix_names(bak)}")
        after_mix = fsyncs["n"]
        h._delete_saved_queue("q_dead")
        h._save()
        await drained(h)
        try:
            tombstoned = "q_dead" in read_json(bak).get("deletedSavedQueues", {})
        except (OSError, ValueError):
            tombstoned = False
        if not tombstoned or fsyncs["n"] <= after_mix:
            failures.append("a new tombstone did not reach the backup with an fsync")
        after_tombstone = fsyncs["n"]
        for i in range(10):
            h.session.position_ms = 60000 + i
            h.session.bump()
            h._save()
        await asyncio.sleep(0.1)
        await drained(h)
        if fsyncs["n"] != after_tombstone:
            failures.append("bookkeeping saves after a backup fsynced again — the "
                            "signature must cover the mixes and tombstones only")

        # ----- 3. single flight, latest wins -------------------------------- #
        writer.update(calls=0, max_active=0)
        for i in range(20):
            h._save_mix({"name": f"Burst {i}", "kind": "genre", "seedId": f"b{i}"})
            h._save()
            await asyncio.sleep(0)  # let the worker start; its write outlasts the loop
        await drained(h)
        if writer["max_active"] > 1:
            failures.append(f"{writer['max_active']} backup writers ran at once — "
                            "two writers on one temp path can land the older "
                            "payload last")
        if not 1 <= writer["calls"] <= 10:
            failures.append(f"a burst of 20 changes made {writer['calls']} backup "
                            "writes — a waiting payload should be replaced, not queued")
        if "Burst 19" not in (mix_names(bak) or []):
            failures.append("the backup does not end on the newest payload of a burst")

        # ----- 4. a refused directory fsync is not a failed backup ---------- #
        fsyncs["refuse_dirs"] = True
        h._save_mix({"name": "On A Share", "kind": "genre", "seedId": "g2"})
        h._save()
        await drained(h)
        if "On A Share" not in (mix_names(bak) or []):
            failures.append("a refused directory fsync lost the backup write")
        if getattr(h, "_backup_sig", None) is None:
            failures.append("a refused directory fsync was treated as a failed "
                            "backup (it would be retried on every save)")
    finally:
        os.fsync = real_fsync
        hubmod.STATE_PATH = saved_path
        if real_writer is not None:
            hubmod._write_state_backup_blocking = real_writer
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


def test_boot_brings_the_backup_in_line():
    """Cases 11-14: what `_load` does to the files, with no hub process needed."""
    import hub as hubmod
    failures = []
    saved_path, real_fsync = hubmod.STATE_PATH, os.fsync
    fsyncs = {"n": 0}

    def counting_fsync(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    mix = {"id": "mx_a", "name": "Kept", "kind": "genre", "seedId": "g", "count": 50,
           "createdAt": 1000, "updatedAt": 2000, "lastPlayedAt": 0}

    def doc(**over):
        return {"session": {"rev": 1, "queue": [], "index": 0}, "savedQueues": [],
                "deletedSavedQueues": {}, "mixes": [], "lbIndex": None,
                "devices": [], **over}

    def boot(main=None, bak=None):
        """Lay the two files out (dict = JSON, bytes = verbatim, None = absent),
        construct a Hub on them, return (hub, dir, fsyncs spent)."""
        tmpdir = tempfile.mkdtemp(prefix="hub-durability-")
        hubmod.STATE_PATH = os.path.join(tmpdir, "state.json")
        for path, content in ((hubmod.STATE_PATH, main),
                              (hubmod.STATE_PATH + ".bak", bak)):
            if content is not None:
                with open(path, "wb") as f:
                    f.write(content if isinstance(content, bytes)
                            else json.dumps(content).encode())
        fsyncs["n"] = 0
        return hubmod.Hub(), tmpdir, fsyncs

    os.fsync = counting_fsync
    dirs = []
    try:
        # ----- 11. first boot on this build: the backup appears at once ----- #
        h, d, spent = boot(main=doc(deletedSavedQueues={"q_gone": 5}))
        dirs.append(d)
        try:
            held = "q_gone" in read_json(hubmod.STATE_PATH + ".bak")["deletedSavedQueues"]
        except (OSError, ValueError, KeyError):
            held = False
        if not held or spent["n"] < 1:
            failures.append("a state.json that already holds a tombstone got no "
                            "fsynced backup at boot — it would stay unprotected "
                            "until the next mix or deletion")

        # ----- 12. a backup that already matches is left alone -------------- #
        stale_session = doc(mixes=[mix], session={"rev": 99, "queue": [], "index": 0})
        h, d, spent = boot(main=doc(mixes=[mix]), bak=stale_session)
        dirs.append(d)
        if spent["n"] != 0 or read_json(hubmod.STATE_PATH + ".bak") != stale_session:
            failures.append("a backup whose mixes and tombstones already match was "
                            "rewritten at boot — every restart would cost an fsync")

        # ----- 13. state.json missing, backup present ----------------------- #
        h, d, spent = boot(bak=doc(mixes=[mix]))
        dirs.append(d)
        if [m["name"] for m in h.mixes.values()] != ["Kept"]:
            failures.append("a missing state.json did not restore from the backup "
                            "(a crash between the set-aside and the first save "
                            "leaves exactly this on disk)")
        if mix_names(hubmod.STATE_PATH) != ["Kept"]:
            failures.append("state.json was not put back after restoring a missing one")

        # ----- 14. both unparseable: both kept, fresh start ----------------- #
        h, d, spent = boot(main=b"{not json", bak=b"{nor this")
        dirs.append(d)
        kept = {os.path.basename(p).split(".corrupt-")[0]: open(p, "rb").read()
                for p in glob.glob(os.path.join(d, "*.corrupt-*"))}
        if kept != {"state.json": b"{not json", "state.json.bak": b"{nor this"}:
            failures.append(f"two unparseable files were not both set aside: {kept}")
        if h.mixes or h.deleted_saved_queues or os.path.exists(hubmod.STATE_PATH + ".bak"):
            failures.append("two unparseable files did not give a clean fresh start")
    finally:
        os.fsync = real_fsync
        hubmod.STATE_PATH = saved_path
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    return failures


# --------------------------------------------------------------------------- #
# Subprocess: what a restart recovers
# --------------------------------------------------------------------------- #
async def test_backup_and_recovery():
    """Cases 5-8, on one state directory carried across four hub runs."""
    port, failures = _free_port(), []
    tmpdir = tempfile.mkdtemp(prefix="hub-durability-")
    state = os.path.join(tmpdir, "state.json")
    bak = state + ".bak"
    url = f"ws://127.0.0.1:{port}"
    open(state, "wb").close()  # zero bytes, as every other suite starts
    try:
        # ----- 5. no backup until there is something irreplaceable ---------- #
        hub = spawn(port, state)
        try:
            await asyncio.sleep(1.2)
            r = Client("Recv", device_id="recv")
            c = Client("Ctl", device_id="ctl", caps=["controller"])
            await r.connect(url)
            await c.connect(url)
            tracks = [{"id": f"t{i}", "title": f"T{i}"} for i in range(4)]
            await r.act(action="setQueue", tracks=tracks, index=0, play=True)
            for pos in (1000, 2000, 3000):
                await r.report(positionMs=pos, index=0, isPlaying=True)
                await asyncio.sleep(0.1)
            await c.act(action="pause")
            await asyncio.sleep(0.4)
            if os.path.exists(bak):
                failures.append("a session of setQueue + reports wrote a backup file")
            if set_asides(state):
                failures.append("a zero-byte state file was set aside as corrupt: "
                                f"{set_asides(state)}")
            await c.act(action="saveMix", name="Keeper", kind="artist",
                        seedId="artist-1", seedName="Band", count=60)
            if not await until(lambda: mix_names(bak) == ["Keeper"] and c.mixes):
                failures.append(f"the mix never reached the backup: {mix_names(bak)}")
            # One more change to the irreplaceable part, then stop almost at once:
            # a graceful stop must not leave the backup a change behind.
            if c.mixes:
                await c.act(action="touchMix", id=c.mixes[0]["id"])
                await asyncio.sleep(0.05)
        finally:
            await stop(hub)
        try:
            in_step = read_json(bak)["mixes"] == read_json(state)["mixes"] \
                and read_json(state)["mixes"][0].get("lastPlayedAt", 0) > 0
        except (OSError, ValueError, KeyError, IndexError):
            in_step = False
        if not in_step:
            failures.append("after a graceful stop the backup's mixes are not "
                            "state.json's (or the last act was lost)")

        # ----- 6. zero-byte state.json, backup present ---------------------- #
        open(state, "wb").close()
        hub = spawn(port, state)
        try:
            await asyncio.sleep(1.2)
            # Before any client connects: a registration saves too, and the claim
            # is that the restore itself puts state.json back.
            if mix_names(state) != ["Keeper"]:
                failures.append("state.json was not rewritten at once after a "
                                f"restore from the backup: {mix_names(state)}")
            c = Client("Ctl", device_id="ctl", caps=["controller"])
            await c.connect(url)
            if [m.get("name") for m in c.mixes] != ["Keeper"]:
                failures.append("a truncated state.json lost the mix although a "
                                f"backup held it: welcome.mixes={c.mixes}")
            elif c.mixes[0].get("seedId") != "artist-1" or c.mixes[0].get("count") != 60:
                failures.append(f"the restored mix lost fields: {c.mixes[0]}")
            if set_asides(state):
                failures.append("an EMPTY state file was set aside — only an "
                                f"unparseable one should be: {set_asides(state)}")
        finally:
            await stop(hub)

        # ----- 7. garbage state.json, backup present ------------------------ #
        with open(state, "wb") as f:
            f.write(GARBAGE)
        hub = spawn(port, state)
        try:
            await asyncio.sleep(1.2)
            c = Client("Ctl", device_id="ctl", caps=["controller"])
            await c.connect(url)
            aside = set_asides(state)
            if len(aside) != 1:
                failures.append(f"expected one .corrupt-* file, found {aside}")
            elif open(aside[0], "rb").read() != GARBAGE:
                failures.append("the set-aside file does not hold the original bytes")
            if [m.get("name") for m in c.mixes] != ["Keeper"]:
                failures.append("an unparseable state.json lost the mix although a "
                                f"backup held it: welcome.mixes={c.mixes}")
        finally:
            await stop(hub)

        # ----- 8. garbage state.json, no backup ----------------------------- #
        for path in set_asides(state) + [bak]:
            if os.path.exists(path):
                os.unlink(path)
        other_garbage = GARBAGE + b" and no backup beside it"
        with open(state, "wb") as f:
            f.write(other_garbage)
        hub = spawn(port, state)
        try:
            await asyncio.sleep(1.2)
            c = Client("Ctl", device_id="ctl", caps=["controller"])
            await c.connect(url)
            if c.mixes:
                failures.append(f"no backup, yet mixes appeared: {c.mixes}")
            tracks = [{"id": "x0", "title": "X0"}]
            await c.act(action="setQueue", tracks=tracks, index=0, play=False)
            await asyncio.sleep(0.4)
            try:
                saved = read_json(state)["session"]["queue"] == tracks
            except (OSError, ValueError, KeyError):
                saved = False
            if not saved:
                failures.append("the hub did not start fresh and save after an "
                                "unparseable state.json")
            aside = set_asides(state)
            if not any(open(p, "rb").read() == other_garbage for p in aside):
                failures.append("the unparseable state.json was overwritten by the "
                                f"next save instead of being set aside: {aside}")
        finally:
            await stop(hub)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


async def test_bad_sections_do_not_abort_the_rest():
    """Case 9."""
    port, failures = _free_port(), []
    tmpdir = tempfile.mkdtemp(prefix="hub-durability-")
    state = os.path.join(tmpdir, "state.json")
    url = f"ws://127.0.0.1:{port}"
    now = int(time.time() * 1000)
    with open(state, "w", encoding="utf-8") as f:
        json.dump({
            "session": {"rev": 3, "queue": [], "index": 0},
            "savedQueues": [],
            "mixes": None,                              # applied BEFORE the tombstones
            "deletedSavedQueues": {"q_dead": now - 5000},
            "devices": [{"name": "a row with no id"},
                        {"id": "kitchen", "name": "Kitchen", "lastSeen": now}],
        }, f)
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)
        if "kitchen" not in c.online:
            failures.append("a device row with no id cost the rows after it: "
                            f"{sorted(c.online)}")
        record = {"songs": [{"id": "z0"}], "songCount": 1, "currentIndex": 0,
                  "positionMs": 0, "updatedAt": now}
        await c.act(action="syncSavedQueues",
                    queues=[{**record, "id": "q_dead"}, {**record, "id": "q_live"}])
        await asyncio.sleep(0.5)
        ids = {r["id"] for r in c.saved_queues}
        if "q_live" not in ids:
            failures.append(f"setup: the control record was not merged: {ids} — "
                            "without it a refused q_dead proves nothing")
        if "q_dead" in ids:
            failures.append('`"mixes": null` aborted the load before the tombstones: '
                            "a deleted saved queue was re-added by a sync (and the "
                            "next save makes the loss permanent)")
    finally:
        await stop(hub)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


async def test_good_main_wins_over_stale_backup():
    """Case 10."""
    port, failures = _free_port(), []
    tmpdir = tempfile.mkdtemp(prefix="hub-durability-")
    state = os.path.join(tmpdir, "state.json")
    bak = state + ".bak"
    url = f"ws://127.0.0.1:{port}"

    def doc(mix_id, name):
        return {"session": {"rev": 1, "queue": [], "index": 0}, "savedQueues": [],
                "deletedSavedQueues": {}, "lbIndex": None, "devices": [],
                "mixes": [{"id": mix_id, "name": name, "kind": "genre", "seedId": "g",
                           "count": 50, "createdAt": 1000, "updatedAt": 2000,
                           "lastPlayedAt": 0}]}

    with open(state, "w", encoding="utf-8") as f:
        json.dump(doc("mx_current", "Current"), f)
    with open(bak, "w", encoding="utf-8") as f:
        json.dump(doc("mx_stale", "Stale"), f)
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)
        if [m.get("name") for m in c.mixes] != ["Current"]:
            failures.append("a stale backup beat a readable state.json: "
                            f"welcome.mixes={[m.get('name') for m in c.mixes]}")
        if not await until(lambda: mix_names(bak) == ["Current"]):
            failures.append("the stale backup was not brought in line at boot: "
                            f"{mix_names(bak)}")
        if set_asides(state):
            failures.append(f"a readable state.json was set aside: {set_asides(state)}")
    finally:
        await stop(hub)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


async def main():
    sys.path.insert(0, HUB_DIR)
    failures = []
    failures += await test_fsync_only_for_the_irreplaceable()
    failures += test_boot_brings_the_backup_in_line()
    failures += await test_backup_and_recovery()
    failures += await test_bad_sections_do_not_abort_the_rest()
    failures += await test_good_main_wins_over_stale_backup()
    if failures:
        print("FAIL (state durability):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - state durability: bookkeeping-saves-no-fsync/mix-and-tombstone-"
          "fsynced-to-backup/single-flight-latest-wins/dir-fsync-refusal-swallowed/"
          "no-backup-without-mix/empty-main-restored/corrupt-main-set-aside-and-"
          "restored/corrupt-without-backup-kept/bad-section-does-not-abort-load/"
          "good-main-beats-stale-backup/backup-written-at-first-boot/matching-"
          "backup-untouched/missing-main-restored/both-corrupt-both-kept")


if __name__ == "__main__":
    asyncio.run(main())
