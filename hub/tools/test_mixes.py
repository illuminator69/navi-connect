#!/usr/bin/env python3
"""
Tests for "Mixed for You" — the hub-stored, client-regenerated recipes.

One case per property the design rests on:

  1. `saveMix` with no id MINTS one, broadcasts to every device, and the recipe
     comes back with the fields a client needs to regenerate from
     (`kind`, `seedId`, `moodCharacter`, `count`).
  2. `saveMix` WITH an id updates in place rather than minting a second row, and
     keeps `createdAt` — a client re-saving sends the recipe, not its history.
  3. `renameMix` / `deleteMix` do what they say; an unknown `renameMix` answers
     an `unknown_mix` error rather than silently disagreeing with a client that
     has already applied the rename locally.
  4. `touchMix` bumps `lastPlayedAt` and NOT `updatedAt`. That asymmetry is the
     test's real subject: `updatedAt` is the sort and eviction key, so folding
     "played" into it would reorder the user's list under them on every play,
     and let a mix they listen to evict one they just made.
  5. A recipe with no name, or with a `kind` this hub has never heard of, is
     refused with `bad_mix` and not stored — these records are persisted and
     fanned out to devices that never saw the sender.
  6. The cap evicts by `updatedAt`, oldest first.
  7. Recipes survive a save/load round trip, and a state.json written BEFORE
     mixes existed still loads (the collection is additive in exactly the way
     savedQueues is: `data.get(key, default)`, no schema version, no migration).

Spins up a real hub subprocess; asserts on the authoritative frames.
Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile

from test_edits import TOKEN, Client

HUB_DIR = os.path.join(os.path.dirname(__file__), "..")


def mix(client, mid):
    return next((m for m in client.mixes if m["id"] == mid), None)


def spawn(port, state):
    env = {**os.environ, "HUB_TOKEN": TOKEN, "HUB_PORT": str(port),
           "HUB_MIRROR_PLAYQUEUE": "false", "HUB_STATE": state,
           "HUB_HOST": "127.0.0.1", "HUB_HEALTH_PORT": str(port + 1)}
    return subprocess.Popen([sys.executable, "hub.py"], env=env, cwd=HUB_DIR)


def stop(hub):
    hub.terminate()
    try:
        hub.wait(timeout=5)
    except subprocess.TimeoutExpired:
        hub.kill()


async def test_crud_and_touch():
    """Cases 1-5."""
    port, failures = 4812, []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://localhost:{port}"
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        a = Client("Phone", device_id="phone")
        b = Client("Desk", device_id="desk", caps=["controller"])
        await a.connect(url)
        await b.connect(url)

        # ----- 1. create mints an id and reaches the OTHER device ----------- #
        await a.act(action="saveMix", name="Late Night", kind="adaptive",
                    seedId="song-1", seedName="Nightcall",
                    moodCharacter="SteadyVibes", count=40)
        await asyncio.sleep(0.4)
        if len(b.mixes) != 1:
            failures.append(f"the create did not reach the other device: {b.mixes}")
            return failures
        rec = b.mixes[0]
        mid = rec["id"]
        if not mid.startswith("mx_"):
            failures.append(f"a minted mix id should be mx_<ms>_<hex>, got {mid!r}")
        for key, want in (("name", "Late Night"), ("kind", "adaptive"),
                          ("seedId", "song-1"), ("seedName", "Nightcall"),
                          ("moodCharacter", "SteadyVibes"), ("count", 40)):
            if rec.get(key) != want:
                failures.append(
                    f"{key} came back {rec.get(key)!r}, not {want!r} — the whole "
                    "point of a mix is that the RECIPE survives, so a client can "
                    "regenerate rather than replay")
        created = rec.get("createdAt")

        # ----- 2. update in place, keeping createdAt ----------------------- #
        await b.act(action="saveMix", id=mid, name="Late Night", kind="adaptive",
                    seedId="song-1", moodCharacter="SteadyVibes", count=80)
        await asyncio.sleep(0.4)
        if len(a.mixes) != 1:
            failures.append(f"a save with an id minted a second row: {a.mixes}")
        rec = mix(a, mid) or {}
        if rec.get("count") != 80:
            failures.append(f"the update did not apply: count={rec.get('count')}")
        if rec.get("createdAt") != created:
            failures.append(
                "createdAt moved on an edit — a client re-saving sends the "
                "recipe, not its history, and taking its word resets the age of "
                "a mix every time its count is changed")

        # ----- 4. touchMix bumps lastPlayedAt ONLY ------------------------- #
        updated_before = rec.get("updatedAt")
        await asyncio.sleep(0.05)
        await a.act(action="touchMix", id=mid)
        await asyncio.sleep(0.4)
        rec = mix(b, mid) or {}
        if not rec.get("lastPlayedAt"):
            failures.append("touchMix did not record lastPlayedAt")
        if rec.get("updatedAt") != updated_before:
            failures.append(
                "touchMix moved updatedAt — that is the sort and eviction key, "
                "so playing a mix would reorder the list under the user and let "
                "a played mix evict one they just made")

        # ----- 3. rename, and an unknown rename is an ERROR ---------------- #
        await b.act(action="renameMix", id=mid, name="  Very Late Night  ")
        await asyncio.sleep(0.4)
        if (mix(a, mid) or {}).get("name") != "Very Late Night":
            failures.append(
                f"rename did not apply/trim: {(mix(a, mid) or {}).get('name')!r}")
        b.errors.clear()
        await b.act(action="renameMix", id="mx_nope", name="X")
        await asyncio.sleep(0.4)
        if not any(e.get("code") == "unknown_mix" for e in b.errors):
            failures.append(
                "renaming an unknown mix was silent — the client has already "
                "applied the rename locally, so silence leaves the two "
                "permanently disagreeing with nothing to notice it")

        # ----- 5. malformed recipes are refused, not stored ---------------- #
        b.errors.clear()
        await b.act(action="saveMix", name="", kind="similar")
        await b.act(action="saveMix", name="Nameless Kind", kind="telepathy")
        await asyncio.sleep(0.4)
        if len(a.mixes) != 1:
            failures.append(f"a malformed recipe was stored: {a.mixes}")
        if len([e for e in b.errors if e.get("code") == "bad_mix"]) != 2:
            failures.append(
                f"a refused recipe must answer bad_mix, got {b.errors} — these "
                "records are persisted and fanned out to devices that never saw "
                "the sender")

        # ----- 3b. delete ------------------------------------------------- #
        await a.act(action="deleteMix", id=mid)
        await asyncio.sleep(0.4)
        if b.mixes:
            failures.append(f"deleteMix left the recipe behind: {b.mixes}")
    finally:
        stop(hub)
        os.unlink(state)
    return failures


async def test_cap_and_persistence():
    """Cases 6-7."""
    import hub as hubmod  # for MIXES_MAX, rather than hard-coding 30 here
    port, failures = 4814, []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://localhost:{port}"
    cap = hubmod.MIXES_MAX
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await c.connect(url)

        # ----- 6. the cap evicts the oldest updatedAt ---------------------- #
        for i in range(cap + 3):
            await c.act(action="saveMix", name=f"Mix {i}", kind="genre",
                        seedId=f"g{i}", count=25)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.8)
        if len(c.mixes) != cap:
            failures.append(f"the cap did not hold: {len(c.mixes)} != {cap}")
        names = {m["name"] for m in c.mixes}
        if "Mix 0" in names:
            failures.append(
                "eviction kept the OLDEST recipe — the cap drops by updatedAt")
        if f"Mix {cap + 2}" not in names:
            failures.append("eviction dropped the newest recipe")

        # ----- 7. ...and they survive a restart ---------------------------- #
        await c.act(action="saveMix", name="Survivor", kind="artist",
                    seedId="artist-1", seedName="Band", count=60)
        await asyncio.sleep(0.6)
    finally:
        stop(hub)

    with open(state, encoding="utf-8") as f:
        parsed = json.load(f)
    if "mixes" not in parsed:
        failures.append("state.json carries no `mixes` key")

    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        c2 = Client("Ctl2", device_id="ctl2", caps=["controller"])
        await c2.connect(url)
        if len(c2.mixes) != cap:
            failures.append(
                f"a reload lost recipes: {len(c2.mixes)} != {cap}")
        survivor = next((m for m in c2.mixes if m["name"] == "Survivor"), None)
        if survivor is None:
            failures.append("the recipe saved last did not survive the reload")
        elif survivor.get("seedId") != "artist-1" or survivor.get("count") != 60:
            failures.append(f"the reloaded recipe lost fields: {survivor}")
        if not c2.mixes:
            failures.append("`mixes` was not embedded in `welcome`")
    finally:
        stop(hub)

    # ----- 7b. a state.json from BEFORE mixes existed still loads ---------- #
    # The collection is additive in exactly the way savedQueues is: there is no
    # schema version and no migration path in hub.py, only `data.get(key,
    # default)`. This is the half of that claim a reader cannot check by eye.
    legacy = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump({"session": {"rev": 3, "queue": [], "index": 0},
                   "savedQueues": [], "devices": []}, f)
    hub = spawn(port, legacy)
    try:
        await asyncio.sleep(1.2)
        c3 = Client("Ctl3", device_id="ctl3", caps=["controller"])
        await c3.connect(url)
        if c3.mixes != []:
            failures.append(
                f"a pre-mixes state.json did not load cleanly: {c3.mixes}")
        await c3.act(action="saveMix", name="After", kind="similar", seedId="s1")
        await asyncio.sleep(0.5)
        if len(c3.mixes) != 1:
            failures.append("a mix could not be added to a pre-mixes state file")
    finally:
        stop(hub)
        os.unlink(legacy)
        os.unlink(state)
    return failures


async def main():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    failures = []
    failures += await test_crud_and_touch()
    failures += await test_cap_and_persistence()
    if failures:
        print("FAIL (mixes):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - mixes: create-mints-and-broadcasts/update-keeps-createdAt/"
          "rename-trims/unknown-rename-errors/bad-recipe-refused/delete/"
          "touch-moves-lastPlayedAt-only/cap-evicts-oldest/survives-reload/"
          "pre-mixes-state-loads")


if __name__ == "__main__":
    asyncio.run(main())
