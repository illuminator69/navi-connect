#!/usr/bin/env python3
"""
Worbus P2, H1: the owner's state migrates in place, and a rollback keeps it.

The hub now keeps one space per Navidrome user (`UserSpace`). The primary user's
space (HUB_ND_USER) stays in today's top-level keys of state.json and in today's
station side files; every other user is under `"users": {key: {...same keys...}}`
with their station files at `<HUB_STATE>.users/<key>.history` / `.stations`.

  1. test_state_without_users_loads_as_primary: a P1-shaped state.json (session,
     saved queues and tombstones, mixes, devices, sleep timer, lbIndex) and its
     station side files load whole into the primary user's space, and nowhere else.
  2. test_primary_space_stays_in_top_level_keys: the ROLLBACK check. A file the new
     hub saved, with a second user in it, is loaded by a parser that knows only
     today's keys -- the base hub's own `Hub()` (P1 hub.py @7efa17f, read with
     `git show`), so it is the real rollback; a minimal P1-keys parser when git or
     that commit is not there (the PASS line says which ran). The primary user's
     data is all there and the other user's is not.
  3. test_other_users_round_trip_under_users: another user's space round-trips
     under `users`, nothing of it leaks into the top-level keys, `deviceAuth`
     round-trips, a malformed `users` entry is skipped without costing the rest,
     and another user's mixes reach the fsynced backup (they are as irreplaceable
     as the owner's).
  4. test_station_side_files_per_user: each user's play history and stations are
     written to and read from their own side files; the primary user's stay at
     today's paths; a key that is not a plain file name cannot leave `.users/`.
  5. test_deleted_user_space_archived (R37): a user Navidrome no longer has is
     moved to `archivedUsers` under `<key>@<unix time>`, side files into
     `<HUB_STATE>.archive/`; a new space of the same name is empty; nothing is
     deleted; the primary user is never archived; the backup carries the archive.
     (The rollback check, 2, also runs with an archived user in the file.)

In process, no sockets. Exits non-zero on failure.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

os.environ.setdefault("HUB_TOKEN", "test-token")
os.environ.setdefault("NAVIDROME_URL", "http://127.0.0.1:9")
os.environ.setdefault("HUB_ND_USER", "Owner")
os.environ.setdefault("HUB_ND_PASS", "test-pass")
os.environ["HUB_MIRROR_PLAYQUEUE"] = "false"
HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB_DIR)

import hub as hubmod  # noqa: E402

BASE_COMMIT = "7efa17f"
NOW = int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# The P1-shaped fixture
# --------------------------------------------------------------------------- #
def _song(i: int) -> dict:
    return {"id": f"tr{i}", "title": f"Track {i}", "artist": "Artist A",
            "album": "Album X", "durationMs": 200_000}


def _station(sid: str) -> dict:
    st = hubmod.Station(id=sid, seed={"kind": "track", "ids": ["tr1"]}, name="Radio",
                        character="close", created_at=NOW, used_at=NOW, seed_ids=["tr1"],
                        anchor_meta=[{"id": "tr1", "title": "", "artist": "", "album": "",
                                      "ak": "", "tk": "", "feats": None, "mix": False}])
    return st.to_state()


def p1_fixture() -> dict:
    """What a P1 hub writes (its `_state_payload`), with one of everything."""
    return {
        "session": {"rev": 7, "activeDeviceId": "phone-1",
                    "queue": [_song(1), _song(2), _song(3)], "index": 1, "order": None,
                    "positionMs": 42_000, "isPlaying": True, "repeat": "all",
                    "shuffle": False, "updatedAt": NOW, "savedQueueId": "q_1",
                    "sourceKind": "album", "sourceName": "Album X",
                    "stationId": "st_1_own"},
        "sleepTimerEndsAt": NOW + 30 * 60_000, "sleepTimerMinutes": 45,
        "sleepTimerSetBy": "phone-1",
        "savedQueues": [
            {"id": "q_1", "songs": [_song(1), _song(2), _song(3)], "currentIndex": 1,
             "positionMs": 42_000, "sourceKind": "album", "sourceName": "Album X",
             "name": "Album X", "createdAt": NOW - 5000, "updatedAt": NOW},
            {"id": "q_2", "songs": [_song(9)], "sourceKind": "manual",
             "createdAt": NOW - 9000, "updatedAt": NOW - 8000},
        ],
        "deletedSavedQueues": {"q_old": NOW - 100_000},
        "mixes": [{"id": "mx_1", "name": "Late Night", "kind": "genre", "seedId": "g1",
                   "count": 40, "createdAt": NOW - 1000, "updatedAt": NOW - 1000,
                   "lastPlayedAt": 0}],
        "lbIndex": {"seq": 5, "epoch": "ep-1"},
        "devices": [
            {"id": "phone-1", "name": "Owner phone", "platform": "android", "kind": "phone",
             "caps": ["controller", "receiver"], "volume": 60, "lastSeen": NOW},
            {"id": "cast-abc", "name": "Living room", "platform": "cast", "kind": "speaker",
             "caps": ["receiver"], "volume": 30, "lastSeen": NOW},
        ],
    }


def write_fixture(state: str) -> dict:
    data = p1_fixture()
    with open(state, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with open(state + hubmod.STATION_STATE_SUFFIX, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "stations": [_station("st_1_own")]}, f)
    with open(state + hubmod.STATION_HISTORY_SUFFIX, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "plays": [["tr1", "abcd", NOW - 1000], ["tr2", "beef", NOW]]}, f)
    return data


def new_hub(state: str, nd_user: str = "Owner") -> "hubmod.Hub":
    hubmod.STATE_PATH = state
    hubmod.ND_USER = nd_user
    return hubmod.Hub()


def check_primary_holds_fixture(label: str, h: object, fx: dict, failures: list) -> None:
    """`h` is anything with the P1 attributes (the new hub's primary space, or a
    P1 Hub): it holds every part of the fixture."""
    s = h.session
    q = [t.get("id") for t in s.queue]
    if q != ["tr1", "tr2", "tr3"]:
        failures.append(f"{label}: queue {q}")
    if (s.index, s.position_ms, s.repeat, s.saved_queue_id, s.source_name) != (
            1, 42_000, "all", "q_1", "Album X"):
        failures.append(f"{label}: session fields {(s.index, s.position_ms, s.repeat,
                                                     s.saved_queue_id, s.source_name)}")
    if s.station_id != "st_1_own":
        failures.append(f"{label}: the session lost its station binding ({s.station_id!r})")
    if s.is_playing or s.active_device_id is not None:
        failures.append(f"{label}: a restart must leave the session paused and unowned")
    if set(h.saved_queues) != {"q_1", "q_2"}:
        failures.append(f"{label}: saved queues {sorted(h.saved_queues)}")
    if dict(h.deleted_saved_queues) != fx["deletedSavedQueues"]:
        failures.append(f"{label}: tombstones {h.deleted_saved_queues}")
    if set(h.mixes) != {"mx_1"} or h.mixes["mx_1"].get("name") != "Late Night":
        failures.append(f"{label}: mixes {h.mixes}")
    if set(h.devices) != {"phone-1", "cast-abc"}:
        failures.append(f"{label}: devices {sorted(h.devices)}")
    else:
        d = h.devices["phone-1"]
        if (d.name, d.volume, d.kind) != ("Owner phone", 60, "phone"):
            failures.append(f"{label}: the phone row came back {(d.name, d.volume, d.kind)}")
    t = h._sleep  # noqa: SLF001
    if t is None or t.minutes != 45 or t.set_by != "phone-1":
        failures.append(f"{label}: the sleep timer did not come back ({t})")
    if set(h.stations.stations) != {"st_1_own"}:
        failures.append(f"{label}: stations {sorted(h.stations.stations)}")
    if [p[0] for p in h.stations.history] != ["tr1", "tr2"]:
        failures.append(f"{label}: play history {h.stations.history}")


# --------------------------------------------------------------------------- #
# 1
# --------------------------------------------------------------------------- #
def test_state_without_users_loads_as_primary(tmp: str) -> list[str]:
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    fx = write_fixture(state)
    h = new_hub(state)
    if h.primary_user != "owner":
        failures.append(f"primary_user is {h.primary_user!r}, not HUB_ND_USER lower-cased")
    if set(h.spaces) != {"owner"}:
        failures.append(f"a file without `users` made spaces {sorted(h.spaces)}")
        return failures
    sp = h.spaces["owner"]
    if h.space_for("OWNER") is not sp or h.primary_space is not sp:
        failures.append("space_for / primary_space do not return the primary user's space")
    check_primary_holds_fixture("primary space", sp, fx, failures)
    if h.lb_index != {"seq": 5, "epoch": "ep-1"}:
        failures.append(f"lbIndex is the hub's, and it did not load: {h.lb_index}")
    if h.device_auth != {}:
        failures.append(f"a P1 file has no deviceAuth, got {h.device_auth}")
    # The migration is in place: nothing else moved, and the next save writes the
    # owner's data back at the top level.
    h._save()  # noqa: SLF001
    with open(state, encoding="utf-8") as f:
        saved = json.load(f)
    for key in ("session", "savedQueues", "deletedSavedQueues", "mixes", "devices",
                "sleepTimerEndsAt", "lbIndex"):
        if key not in saved:
            failures.append(f"the first save dropped top-level {key!r}")
    if [t["id"] for t in saved.get("session", {}).get("queue", [])] != ["tr1", "tr2", "tr3"]:
        failures.append("the first save did not write the owner's queue at the top level")
    if saved.get("users") not in ({}, None):
        failures.append(f"the first save invented other users: {saved.get('users')}")
    return failures


# --------------------------------------------------------------------------- #
# 2 -- the rollback
# --------------------------------------------------------------------------- #
def _load_p1_module(tmp: str):
    """The base hub, from git. None when the commit is not reachable here (the
    workstation's `navi-connect/hub` repo has other history)."""
    for spec in (f"{BASE_COMMIT}:hub/hub.py", f"{BASE_COMMIT}:hub.py"):
        try:
            src = subprocess.run(["git", "-C", HUB_DIR, "show", spec], capture_output=True,
                                 timeout=20, check=True).stdout
        except Exception:  # noqa: BLE001 — no git, no such commit or path
            continue
        path = os.path.join(tmp, "hub_p1_base.py")
        with open(path, "wb") as f:
            f.write(src)
        mod_spec = importlib.util.spec_from_file_location("hub_p1_base", path)
        mod = importlib.util.module_from_spec(mod_spec)
        sys.modules["hub_p1_base"] = mod   # dataclasses look their module up here
        mod_spec.loader.exec_module(mod)
        if hasattr(mod, "UserSpace"):
            raise AssertionError("the 'P1' hub from git already knows UserSpace")
        return mod
    return None


def _base_commit_present() -> bool:
    """Whether BASE_COMMIT exists in the repository holding this hub (the VM's
    navi-connect: it must). False where git or the commit is absent."""
    try:
        return subprocess.run(["git", "-C", HUB_DIR, "cat-file", "-e",
                               f"{BASE_COMMIT}^{{commit}}"], capture_output=True,
                              timeout=20).returncode == 0
    except Exception:  # noqa: BLE001 — no git
        return False


class _P1KeysOnly:
    """Fallback rollback parser: reads exactly the keys P1 reads, nothing else."""

    def __init__(self, state: str) -> None:
        with open(state, encoding="utf-8") as f:
            data = json.load(f)
        s = data.get("session", {})
        self.session = hubmod.Session(
            queue=s.get("queue", []), index=s.get("index", 0),
            position_ms=s.get("positionMs", 0), repeat=s.get("repeat", "none"),
            saved_queue_id=s.get("savedQueueId"), source_name=s.get("sourceName"),
            station_id=s.get("stationId"))
        self.saved_queues = {r["id"]: r for r in data.get("savedQueues", [])}
        self.deleted_saved_queues = dict(data.get("deletedSavedQueues") or {})
        self.mixes = {m["id"]: m for m in data.get("mixes", [])}
        self.devices = {d["id"]: hubmod.Device(id=d["id"], name=d.get("name"),
                                               volume=d.get("volume"), kind=d.get("kind"))
                        for d in data.get("devices", [])}
        self._sleep = None
        if data.get("sleepTimerEndsAt"):
            self._sleep = hubmod.SleepTimer(minutes=data.get("sleepTimerMinutes"),
                                            set_by=data.get("sleepTimerSetBy"),
                                            ends_at=data["sleepTimerEndsAt"], deadline=0)

        class _St:
            pass
        self.stations = _St()
        with open(state + ".stations", encoding="utf-8") as f:
            self.stations.stations = {r["id"]: r for r in json.load(f)["stations"]}
        with open(state + ".history", encoding="utf-8") as f:
            self.stations.history = json.load(f)["plays"]


ROLLBACK_PARSER = "?"


def test_primary_space_stays_in_top_level_keys(tmp: str) -> list[str]:
    global ROLLBACK_PARSER
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    fx = write_fixture(state)
    h = new_hub(state)
    bob = h.space_for("Bob")
    bob.session.queue = [{"id": "bob-tr", "title": "Bob's"}]
    bob.devices["bob-phone"] = hubmod.Device(id="bob-phone", name="Bob phone",
                                             last_seen=NOW)
    if bob._save_mix({"name": "Bob mix", "kind": "genre", "seedId": "g2"}) is None:  # noqa: SLF001
        failures.append("could not save a mix in bob's space")
    bob.deleted_saved_queues["q_bob_gone"] = NOW
    bob.stations.stations["st_bob"] = hubmod.Station.from_state(_station("st_bob"))
    bob.stations._history_add("bob-tr", "cafe")  # noqa: SLF001
    # R37: an archived user is in the file too, and a P1 hub must not see them.
    carol = h.space_for("carol")
    carol.session.queue = [{"id": "carol-tr", "title": "Carol's"}]
    carol.devices["carol-phone"] = hubmod.Device(id="carol-phone", name="Carol phone",
                                                 last_seen=NOW)
    if h._archive_space("carol") is None:  # noqa: SLF001
        failures.append("carol's space was not archived")
    h._save()  # noqa: SLF001
    for sp in h.spaces.values():
        sp.stations.close()

    p1 = _load_p1_module(tmp)
    if p1 is not None:
        ROLLBACK_PARSER = f"P1 hub.py @{BASE_COMMIT}"
        p1.STATE_PATH = state
        old = p1.Hub()
    else:
        ROLLBACK_PARSER = "P1-keys fallback parser"
        if _base_commit_present():
            # H1 review Minor 4: in this repo the real base hub is there, so the
            # fallback would hide a broken loader behind a weaker check.
            failures.append(f"the base commit {BASE_COMMIT} is in this repository but its "
                            "hub.py could not be read: the rollback ran on the fallback "
                            "parser only")
        old = _P1KeysOnly(state)
    check_primary_holds_fixture(f"rollback ({ROLLBACK_PARSER})", old, fx, failures)
    leaked = []
    if any(t.get("id") == "bob-tr" for t in old.session.queue):
        leaked.append("queue")
    if "bob-phone" in old.devices:
        leaked.append("device")
    if any(m.get("name") == "Bob mix" for m in old.mixes.values()):
        leaked.append("mix")
    if "q_bob_gone" in old.deleted_saved_queues:
        leaked.append("tombstone")
    if "st_bob" in old.stations.stations:
        leaked.append("station")
    if any(p[0] == "bob-tr" for p in old.stations.history):
        leaked.append("play history")
    if any(t.get("id") == "carol-tr" for t in old.session.queue) or "carol-phone" in old.devices:
        leaked.append("archived user's queue or device")
    if leaked:
        failures.append(f"the rollback sees bob's {', '.join(leaked)} as the owner's")
    return failures


# --------------------------------------------------------------------------- #
# 3
# --------------------------------------------------------------------------- #
def test_other_users_round_trip_under_users(tmp: str) -> list[str]:
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    write_fixture(state)
    h = new_hub(state)
    bob = h.space_for("Bob")
    if bob is h.primary_space or bob.key != "bob":
        failures.append(f"space_for('Bob') gave key {bob.key!r}")
    bob.session.queue = [_song(7), _song(8)]
    bob.session.index = 1
    bob.session.position_ms = 1234
    bob.session.saved_queue_id = "q_b"
    rec = bob._sanitize_saved_queue({"id": "q_b", "songs": [_song(7), _song(8)],  # noqa: SLF001
                                     "createdAt": NOW, "updatedAt": NOW})
    bob.saved_queues["q_b"] = rec
    bob.deleted_saved_queues["q_bx"] = NOW
    mid = bob._save_mix({"name": "Bob mix", "kind": "genre", "seedId": "g2"})  # noqa: SLF001
    bob.devices["bob-phone"] = hubmod.Device(id="bob-phone", name="Bob phone",
                                             platform="android", volume=55, last_seen=NOW)
    bob._sleep = hubmod.SleepTimer(minutes=20, set_by="bob-phone",  # noqa: SLF001
                                   ends_at=NOW + 10 * 60_000,
                                   deadline=time.monotonic() + 600)
    sha = "ab" * 32
    h.device_auth[sha] = {"user": "bob", "deviceId": "bob-phone", "createdAt": NOW}
    h._save()  # noqa: SLF001

    with open(state, encoding="utf-8") as f:
        raw = json.load(f)
    users = raw.get("users")
    if not isinstance(users, dict) or set(users) != {"bob"}:
        failures.append(f"`users` is {users!r}, not {{'bob': ...}}")
        return failures
    ub = users["bob"]
    for key in ("session", "savedQueues", "deletedSavedQueues", "mixes", "devices",
                "sleepTimerEndsAt", "sleepTimerMinutes", "sleepTimerSetBy"):
        if key not in ub:
            failures.append(f"users.bob lacks {key!r}")
    if "lbIndex" in ub or "users" in ub or "deviceAuth" in ub:
        failures.append(f"users.bob carries hub-wide keys: {sorted(ub)}")
    top_q = [t["id"] for t in raw["session"]["queue"]]
    if top_q != ["tr1", "tr2", "tr3"]:
        failures.append(f"bob's session reached the top level: {top_q}")
    if any(m["id"] == mid for m in raw["mixes"]) or "bob-phone" in {
            d["id"] for d in raw["devices"]} or "q_b" in {r["id"] for r in raw["savedQueues"]}:
        failures.append("bob's rows reached the top-level keys")
    if raw.get("deviceAuth") != {sha: {"user": "bob", "deviceId": "bob-phone",
                                       "createdAt": NOW}}:
        failures.append(f"deviceAuth saved as {raw.get('deviceAuth')}")

    # Another user's mixes are as irreplaceable as the owner's: the backup has them.
    with open(state + hubmod.STATE_BACKUP_SUFFIX, encoding="utf-8") as f:
        bak = json.load(f)
    if not any(m["id"] == mid for m in (bak.get("users", {}).get("bob", {}).get("mixes") or [])):
        failures.append("bob's new mix did not reach the fsynced backup")

    # A malformed `users` section costs only itself.
    raw["users"]["carol"] = "not an object"
    raw["users"]["OWNER"] = {"session": {"queue": [{"id": "dup"}]}}
    raw["users"][""] = {}
    raw["users"]["dave"] = {"mixes": None, "devices": [{"name": "no id"}],
                            "session": {"queue": [_song(4)]}}
    raw["deviceAuth"]["zz"] = {"user": "bob"}   # not a sha256 hex: dropped
    with open(state, "w", encoding="utf-8") as f:
        json.dump(raw, f)

    h2 = new_hub(state)
    if set(h2.spaces) != {"owner", "bob", "dave"}:
        failures.append(f"reload made spaces {sorted(h2.spaces)}")
        return failures
    b2 = h2.spaces["bob"]
    if [t["id"] for t in b2.session.queue] != ["tr7", "tr8"] or b2.session.index != 1 \
            or b2.session.position_ms != 1234 or b2.session.saved_queue_id != "q_b":
        failures.append("bob's session did not round-trip")
    if set(b2.saved_queues) != {"q_b"} or set(b2.deleted_saved_queues) != {"q_bx"}:
        failures.append(f"bob's saved queues / tombstones: {sorted(b2.saved_queues)} "
                        f"{sorted(b2.deleted_saved_queues)}")
    if set(b2.mixes) != {mid}:
        failures.append(f"bob's mixes: {sorted(b2.mixes)}")
    if set(b2.devices) != {"bob-phone"} or b2.devices["bob-phone"].volume != 55:
        failures.append(f"bob's devices: {sorted(b2.devices)}")
    if b2._sleep is None or b2._sleep.minutes != 20:  # noqa: SLF001
        failures.append("bob's sleep timer did not round-trip")
    if [t["id"] for t in h2.primary_space.session.queue] != ["tr1", "tr2", "tr3"]:
        failures.append("the owner's queue changed on reload (a `users.OWNER` entry won?)")
    if [t["id"] for t in h2.spaces["dave"].session.queue] != ["tr4"]:
        failures.append("dave's readable session was lost with his unreadable sections")
    if set(h2.device_auth) != {sha}:
        failures.append(f"deviceAuth reloaded as {sorted(h2.device_auth)}")
    return failures


# --------------------------------------------------------------------------- #
# 4
# --------------------------------------------------------------------------- #
def test_station_side_files_per_user(tmp: str) -> list[str]:
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    write_fixture(state)
    h = new_hub(state)
    owner, bob = h.primary_space, h.space_for("bob")
    odd = h.space_for("../x/y")
    paths = {
        "owner history": (owner.stations._history_file.path,  # noqa: SLF001
                          state + hubmod.STATION_HISTORY_SUFFIX),
        "owner stations": (owner.stations._stations_file.path,  # noqa: SLF001
                           state + hubmod.STATION_STATE_SUFFIX),
        "bob history": (bob.stations._history_file.path,  # noqa: SLF001
                        os.path.join(state + ".users", "bob.history")),
        "bob stations": (bob.stations._stations_file.path,  # noqa: SLF001
                         os.path.join(state + ".users", "bob.stations")),
    }
    for label, (got, want) in paths.items():
        if got != want:
            failures.append(f"{label} file is {got}, not {want}")
    users_dir = os.path.realpath(state + ".users")
    for f in (odd.stations._history_file.path, odd.stations._stations_file.path):  # noqa: SLF001
        if os.path.dirname(os.path.realpath(f)) != users_dir:
            failures.append(f"a key with '/' and '..' put its file at {f}")

    bob.stations.stations["st_bob"] = hubmod.Station.from_state(_station("st_bob"))
    bob.stations._stations_touch()  # noqa: SLF001
    bob.stations._history_add("bob-tr", "cafe")  # noqa: SLF001
    owner.stations._history_add("own-tr", "f00d")  # noqa: SLF001
    odd.stations._history_add("odd-tr", "0dd0")  # noqa: SLF001
    h._save()  # noqa: SLF001
    for sp in h.spaces.values():
        sp.stations.close()

    def read(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    try:
        oh = [p[0] for p in read(paths["owner history"][1])["plays"]]
        os_ = [s["id"] for s in read(paths["owner stations"][1])["stations"]]
        bh = [p[0] for p in read(paths["bob history"][1])["plays"]]
        bs = [s["id"] for s in read(paths["bob stations"][1])["stations"]]
    except Exception as e:  # noqa: BLE001
        failures.append(f"a side file was not written where expected: {e!r}")
        return failures
    if oh != ["tr1", "tr2", "own-tr"] or os_ != ["st_1_own"]:
        failures.append(f"the owner's side files hold {oh} / {os_}")
    if bh != ["bob-tr"] or bs != ["st_bob"]:
        failures.append(f"bob's side files hold {bh} / {bs}")

    h2 = new_hub(state)
    b2 = h2.spaces.get("bob")
    if b2 is None:
        failures.append("bob's space did not come back")
        return failures
    if set(b2.stations.stations) != {"st_bob"} or [p[0] for p in b2.stations.history] != ["bob-tr"]:
        failures.append(f"bob's stations/history reloaded as {sorted(b2.stations.stations)} "
                        f"{b2.stations.history}")
    o2 = h2.primary_space
    if set(o2.stations.stations) != {"st_1_own"} or "bob-tr" in [p[0] for p in o2.stations.history]:
        failures.append("the owner's stations/history picked up bob's")
    o3 = h2.spaces.get("../x/y")
    if o3 is None or [p[0] for p in o3.stations.history] != ["odd-tr"]:
        failures.append("the odd key's history did not round-trip through its encoded file")
    return failures


# --------------------------------------------------------------------------- #
# R37: a deleted user's space is archived, not inherited
# --------------------------------------------------------------------------- #
def test_deleted_user_space_archived(tmp: str) -> list[str]:
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    fx = write_fixture(state)
    h = new_hub(state)
    bob = h.space_for("bob")
    bob.session.queue = [{"id": "bob-tr", "title": "Bob's"}]
    bob.devices["bob-phone"] = hubmod.Device(id="bob-phone", name="Bob phone", last_seen=NOW)
    if bob._save_mix({"name": "Bob mix", "kind": "genre", "seedId": "g2"}) is None:  # noqa: SLF001
        failures.append("could not save a mix in bob's space")
    bob.deleted_saved_queues["q_bob_gone"] = NOW
    bob.stations.stations["st_bob"] = hubmod.Station.from_state(_station("st_bob"))
    bob.stations._stations_touch()  # noqa: SLF001
    bob.stations._history_add("bob-tr", "cafe")  # noqa: SLF001
    h._save()  # noqa: SLF001
    live = os.path.join(state + ".users", "bob")
    for suffix in (hubmod.STATION_HISTORY_SUFFIX, hubmod.STATION_STATE_SUFFIX):
        if not os.path.exists(live + suffix):
            failures.append(f"precondition: bob's {suffix} side file was not written")

    if h._archive_space(h.primary_user) is not None or h.primary_user not in h.spaces:  # noqa: SLF001
        failures.append("the primary user's space was archived")
    akey = h._archive_space("bob")  # noqa: SLF001
    h._save(blocking_backup=True)  # noqa: SLF001
    if not (isinstance(akey, str) and akey.startswith("bob@")
            and akey[4:].isdigit()):
        failures.append(f"the archive key is {akey!r}, not bob@<unix time>")
        return failures
    if "bob" in h.spaces:
        failures.append("bob's space is still live after archiving")
    for suffix in (hubmod.STATION_HISTORY_SUFFIX, hubmod.STATION_STATE_SUFFIX):
        if os.path.exists(live + suffix):
            failures.append(f"bob's {suffix} side file is still where a new bob reads it")
    entry = h.archived_users.get(akey) or {}
    side = os.path.join(tmp, entry.get("sideFiles") or "missing")
    try:
        with open(side + hubmod.STATION_HISTORY_SUFFIX, encoding="utf-8") as f:
            plays = [p[0] for p in json.load(f)["plays"]]
        with open(side + hubmod.STATION_STATE_SUFFIX, encoding="utf-8") as f:
            sts = [x["id"] for x in json.load(f)["stations"]]
    except Exception as e:  # noqa: BLE001
        failures.append(f"the archived side files are not at {entry.get('sideFiles')!r}: {e!r}")
        plays, sts = [], []
    if plays != ["bob-tr"] or sts != ["st_bob"]:
        failures.append(f"the archived side files hold {plays} / {sts}")
    if not os.path.realpath(side).startswith(os.path.realpath(state + ".archive") + os.sep):
        failures.append(f"the archived side files are outside .archive/: {side}")

    # A late write by the archived space's engine goes to the archive, not to `.users/bob`.
    bob.stations._history_add("late-tr", "beef")  # noqa: SLF001
    if os.path.exists(live + hubmod.STATION_HISTORY_SUFFIX):
        failures.append("a late write of the archived space recreated .users/bob.history")

    # Reloaded: no bob space; the archive whole; a new bob starts empty.
    h2 = new_hub(state)
    if "bob" in h2.spaces:
        failures.append("bob's space came back from the file after archiving")
    got = (h2.archived_users.get(akey) or {})
    sp_state = got.get("space") or {}
    q = [t.get("id") for t in (sp_state.get("session") or {}).get("queue", [])]
    devs = [d.get("id") for d in sp_state.get("devices", [])] if isinstance(
        sp_state.get("devices"), list) else list((sp_state.get("devices") or {}).keys())
    mixes = json.dumps(sp_state.get("mixes"))
    if got.get("user") != "bob" or q != ["bob-tr"] or "bob-phone" not in json.dumps(devs) \
            or "Bob mix" not in mixes or "q_bob_gone" not in json.dumps(sp_state):
        failures.append(f"the archive does not hold bob's data: user {got.get('user')!r}, "
                        f"queue {q}, devices {devs}, mix {'Bob mix' in mixes}")
    nb = h2.space_for("bob")
    if (nb.session.queue or nb.devices or nb.mixes or nb.deleted_saved_queues
            or nb.stations.stations or nb.stations.history):
        failures.append(f"a new bob inherited data: queue {len(nb.session.queue)}, devices "
                        f"{list(nb.devices)}, mixes {len(nb.mixes)}, stations "
                        f"{list(nb.stations.stations)}, history {len(nb.stations.history)}")
    check_primary_holds_fixture("after archiving bob", h2.primary_space, fx, failures)
    for sp in h2.spaces.values():
        sp.stations.close()
    with open(state + ".bak", encoding="utf-8") as f:
        if akey not in (json.load(f).get("archivedUsers") or {}):
            failures.append("the backup does not carry the archive")

    # A second archive of the same name in the same second gets its own key.
    h2.space_for("bob").session.queue = [{"id": "bob2-tr", "title": "x"}]
    akey2 = h2._archive_space("bob")  # noqa: SLF001
    if akey2 is None or akey2 == akey or akey not in h2.archived_users:
        failures.append(f"a second archive of bob is {akey2!r}, beside {akey!r}")
    return failures


# --------------------------------------------------------------------------- #
# 5 (R14): a long name's side files
# --------------------------------------------------------------------------- #
def test_side_file_stem_long_names(tmp: str) -> list[str]:
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    h = new_hub(state)
    users_dir = state + ".users"
    short, edge, long_ = "Ж" * 19, "ж" * 20, "Жанна" * 10   # 114 / 120 / 300 bytes quoted
    stems = {k: os.path.basename(h.space_for(k).stations._history_file.path)  # noqa: SLF001
             for k in (short, edge, long_)}
    import hashlib
    import urllib.parse
    want = {short: urllib.parse.quote(short.lower(), safe="") + ".history",
            edge: urllib.parse.quote(edge, safe="") + ".history",
            long_: "sha256-" + hashlib.sha256(long_.lower().encode()).hexdigest() + ".history"}
    for k, w in want.items():
        if stems[k] != w:
            failures.append(f"a {len(k)}-letter name's history file is {stems[k]!r}, not {w!r}")
        if len((stems[k] + ".tmp").encode()) > 255:
            failures.append(f"a {len(k)}-letter name's file name is past 255 bytes")
    sp = h.space_for(long_)
    sp.stations._history_add("long-tr", "a1a1")  # noqa: SLF001
    sp.stations.stations["st_long"] = hubmod.Station.from_state(_station("st_long"))
    sp.stations._stations_touch()  # noqa: SLF001
    h._save()  # noqa: SLF001
    for s in h.spaces.values():
        s.stations.close()
    h2 = new_hub(state)
    s2 = h2.spaces.get(long_.lower())
    if s2 is None or [p[0] for p in s2.stations.history] != ["long-tr"] \
            or set(s2.stations.stations) != {"st_long"}:
        failures.append("a long Cyrillic name's stations/history did not round-trip")
    if any(os.path.basename(p).startswith("%") and len(p) > 200 for p in os.listdir(users_dir)):
        failures.append(f"a long encoded stem reached the disk: {os.listdir(users_dir)}")
    return failures


# --------------------------------------------------------------------------- #
# 6 (R15): HUB_ND_USER changed between two starts
# --------------------------------------------------------------------------- #
def _bob_into(h: "hubmod.Hub") -> None:
    bob = h.space_for("bob")
    bob.session.queue = [_song(7), _song(8)]
    bob.session.saved_queue_id = "q_b"
    bob.saved_queues["q_b"] = bob._sanitize_saved_queue(  # noqa: SLF001
        {"id": "q_b", "songs": [_song(7)], "createdAt": NOW, "updatedAt": NOW})
    bob.mixes["mx_b"] = hubmod.Hub._sanitize_mix({"id": "mx_b", "name": "Bob mix",
                                                  "kind": "genre", "seedId": "g2"})
    bob.devices["bob-phone"] = hubmod.Device(id="bob-phone", name="Bob phone", last_seen=NOW)
    bob.deleted_saved_queues["q_bx"] = NOW
    bob.stations.stations["st_bob"] = hubmod.Station.from_state(_station("st_bob"))
    bob.stations._stations_touch()  # noqa: SLF001
    bob.stations._history_add("bob-tr", "cafe")  # noqa: SLF001
    bob.session.station_id = "st_bob"


def _check_bob(label: str, sp: object, failures: list) -> None:
    if [t.get("id") for t in sp.session.queue] != ["tr7", "tr8"]:
        failures.append(f"{label}: bob's queue {[t.get('id') for t in sp.session.queue]}")
    if set(sp.saved_queues) != {"q_b"} or set(sp.mixes) != {"mx_b"} \
            or set(sp.devices) != {"bob-phone"} or set(sp.deleted_saved_queues) != {"q_bx"}:
        failures.append(f"{label}: bob's rows {sorted(sp.saved_queues)} {sorted(sp.mixes)} "
                        f"{sorted(sp.devices)} {sorted(sp.deleted_saved_queues)}")
    if set(sp.stations.stations) != {"st_bob"} or [p[0] for p in sp.stations.history] != ["bob-tr"]:
        failures.append(f"{label}: bob's stations/history {sorted(sp.stations.stations)} "
                        f"{sp.stations.history}")
    if sp.session.station_id != "st_bob":
        failures.append(f"{label}: bob's station binding {sp.session.station_id!r}")


def test_primary_user_change_moves_spaces(tmp: str) -> list[str]:
    global ROLLBACK_PARSER
    import contextlib
    import io
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    fx = write_fixture(state)
    h = new_hub(state)
    _bob_into(h)
    h._save()  # noqa: SLF001
    for sp in h.spaces.values():
        sp.stations.close()
    with open(state, encoding="utf-8") as f:
        if json.load(f).get("primaryUser") != "owner":
            failures.append("state.json does not name its primary user")

    # owner -> bob
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        h2 = new_hub(state, "Bob")
    moved = [ln for ln in out.getvalue().splitlines() if "primary user changed" in ln]
    if len(moved) != 1 or "'owner'" not in moved[0] or "'bob'" not in moved[0]:
        failures.append(f"the move was not logged once by name: {moved}")
    if h2.primary_user != "bob" or set(h2.spaces) != {"bob", "owner"}:
        failures.append(f"after the move: primary {h2.primary_user!r}, spaces {sorted(h2.spaces)}")
        return failures
    _check_bob("bob, now primary", h2.primary_space, failures)
    check_primary_holds_fixture("owner, now under users", h2.spaces["owner"], fx, failures)
    with open(state, encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("primaryUser") != "bob" or [t["id"] for t in raw["session"]["queue"]] != ["tr7", "tr8"]:
        failures.append("the move was not saved: bob is not the top level")
    if [t["id"] for t in raw.get("users", {}).get("owner", {}).get("session", {}).get("queue", [])] \
            != ["tr1", "tr2", "tr3"]:
        failures.append("the move was not saved: the owner is not under users")
    with open(state + ".history", encoding="utf-8") as f:
        if [p[0] for p in json.load(f)["plays"]] != ["bob-tr"]:
            failures.append("today's history file is not bob's after the move")
    with open(os.path.join(state + ".users", "owner.history"), encoding="utf-8") as f:
        if [p[0] for p in json.load(f)["plays"]] != ["tr1", "tr2"]:
            failures.append("the owner's history did not move under .users/")
    if os.path.exists(os.path.join(state + ".users", "bob.history")):
        failures.append("bob's old .users/ copy was left behind")

    # bob -> owner: and back, nothing lost either way
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        h3 = new_hub(state, "owner")
    if sum("primary user changed" in ln for ln in out.getvalue().splitlines()) != 1:
        failures.append("the move back was not logged once")
    check_primary_holds_fixture("owner, primary again", h3.primary_space, fx, failures)
    if "bob" not in h3.spaces:
        failures.append("bob's space was lost on the move back")
    else:
        _check_bob("bob, under users again", h3.spaces["bob"], failures)
    # A third start with the same user moves nothing.
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        new_hub(state, "owner")
    if "primary user changed" in out.getvalue():
        failures.append("a start with an unchanged HUB_ND_USER moved the spaces")

    # The rollback still finds the owner at the top level.
    p1 = _load_p1_module(tmp)
    if p1 is not None:
        p1.STATE_PATH = state
        old = p1.Hub()
    else:
        old = _P1KeysOnly(state)
    check_primary_holds_fixture("rollback after two moves", old, fx, failures)
    if any(t.get("id") == "tr7" for t in old.session.queue) or "st_bob" in old.stations.stations:
        failures.append("the rollback after two moves sees bob's data as the owner's")
    return failures


def test_move_to_a_user_without_a_space(tmp: str) -> list[str]:
    """HUB_ND_USER changed to someone the file never had: an empty primary space,
    the old one kept whole under users. And a file without `primaryUser` (P1's)
    belongs to whoever HUB_ND_USER is now."""
    failures: list[str] = []
    state = os.path.join(tmp, "state.json")
    fx = write_fixture(state)
    h = new_hub(state)            # writes nothing yet: the fixture has no primaryUser
    h._save()  # noqa: SLF001
    h2 = new_hub(state, "carol")
    if h2.primary_space.session.queue or h2.primary_space.mixes or h2.primary_space.stations.stations:
        failures.append("carol's new primary space is not empty")
    if "owner" not in h2.spaces:
        failures.append("the owner's space was lost moving to a user with none")
    else:
        check_primary_holds_fixture("owner under users", h2.spaces["owner"], fx, failures)
    # P1's file, no primaryUser: HUB_ND_USER's, whoever that is.
    state2 = os.path.join(tmp, "p1", "state.json")
    os.makedirs(os.path.dirname(state2))
    write_fixture(state2)
    h3 = new_hub(state2, "dave")
    if set(h3.spaces) != {"dave"}:
        failures.append(f"a P1 file with HUB_ND_USER=dave made spaces {sorted(h3.spaces)}")
    else:
        check_primary_holds_fixture("P1 file as dave's", h3.primary_space, fx, failures)
    return failures


def main() -> int:
    tests = [test_state_without_users_loads_as_primary,
             test_primary_space_stays_in_top_level_keys,
             test_other_users_round_trip_under_users,
             test_station_side_files_per_user,
             test_deleted_user_space_archived,
             test_side_file_stem_long_names,
             test_primary_user_change_moves_spaces,
             test_move_to_a_user_without_a_space]
    failures: list[str] = []
    saved_path, saved_user = hubmod.STATE_PATH, hubmod.ND_USER
    for t in tests:
        tmp = tempfile.mkdtemp(prefix="state-migration-")
        try:
            got = t(tmp)
        except Exception:  # noqa: BLE001 — a crash is a failure of that test only
            got = [f"raised:\n{traceback.format_exc()}"]
        finally:
            hubmod.STATE_PATH, hubmod.ND_USER = saved_path, saved_user
            shutil.rmtree(tmp, ignore_errors=True)
        for f in got:
            print(f"FAIL - {t.__name__}: {f}")
        failures += got
    if failures:
        return 1
    print("PASS - state migration: p1-file-loads-as-primary/rollback-keeps-owner-top-level"
          f"({ROLLBACK_PARSER})/other-users-round-trip-under-users/"
          "station-side-files-per-user/deleted-user-space-archived-not-inherited/"
          "long-name-stem-hashed/"
          "primary-user-change-moves-spaces-both-ways+rollback/move-to-a-new-user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
