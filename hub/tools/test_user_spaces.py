#!/usr/bin/env python3
"""
Worbus P2, H1: one space per user.

  1. test_startup_refuses_missing_service_account: NAVIDROME_URL, HUB_ND_USER and
     HUB_ND_PASS are required, like HUB_TOKEN. One case per variable, in the pure
     decision and in `hub.py` itself (non-zero exit, the line, no state file). The
     refusal names the setting and never a value of the others.
  2. test_space_for_is_lazy_and_case_blind: a fresh hub holds only the primary
     user's space; `space_for` creates another on first use, keyed lower-cased.
  3. test_per_user_machinery_lives_in_space: the session, devices, saved queues,
     mixes, held acts and their drain task, the intent timers, `_ended_judged`, the
     sleep timer and its sender, the mirror worker, the stations and the sockets
     are the space's, each user's its own -- and the Hub has none of them.
  4. test_station_engine_reads_its_space: a space's StationEngine reads that
     space's session (binding, dropping, the play history a departure feeds) and
     no other; its caches and probe state are one object for the whole hub.
  5. test_sleep_timer_per_space: a timer set in one space is that space's alone,
     and is saved under that user.
  6. test_legacy_connection_lands_in_primary: a real hub subprocess; a client with
     the legacy HUB_TOKEN lands in the primary user's space, whose state is saved
     in today's top-level keys.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
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
HERE = os.path.dirname(os.path.abspath(__file__))
HUB_DIR = os.path.dirname(HERE)
sys.path.insert(0, HUB_DIR)
sys.path.insert(0, HERE)

import hub as hubmod  # noqa: E402
from test_edits import TOKEN, Client, _free_port  # noqa: E402

REAL_32 = "0123456789abcdef0123456789abcdef"
URL_V, USER_V, PASS_V = "http://nd.invalid:4533", "svc-owner-name", "svc-pass-9q7x"
ND = {"NAVIDROME_URL": URL_V, "HUB_ND_USER": USER_V, "HUB_ND_PASS": PASS_V}


# --------------------------------------------------------------------------- #
# 1
# --------------------------------------------------------------------------- #
def _subprocess_refused(var: str) -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HUB_", "PREVIEW_", "AUDIOMUSE_", "LBBOT_", "NAVIDROME_"))}
        env.update(HUB_HOST="127.0.0.1", HUB_PORT=str(_free_port()),
                   HUB_HEALTH_PORT=str(_free_port()), HUB_TOKEN=REAL_32,
                   HUB_STATE=os.path.join(tmp, "state.json"), HUB_MIRROR_PLAYQUEUE="false")
        env.update({k: v for k, v in ND.items() if k != var})
        try:
            proc = subprocess.run([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                                  capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            return [f"subprocess without {var}: still running after 8 s, it started"]
        out = proc.stdout + proc.stderr
        if proc.returncode == 0:
            fails.append(f"subprocess without {var}: exit 0")
        if not any(ln.startswith(f"{var} is not set") for ln in out.splitlines()):
            fails.append(f"subprocess without {var}: no line naming it; output {out!r}")
        for other in ND.values():
            if other in out:
                fails.append(f"subprocess without {var}: the output echoes a value")
        written = os.listdir(tmp)
        if written:
            fails.append(f"subprocess without {var}: a refused start wrote {written}")
    return fails


def test_startup_refuses_missing_service_account() -> list[str]:
    failures: list[str] = []
    r = hubmod.startup_config_refusal

    def call(**over):
        nd = {"navidrome_url": URL_V, "nd_user": USER_V, "nd_pass": PASS_V}
        nd.update(over)
        return r(REAL_32, "0.0.0.0", False, "", "", **nd)

    if call() is not None:
        failures.append(f"a complete service account was refused: {call()!r}")
    for var, kw in (("NAVIDROME_URL", "navidrome_url"), ("HUB_ND_USER", "nd_user"),
                    ("HUB_ND_PASS", "nd_pass")):
        for blank in ("", "   "):
            got = call(**{kw: blank})
            if got is None:
                failures.append(f"{var}={blank!r} was accepted")
                continue
            if not got.startswith(f"{var} is not set"):
                failures.append(f"{var}={blank!r}: the refusal does not name it first: {got!r}")
            for value in (URL_V, USER_V, PASS_V):
                if value in got:
                    failures.append(f"{var}={blank!r}: the refusal echoes a value")
        failures += _subprocess_refused(var)
    # The token's own refusal still comes first (the .env.example check relies on it).
    got = r("change-me-x", "0.0.0.0", False, "", "", navidrome_url="", nd_user="", nd_pass="")
    if not (got or "").startswith("HUB_TOKEN is still the example value"):
        failures.append(f"an example token with no service account: {got!r}")
    return failures


# --------------------------------------------------------------------------- #
# 2-5, in process
# --------------------------------------------------------------------------- #
def _fresh_hub(tmp: str) -> "hubmod.Hub":
    hubmod.STATE_PATH = os.path.join(tmp, "state.json")
    hubmod.ND_USER = "Owner"
    return hubmod.Hub()


def test_space_for_is_lazy_and_case_blind(tmp: str) -> list[str]:
    failures: list[str] = []
    h = _fresh_hub(tmp)
    if h.primary_user != "owner":
        failures.append(f"primary_user {h.primary_user!r}")
    if set(h.spaces) != {"owner"}:
        failures.append(f"a fresh hub holds spaces {sorted(h.spaces)}")
    bob = h.space_for("Bob")
    if set(h.spaces) != {"owner", "bob"} or bob.key != "bob":
        failures.append(f"space_for('Bob') -> key {bob.key!r}, spaces {sorted(h.spaces)}")
    if h.space_for("BOB") is not bob or h.space_for("bob") is not bob:
        failures.append("space_for is not case-blind")
    if h.primary_space is not h.spaces["owner"] or h.space_for("owner") is not h.primary_space:
        failures.append("primary_space is not the HUB_ND_USER space")
    if bob.primary or not h.primary_space.primary:
        failures.append("UserSpace.primary is wrong")
    return failures


PER_SPACE = ("session", "devices", "saved_queues", "deleted_saved_queues", "mixes",
             "_held", "_drain_task", "_play_intent_at", "_position_intent_at",
             "_pre_intent_position", "_index_intent_at", "_pre_intent_index",
             "_ended_judged", "_sleep", "_sleep_handle", "_sleep_dev", "_mirror_task",
             "_mirror_pending", "_mirror_latest", "stations", "sockets",
             "_last_sq_progress_at", "_last_position_save_at", "_last_progress_sent")


def test_per_user_machinery_lives_in_space(tmp: str) -> list[str]:
    failures: list[str] = []
    h = _fresh_hub(tmp)
    owner, bob = h.primary_space, h.space_for("bob")
    for attr in PER_SPACE:
        for sp in (owner, bob):
            if not hasattr(sp, attr):
                failures.append(f"space {sp.key} has no {attr}")
        if hasattr(h, attr):
            failures.append(f"the Hub still has a hub-wide {attr}")
    for attr in ("session", "devices", "saved_queues", "deleted_saved_queues", "mixes",
                 "_held", "_sleep_dev", "stations"):
        if hasattr(owner, attr) and getattr(owner, attr) is getattr(bob, attr, None):
            failures.append(f"{attr} is one object for two users")
    if owner.stations.space is not owner or bob.stations.space is not bob:
        failures.append("a space's StationEngine is not bound to that space")
    if hasattr(owner.stations, "hub"):
        failures.append("StationEngine still holds a `hub` it could read the session from")
    return failures


def _queue_space(sp: "hubmod.UserSpace", n: int = 3) -> None:
    sp.session.queue = [{"id": f"{sp.key}-{i}", "title": f"T{i}", "artist": f"A{i}",
                         "durationMs": 100_000} for i in range(n)]
    sp.session.index = 0
    sp.session.saved_queue_id = f"q_{sp.key}"
    sp.devices["rx"] = hubmod.Device(id="rx", name="rx")
    sp.session.set_active("rx")
    sp.session.is_playing = True


async def _station_engine_reads_its_space(tmp: str) -> list[str]:
    failures: list[str] = []
    h = _fresh_hub(tmp)
    owner, bob = h.primary_space, h.space_for("bob")
    now = int(time.time() * 1000)
    st = hubmod.Station(id="st_1_x", seed={"kind": "track", "ids": ["s1"]}, name="R",
                        character="close", created_at=now, used_at=now)
    owner.stations.stations[st.id] = st
    owner.session.station_id = st.id
    bob.session.station_id = st.id       # the same id, but bob has no such station
    if owner.stations.bound() is not st:
        failures.append("owner's engine does not see owner's binding")
    if bob.stations.bound() is not None:
        failures.append("bob's engine found a station in another user's engine")
    # Bob's own station, bound in bob's session: bob's engine reads bob's binding.
    sb = hubmod.Station(id="st_1_b", seed={"kind": "track", "ids": ["s2"]}, name="RB",
                        character="close", created_at=now, used_at=now)
    bob.stations.stations[sb.id] = sb
    bob.session.station_id = sb.id
    if bob.stations.bound() is not sb or owner.stations.bound() is not st:
        failures.append("an engine read another user's session binding")
    bob.stations._drop(sb.id, "test")  # noqa: SLF001
    if bob.session.station_id is not None or owner.session.station_id != st.id:
        failures.append("dropping bob's station did not unbind bob's session alone")
    bob.session.station_id = st.id
    owner.stations._drop(st.id, "test")  # noqa: SLF001
    if owner.session.station_id is not None:
        failures.append("dropping owner's station left owner's session bound")
    if bob.session.station_id != st.id:
        failures.append("dropping owner's station touched bob's session")

    # A departure feeds the play history of the space it happened in.
    _queue_space(owner)
    _queue_space(bob)
    o0, b0 = len(owner.stations.history), len(bob.stations.history)
    owner._judge_departure(0, 1, 100_000, ended=True)  # noqa: SLF001
    if len(owner.stations.history) != o0 + 1 or len(bob.stations.history) != b0:
        failures.append(f"a play in owner's space: owner {o0}->{len(owner.stations.history)}, "
                        f"bob {b0}->{len(bob.stations.history)}")
    bob._judge_departure(0, 1, 100_000, ended=True)  # noqa: SLF001
    if len(bob.stations.history) != b0 + 1 or bob.stations.history[-1][0] != "bob-0":
        failures.append(f"bob's play went to {bob.stations.history}")

    # The caches and the probe are the hub's: one object, one verdict.
    if owner.stations._lists is not bob.stations._lists:  # noqa: SLF001
        failures.append("the nearest-list cache is per user")
    if owner.stations._computing is not bob.stations._computing:  # noqa: SLF001
        failures.append("the computing semaphore is per user")
    owner.stations.tier2_ok = True
    if bob.stations.tier2_ok is not True:
        failures.append("a probe verdict seen by one space is not seen by another")
    if owner.stations._probed is not h.station_shared._probed:  # noqa: SLF001
        failures.append("the probe event is not the hub's")
    if owner.stations._binding is bob.stations._binding:  # noqa: SLF001
        failures.append("_binding is shared between users")
    await asyncio.sleep(0)
    return failures


def test_station_engine_reads_its_space(tmp: str) -> list[str]:
    return asyncio.run(_station_engine_reads_its_space(tmp))


async def _sleep_timer_per_space(tmp: str) -> list[str]:
    failures: list[str] = []
    h = _fresh_hub(tmp)
    owner, bob = h.primary_space, h.space_for("bob")
    now = int(time.time() * 1000)
    bob._sleep_set(hubmod.SleepTimer(minutes=10, set_by="bob-phone",  # noqa: SLF001
                                     ends_at=now + 600_000, deadline=time.monotonic() + 600))
    if owner._sleep is not None or owner._sleep_handle is not None:  # noqa: SLF001
        failures.append("bob's timer armed in owner's space")
    if bob._sleep_handle is None:  # noqa: SLF001
        failures.append("bob's timer has no clock")
    if owner._session_snapshot()["sleepTimer"] is not None:  # noqa: SLF001
        failures.append("owner's session shows bob's timer")
    if (bob._session_snapshot()["sleepTimer"] or {}).get("minutes") != 10:  # noqa: SLF001
        failures.append("bob's session does not show bob's timer")
    h._save()  # noqa: SLF001
    with open(hubmod.STATE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("sleepTimerEndsAt") is not None:
        failures.append("bob's timer was saved as the owner's")
    if (raw.get("users", {}).get("bob", {}) or {}).get("sleepTimerMinutes") != 10:
        failures.append("bob's timer was not saved under users.bob")
    bob._sleep_set(None)  # noqa: SLF001
    return failures


def test_sleep_timer_per_space(tmp: str) -> list[str]:
    return asyncio.run(_sleep_timer_per_space(tmp))


# --------------------------------------------------------------------------- #
# 6, a real hub
# --------------------------------------------------------------------------- #
async def _legacy_connection_lands_in_primary(tmp: str) -> list[str]:
    failures: list[str] = []
    port = _free_port()
    state = os.path.join(tmp, "state.json")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PREVIEW_")}
    env.update({"HUB_TOKEN": TOKEN, "HUB_PORT": str(port), "HUB_HOST": "127.0.0.1",
                "HUB_HEALTH_PORT": str(_free_port()), "HUB_STATE": state,
                "HUB_MIRROR_PLAYQUEUE": "false", **ND})
    for k in ("AUDIOMUSE_URL", "LBBOT_URL"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(1.2)
        a = Client("Phone", device_id="legacy-phone")
        await a.connect(f"ws://127.0.0.1:{port}")
        await a.act(action="setQueue", tracks=[{"id": "L1", "title": "One"},
                                               {"id": "L2", "title": "Two"}],
                    index=0, positionMs=0, play=False)
        await asyncio.sleep(0.6)
        await a.ws.close()
        await asyncio.sleep(0.3)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        with open(state, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        return [f"no state file after the run: {e!r}"]
    q = [t.get("id") for t in raw.get("session", {}).get("queue", [])]
    if q != ["L1", "L2"]:
        failures.append(f"the legacy client's queue is not in the top-level session: {q}")
    if "legacy-phone" not in {d.get("id") for d in raw.get("devices", [])}:
        failures.append("the legacy client's device is not in the top-level devices")
    if raw.get("users") not in ({}, None):
        failures.append(f"a legacy connection made another space: {raw.get('users')}")
    return failures


def test_legacy_connection_lands_in_primary(tmp: str) -> list[str]:
    return asyncio.run(_legacy_connection_lands_in_primary(tmp))


def main() -> int:
    failures: list[str] = []
    saved_path, saved_user = hubmod.STATE_PATH, hubmod.ND_USER
    tests = [test_space_for_is_lazy_and_case_blind, test_per_user_machinery_lives_in_space,
             test_station_engine_reads_its_space, test_sleep_timer_per_space,
             test_legacy_connection_lands_in_primary]
    try:
        got = test_startup_refuses_missing_service_account()
    except Exception:  # noqa: BLE001
        got = [f"raised:\n{traceback.format_exc()}"]
    for f in got:
        print(f"FAIL - test_startup_refuses_missing_service_account: {f}")
    failures += got
    for t in tests:
        tmp = tempfile.mkdtemp(prefix="user-spaces-")
        try:
            got = t(tmp)
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        finally:
            hubmod.STATE_PATH, hubmod.ND_USER = saved_path, saved_user
            shutil.rmtree(tmp, ignore_errors=True)
        for f in got:
            print(f"FAIL - {t.__name__}: {f}")
        failures += got
    if failures:
        return 1
    print("PASS - user spaces: startup-refuses-missing-service-account(3 vars x pure+subprocess)/"
          "space-for-lazy-case-blind/per-user-machinery-in-space/"
          "station-engine-reads-its-space/sleep-timer-per-space/"
          "legacy-connection-lands-in-primary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
