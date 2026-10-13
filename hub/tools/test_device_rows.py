#!/usr/bin/env python3
"""
Tests for truthful device rows: `hello.device.kind` and `deviceState.volume`
(PROTOCOL §3.1, §3.2, §5.3).

A picker row said two things nobody had checked. What a device is was a guess
from its name (a 📺 a cast bridge put there), and its volume was whatever the hub
had last TOLD it, 100 from the start: turn the speaker down at the speaker and
every picker went on showing the old level.

  1. `kind`: a device says what it is in `hello`. The hub keeps a string of 1 to
     16 lowercase letters, stores it with the device (state file too) and relays
     it in every DeviceInfo, known value or not. Anything else is dropped, which
     reads as "not given"; a hello that gives none leaves the stored one alone.
  2. `volume`: `deviceState {volume}` is accepted from every device, not only from
     a bridge. The row takes the level (clamped to 0..100), `devices` goes out
     once when it changed and not at all when it did not, and nothing comes back
     to the device: a report is not a request. What a bridge's `deviceState` says
     about its hardware (`reachable`, `appRunning`) works as it did.
  3. `tools/fake_receiver.py --kind K --volume N` does both.

Spins up real hub subprocesses, each on a free port. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from fake_receiver import FakeReceiver
from test_cmd_echo import HUB_DIR, Hub, Peer, tracks, until
from test_edits import TOKEN


def rows(peer):
    """The newest DeviceInfo list this peer was sent, by device id."""
    for f in reversed(peer.frames):
        if f.get("t") in ("devices", "welcome"):
            return {d["id"]: d for d in f.get("devices", [])}
    return {}


def row(peer, device_id):
    return rows(peer).get(device_id) or {}


def n_devices(peer):
    return sum(1 for f in peer.frames if f.get("t") == "devices")


async def devices_caused(observer, do, settle=0.3):
    """Run `do()` and count the `devices` frames `observer` was sent because of it."""
    before = n_devices(observer)
    await do()
    await asyncio.sleep(settle)
    return n_devices(observer) - before


def read_state(hub):
    with open(hub.state, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 1. kind: relayed, checked, stored, and still there after a restart
# --------------------------------------------------------------------------- #
BAD_KINDS = ["TV", "Speaker", "smart speaker", "smart-speaker", "tv1", "", "x" * 17,
             "télé", 5, True, ["tv"], {"is": "tv"}, None]


async def group_kind(failures):
    async with Hub() as hub:
        ctl = Peer("Ctl", "ctl", ["controller"])
        await ctl.connect(hub.url)
        spk = Peer("Kitchen", "spk", ["receiver"], kind="speaker")
        await spk.connect(hub.url)

        # ----- relayed, to the device itself and to everyone else ------------ #
        if row(ctl, "spk").get("kind") != "speaker":
            failures.append("kind: a controller's `devices` frame should carry "
                            f"kind \"speaker\" for the row, got {row(ctl, 'spk')!r}")
        own = {d["id"]: d for d in spk.welcome.get("devices", [])}.get("spk", {})
        if own.get("kind") != "speaker":
            failures.append(f"kind: the device's own row in its `welcome` says "
                            f"{own.get('kind')!r}, not \"speaker\"")
        mine = row(ctl, "ctl")
        if "kind" not in mine or mine["kind"] is not None:
            failures.append("kind: a device that gave none has `kind: null` in its "
                            f"row (the key is always there), got {mine!r}")

        # ----- the shape: 1 to 16 lowercase letters, known value or not ------ #
        good = {"a": "a", "fridge": "fridge", "x" * 16: "x" * 16}
        for i, kind in enumerate(good):
            p = Peer(f"Good{i}", f"good{i}", ["controller"], kind=kind)
            await p.connect(hub.url, settle=0.15)
            await p.close()
        for i, kind in enumerate(BAD_KINDS):
            p = Peer(f"Bad{i}", f"bad{i}", ["controller"], kind=kind)
            await p.connect(hub.url, settle=0.15)
            if p.welcome is None:
                failures.append(f"kind: a hello with kind {kind!r} got no `welcome`")
            await p.close()
        await asyncio.sleep(0.3)
        seen = rows(ctl)
        for i, kind in enumerate(good):
            if seen.get(f"good{i}", {}).get("kind") != kind:
                failures.append(f"kind: {kind!r} has the shape and must be relayed as "
                                f"given, the row says {seen.get(f'good{i}', {}).get('kind')!r}")
        for i, kind in enumerate(BAD_KINDS):
            got = seen.get(f"bad{i}", {"kind": "<no row>"}).get("kind")
            if got is not None:
                failures.append(f"kind: {kind!r} is not 1 to 16 lowercase letters and "
                                f"must be dropped, the row says {got!r}")

        # ----- a hello that gives none, or a bad one, leaves the stored one -- #
        await spk.close()
        await asyncio.sleep(0.2)
        for label, device in (("no kind", {}), ("a bad kind", {"kind": "Speaker!"})):
            again = Peer("Kitchen", "spk", ["receiver"], **device)
            await again.connect(hub.url)
            if row(ctl, "spk").get("kind") != "speaker":
                failures.append(f"kind: a later hello with {label} must leave the "
                                f"stored kind alone, the row says "
                                f"{row(ctl, 'spk').get('kind')!r}")
            await again.close()
            await asyncio.sleep(0.2)
        # ... and one that gives another replaces it.
        tv = Peer("Kitchen", "spk", ["receiver"], kind="tv")
        await tv.connect(hub.url)
        if row(ctl, "spk").get("kind") != "tv":
            failures.append("kind: a later hello with another kind replaces the stored "
                            f"one, the row says {row(ctl, 'spk').get('kind')!r}")
        await tv.send(t="deviceState", volume=35)
        await asyncio.sleep(0.2)
        await tv.close()
        await ctl.close()
        await asyncio.sleep(0.3)

        # ----- stored: the state file, and a restart -------------------------- #
        await hub.stop()
        stored = {d["id"]: d for d in read_state(hub).get("devices", [])}
        if stored.get("spk", {}).get("kind") != "tv":
            failures.append(f"kind: the state file's row is {stored.get('spk')!r}, "
                            "expected kind \"tv\"")
        await hub.start()
        probe = Peer("Probe", "probe", ["controller"])
        await probe.connect(hub.url)
        back = row(probe, "spk")
        if (back.get("kind"), back.get("online"), back.get("volume")) != ("tv", False, 35):
            failures.append("kind: after a hub restart the offline row should still "
                            f"say kind tv, volume 35; got kind={back.get('kind')!r} "
                            f"online={back.get('online')!r} volume={back.get('volume')!r}")
        if row(probe, "good1").get("kind") != "fridge" or row(probe, "bad0").get("kind"):
            failures.append("kind: after a restart good1 should say \"fridge\" and bad0 "
                            f"nothing, got {row(probe, 'good1').get('kind')!r} / "
                            f"{row(probe, 'bad0').get('kind')!r}")
        await probe.close()
        await asyncio.sleep(0.2)

        # ----- a state file from before, or a damaged one --------------------- #
        await hub.stop()
        state = read_state(hub)
        for d in state["devices"]:
            if d["id"] == "spk":
                d["kind"] = "Not A Kind"
            elif d["id"] == "good1":
                del d["kind"]
        with open(hub.state, "w", encoding="utf-8") as f:
            json.dump(state, f)
        await hub.start()
        probe = Peer("Probe", "probe", ["controller"])
        await probe.connect(hub.url)
        got = rows(probe)
        if "spk" not in got or "good1" not in got or "good0" not in got:
            failures.append("kind: a state file whose rows lack `kind`, or hold a bad "
                            f"one, must still load every row; got {sorted(got)}")
        elif (got["spk"]["kind"], got["good1"]["kind"], got["good0"]["kind"]) != (None, None, "a"):
            failures.append("kind: from the state file, a bad kind and a missing one "
                            "both load as null and a good one as given; got "
                            f"{got['spk']['kind']!r} / {got['good1']['kind']!r} / "
                            f"{got['good0']['kind']!r}")
        await probe.close()


# --------------------------------------------------------------------------- #
# 2. volume: what the device says, from any device, broadcast on change only
# --------------------------------------------------------------------------- #
async def group_volume(failures):
    async with Hub() as hub:
        obs = Peer("Obs", "obs", ["controller"])
        a = Peer("A", "a", ["receiver", "controller"])
        c = Peer("Ctl", "ctl", ["controller"])
        for p in (obs, a, c):
            await p.connect(hub.url)

        every = rows(obs)
        if any(type(d.get("volume")) is not int for d in every.values()):
            failures.append("volume: every row carries an int `volume` (a client "
                            f"reads a null as 100): {[(i, d.get('volume')) for i, d in every.items()]}")

        # ----- a report from a device that is no bridge ----------------------- #
        n = await devices_caused(obs, lambda: a.send(t="deviceState", volume=35))
        if n != 1 or row(obs, "a").get("volume") != 35:
            failures.append("volume: a report of 35 from an ordinary device should "
                            f"set the row and broadcast `devices` once; got {n} "
                            f"frame(s), row volume {row(obs, 'a').get('volume')!r}")
        n = await devices_caused(obs, lambda: a.send(t="deviceState", volume=35))
        if n != 0:
            failures.append(f"volume: an identical report broadcast {n} `devices` "
                            "frame(s); only a change is broadcast")
        if row(a, "a").get("volume") != 35:
            failures.append("volume: the reporting device's own mirror should hold "
                            f"its row at 35, got {row(a, 'a').get('volume')!r}")

        # ----- clamped; what cannot be read changes nothing -------------------- #
        for sent, want in ((150, 100), (-5, 0), (64, 64)):
            n = await devices_caused(obs, lambda: a.send(t="deviceState", volume=sent))
            if n != 1 or row(obs, "a").get("volume") != want:
                failures.append(f"volume: a report of {sent} should leave the row at "
                                f"{want} with one broadcast; got {n} frame(s), "
                                f"volume {row(obs, 'a').get('volume')!r}")
        for junk in ('"loud"', "null", "true", "[50]", '{"level":5}', "NaN", "Infinity"):
            raw = '{"t":"deviceState","volume":%s}' % junk
            n = await devices_caused(obs, lambda: a.ws.send(raw), settle=0.15)
            if n != 0 or row(obs, "a").get("volume") != 64:
                failures.append(f"volume: a report of {junk} is not a level and must "
                                f"change nothing; got {n} frame(s), volume "
                                f"{row(obs, 'a').get('volume')!r}")

        # ----- from a device that cannot even play ----------------------------- #
        n = await devices_caused(obs, lambda: c.send(t="deviceState", volume=60))
        if n != 1 or row(obs, "ctl").get("volume") != 60:
            failures.append("volume: a report is accepted from ANY device, also a "
                            f"controller; got {n} frame(s), volume "
                            f"{row(obs, 'ctl').get('volume')!r}")

        # ----- told 37, says 36: the row says what the device says ------------ #
        await a.act(action="setQueue", tracks=tracks(3), index=0, play=True)
        await asyncio.sleep(0.3)
        await c.act(action="volume", level=37)
        await until(lambda: a.last_do("setVolume") is not None)
        if (a.last_do("setVolume") or {}).get("level") != 37 or row(obs, "a").get("volume") != 37:
            failures.append("volume: act:volume 37 should reach the active device and "
                            f"the row; do={a.last_do('setVolume')!r}, row volume "
                            f"{row(obs, 'a').get('volume')!r}")
        n = await devices_caused(obs, lambda: a.send(t="deviceState", volume=36))
        if n != 1 or row(obs, "a").get("volume") != 36:
            failures.append("volume: the device applied 37 as 36 and said so; the row "
                            f"should say 36 after one broadcast, got {n} frame(s), "
                            f"volume {row(obs, 'a').get('volume')!r}")

        # ----- a report is never answered -------------------------------------- #
        sent_back = [d for p in (a, c) for d in p.dos if d.get("cmd") == "setVolume"]
        if len(sent_back) != 1:
            failures.append("volume: the only do:setVolume in this whole case is the "
                            "one act:volume caused; a report must never be answered "
                            f"with one. Seen: {sent_back}")
        if a.errors or c.errors:
            failures.append(f"volume: reports produced `error` frames: "
                            f"{a.err_codes()} {c.err_codes()}")

        # ----- a bridge: its verdict on the hardware works as it did ---------- #
        tv = Peer("TV", "cast-tv", ["receiver", "loadAck"], bridgedBy="a", kind="tv")
        await tv.connect(hub.url)
        state = dict(t="deviceState", reachable=True, appRunning=True, volume=40)
        n = await devices_caused(obs, lambda: tv.send(**state))
        got = row(obs, "cast-tv")
        if n != 1 or (got.get("reachable"), got.get("appRunning"), got.get("volume")) != (True, True, 40):
            failures.append("bridge: one deviceState carrying reachable, appRunning and "
                            f"volume should give one `devices` frame with all three; "
                            f"got {n} frame(s), {got!r}")
        n = await devices_caused(obs, lambda: tv.send(**state))
        if n != 0:
            failures.append(f"bridge: its 30 s re-assert of the same state broadcast "
                            f"{n} `devices` frame(s)")
        n = await devices_caused(obs, lambda: tv.send(**{**state, "volume": 45}))
        if n != 1 or row(obs, "cast-tv").get("volume") != 45:
            failures.append("bridge: a re-assert in which only the volume moved should "
                            f"broadcast once; got {n} frame(s), volume "
                            f"{row(obs, 'cast-tv').get('volume')!r}")
        n = await devices_caused(obs, lambda: tv.send(t="deviceState", reachable=False))
        got = row(obs, "cast-tv")
        if n != 1 or got.get("reachable") is not False or got.get("volume") != 45:
            failures.append("bridge: reachable:false is still broadcast, and says "
                            f"nothing about the level; got {n} frame(s), {got!r}")
        n = await devices_caused(obs, lambda: tv.send(t="deviceState", volume=50))
        got = row(obs, "cast-tv")
        if n != 1 or got.get("reachable") is not False or got.get("volume") != 50:
            failures.append("bridge: a volume report says nothing about reachability; "
                            f"got {n} frame(s), {got!r}")

        # ----- an offline row keeps the level it last had ---------------------- #
        await a.close()
        await until(lambda: row(obs, "a").get("online") is False)
        if row(obs, "a").get("volume") != 36:
            failures.append("volume: the row of a device that went offline should keep "
                            f"its last level (36), got {row(obs, 'a').get('volume')!r}")
        for p in (obs, c, tv):
            await p.close()


# --------------------------------------------------------------------------- #
# 3. tools/fake_receiver.py --kind / --volume
# --------------------------------------------------------------------------- #
async def group_fake_receiver(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        c.auto_release = True                # it holds the slot until the transfer
        fake = FakeReceiver(hub.url, TOKEN, "Fake", "fake", kind="speaker", volume=35,
                            quiet=True)
        run = asyncio.create_task(fake.run())
        await until(lambda: row(c, "fake").get("volume") == 35, timeout=3.0)
        got = row(c, "fake")
        if (got.get("kind"), got.get("volume")) != ("speaker", 35):
            failures.append("fake_receiver: with kind=speaker volume=35 the row should "
                            f"say so once it has connected; got {got!r}")

        # Changed at the device: reported, and nothing comes back.
        await fake.set_volume(20)
        await until(lambda: row(c, "fake").get("volume") == 20)
        await asyncio.sleep(0.2)
        if row(c, "fake").get("volume") != 20 or fake.volume != 20:
            failures.append("fake_receiver: set_volume(20) should be reported and stay "
                            f"20; row {row(c, 'fake').get('volume')!r}, receiver "
                            f"{fake.volume}")

        # Told by the hub while it is the playing device: applied, and reported.
        await c.act(action="setQueue", tracks=tracks(3), index=0, play=True)
        await asyncio.sleep(0.2)
        await c.act(action="transfer", target="fake")
        await until(lambda: c.active == "fake" and fake.active, timeout=3.0)
        await c.act(action="volume", level=50)
        await until(lambda: fake.volume == 50)
        await asyncio.sleep(0.2)
        if fake.volume != 50 or row(c, "fake").get("volume") != 50:
            failures.append("fake_receiver: act:volume 50 on the playing fake should "
                            f"be applied and reported; receiver {fake.volume}, row "
                            f"{row(c, 'fake').get('volume')!r}")
        run.cancel()

        # The switches themselves.
        cli = subprocess.Popen(
            [sys.executable, os.path.join("tools", "fake_receiver.py"), "--url", hub.url,
             "--token", TOKEN, "--name", "Cli", "--id", "cli", "--kind", "tv",
             "--volume", "12"],
            cwd=HUB_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await until(lambda: row(c, "cli").get("volume") == 12, timeout=5.0)
            got = row(c, "cli")
            if (got.get("kind"), got.get("volume"), got.get("online")) != ("tv", 12, True):
                failures.append("fake_receiver --kind tv --volume 12: the row should "
                                f"say kind tv, volume 12, online; got {got!r}")
        finally:
            cli.terminate()
            await asyncio.to_thread(cli.wait, 5)
        await c.close()


async def main():
    failures = []
    for name, coro in (("kind", group_kind(failures)),
                       ("volume", group_volume(failures)),
                       ("fake receiver", group_fake_receiver(failures))):
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one group must not hide the others
            failures.append(f"[{name}] raised {type(e).__name__}: {e}")

    if failures:
        print("FAIL (device rows):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - device rows: kind-relayed/bad-kind-dropped/unknown-kind-passed-through/"
          "stored-kind-kept/kind-and-volume-across-a-restart/old-state-file-loads/"
          "volume-from-any-device/broadcast-on-change-only/clamped/unreadable-ignored/"
          "report-never-answered/bridge-verdict-unchanged/offline-row-keeps-level/"
          "fake_receiver---kind---volume")


if __name__ == "__main__":
    asyncio.run(main())
