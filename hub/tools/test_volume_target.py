#!/usr/bin/env python3
"""
Tests for `act:volume` with a `target` (PROTOCOL §5.1): the volume of a device
that is not the playing one.

`volume` could only reach the active device, so a speaker could not be turned
down BEFORE music was sent to it: the first seconds of every transfer played at
whatever level it had been left at.

  1. A `target` naming a device other than the active one sends `do:setVolume` to
     THAT device and to nobody else, records the level on its row and broadcasts
     `devices`, whether or not anything is playing. The ack follows the broadcast.
  2. It is refused, with nothing recorded and nothing sent, as `target_offline`
     (no such device, or no socket), `target_unreachable` (a bridge says the
     hardware is gone) or `not_supported` (the device did not say "idleVolume").
     With a `cid` the refusal is in the ack and nowhere else; without one it is an
     `error` frame to the sender alone.
  3. No `target`, or the active device's id: the path `volume` always took. The
     active device needs no cap, and with none active it is `no_active_device`.
  4. A `target` that is not a string is `bad_request`.
  5. `do:setVolume` carries no `seq` and takes no number from the counter, so the
     command echo of the playing device and of the target is as it was.
  6. It is not held by a transfer.
  7. `tools/fake_receiver.py --idle-volume` and `tools/controller.py`'s
     `volume N --target I`.

Spins up real hub subprocesses, each on a free port. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from fake_receiver import FakeReceiver
from test_act_ack import ack_of, ask, check, mint
from test_cmd_echo import ECHO, HUB_DIR, Hub, Peer, tracks, until
from test_device_rows import n_devices, row, rows
from test_edits import TOKEN

IDLE = ECHO + ["idleVolume"]


def volumes(peer, since=0):
    """The levels of the `do:setVolume` frames a peer was sent, from `since` on."""
    return [d.get("level") for d in peer.dos[since:] if d.get("cmd") == "setVolume"]


class Cli:
    """tools/controller.py as a subprocess: lines in, everything it printed out."""

    def __init__(self, hub):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join("tools", "controller.py"),
             "--url", hub.url, "--token", TOKEN],
            cwd=HUB_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)

    async def type(self, line, settle=0.5):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        await asyncio.sleep(settle)

    async def quit(self):
        try:
            out, _ = await asyncio.to_thread(self.proc.communicate, "quit\n", 5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = await asyncio.to_thread(self.proc.communicate)
        return out


# --------------------------------------------------------------------------- #
# 1-4. where it goes, what refuses it, and the path it always took
# --------------------------------------------------------------------------- #
async def group_target(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        obs = Peer("Obs", "obs", ["controller"])
        a = Peer("A", "a", IDLE)
        b = Peer("B", "b", IDLE)
        old = Peer("Old", "old", ["receiver", "controller"])
        tv = Peer("TV", "cast-tv", ["receiver", "loadAck", "idleVolume"], bridgedBy="a")
        gone = Peer("Gone", "gone", IDLE)
        everyone = (c, obs, a, b, old, tv)
        for p in everyone + (gone,):
            await p.connect(hub.url)
        a.auto_echo = a.auto_release = True
        await gone.close()
        await tv.send(t="deviceState", reachable=False)
        await asyncio.sleep(0.3)

        if "volumeTarget" not in ((c.welcome or {}).get("features") or []):
            failures.append("welcome.features lacks \"volumeTarget\": "
                            f"{(c.welcome or {}).get('features')!r}")

        def errors():
            return {p.name: p.err_codes() for p in everyone if p.errors}

        def others_told(target):
            return {p.name: volumes(p) for p in everyone
                    if p is not target and volumes(p)}

        # ----- with nothing playing at all ------------------------------------ #
        start = len(c.frames)
        ack = await ask(c, action="volume", level=30, target="b")
        await asyncio.sleep(0.2)
        if check(failures, "target b, nothing active", ack, True):
            frames = c.frames[start:]
            at = next(i for i, f in enumerate(frames) if f is ack)
            if "devices" not in [f.get("t") for f in frames[:at]]:
                failures.append("target b, nothing active: the ack must follow the "
                                "`devices` frame the act caused; ahead of it the "
                                f"sender got {[f.get('t') for f in frames[:at]]}")
        if volumes(b) != [30] or others_told(b):
            failures.append("target b, nothing active: do:setVolume 30 goes to B and "
                            f"only to B; B got {volumes(b)}, others {others_told(b)}")
        if row(obs, "b").get("volume") != 30:
            failures.append("target b, nothing active: every client's row for B "
                            f"should say 30, got {row(obs, 'b').get('volume')!r}")
        check(failures, "no target, nothing active",
              await ask(c, action="volume", level=10), False, "no_active_device")

        # ----- while another device plays -------------------------------------- #
        await a.act(action="setQueue", tracks=tracks(4), index=0, play=True)
        await asyncio.sleep(0.3)
        a.model.update(index=0, positionMs=0, isPlaying=True)
        nb = len(b.dos)
        check(failures, "target b, A playing",
              await ask(c, action="volume", level=45, target="b"), True)
        await asyncio.sleep(0.2)
        if volumes(b, nb) != [45] or others_told(b):
            failures.append("target b, A playing: only B is told; B got "
                            f"{volumes(b, nb)}, others {others_told(b)}")
        if (row(obs, "b").get("volume"), row(obs, "a").get("volume")) != (45, 100):
            failures.append("target b, A playing: B's row 45, A's untouched at 100; "
                            f"got {row(obs, 'b').get('volume')!r} / "
                            f"{row(obs, 'a').get('volume')!r}")
        for sent, want in ((250, 100), (-3, 0)):
            nb = len(b.dos)
            await ask(c, action="volume", level=sent, target="b")
            await until(lambda: volumes(b, nb) == [want]
                        and row(obs, "b").get("volume") == want)
            if volumes(b, nb) != [want] or row(obs, "b").get("volume") != want:
                failures.append(f"target b: level {sent} is clamped to {want}; B got "
                                f"{volumes(b, nb)}, row {row(obs, 'b').get('volume')!r}")
        # The target says what it made of it, and the row follows (a phone's steps).
        await ask(c, action="volume", level=45, target="b")
        await b.send(t="deviceState", volume=44)
        await until(lambda: row(obs, "b").get("volume") == 44)
        if row(obs, "b").get("volume") != 44:
            failures.append("target b: told 45, it reported 44, and the row should "
                            f"say 44; got {row(obs, 'b').get('volume')!r}")

        # ----- no target, or the active device: as it always was -------------- #
        na, nb = len(a.dos), len(b.dos)
        check(failures, "no target", await ask(c, action="volume", level=60), True)
        check(failures, "target = the active device",
              await ask(c, action="volume", level=61, target="a"), True)
        await c.send(t="act", action="volume", level=62, target=None)   # JSON null
        await until(lambda: volumes(a, na) == [60, 61, 62]
                    and row(obs, "a").get("volume") == 62)
        if volumes(a, na) != [60, 61, 62] or volumes(b, nb):
            failures.append("no target, the active device's id and a null target all "
                            f"mean the active device: A got {volumes(a, na)}, B got "
                            f"{volumes(b, nb)}")
        if row(obs, "a").get("volume") != 62:
            failures.append(f"no target: A's row should say 62, got "
                            f"{row(obs, 'a').get('volume')!r}")

        # ----- refused, with a cid: the ack and nothing else ------------------ #
        before = {i: d.get("volume") for i, d in rows(obs).items()}
        n_frames = n_devices(obs)
        told = {p.name: len(p.dos) for p in everyone}
        refusals = (("ghost", "target_offline", "a device nobody has heard of"),
                    ("gone", "target_offline", "a known device with no socket"),
                    ("cast-tv", "target_unreachable", "a speaker its bridge cannot reach"),
                    ("old", "not_supported", "a device without \"idleVolume\""))
        for target, code, what in refusals:
            check(failures, f"target {target} ({what})",
                  await ask(c, action="volume", level=5, target=target), False, code)
        await asyncio.sleep(0.3)
        after = {i: d.get("volume") for i, d in rows(obs).items()}
        if after != before or n_devices(obs) != n_frames:
            failures.append("a refused volume must change nothing and broadcast "
                            f"nothing: rows {before} -> {after}, "
                            f"{n_devices(obs) - n_frames} `devices` frame(s)")
        if {p.name: len(p.dos) for p in everyone} != told:
            failures.append("a refused volume sent a directive to somebody: "
                            f"{ {p.name: p.dos[told[p.name]:] for p in everyone if len(p.dos) != told[p.name]} }")
        if errors():
            failures.append("with a `cid` a refusal is in the ack ONLY, but `error` "
                            f"frames were sent: {errors()}")

        # ----- refused, without a cid: an `error` to the sender alone --------- #
        n = len(c.acks)
        for target, _, _ in refusals:
            await c.act(action="volume", level=5, target=target)
        await asyncio.sleep(0.5)
        want = [code for _, code, _ in refusals]
        if c.err_codes() != want:
            failures.append(f"without a cid the sender should get {want} as `error` "
                            f"frames, got {c.err_codes()}")
        elsewhere = {name: codes for name, codes in errors().items() if name != "Ctl"}
        if elsewhere:
            failures.append("without a cid a refused volume is reported to the sender "
                            f"alone, but these also got `error` frames: {elsewhere}")
        if len(c.acks) != n:
            failures.append(f"refusals without `cid` were acked: {c.acks[n:]}")
        if {i: d.get("volume") for i, d in rows(obs).items()} != before:
            failures.append("a volume refused without a cid changed a row")
        c.errors.clear()

        # ----- a target that is not a device id ------------------------------- #
        na, nb = len(a.dos), len(b.dos)
        for bad in (5, ["b"], {"id": "b"}, True):
            check(failures, f"target {bad!r}",
                  await ask(c, action="volume", level=5, target=bad), False, "bad_request")
        await asyncio.sleep(0.2)
        if volumes(a, na) or volumes(b, nb) or n_devices(obs) != n_frames:
            failures.append("a volume whose target is not a string must change "
                            f"nothing: A got {volumes(a, na)}, B got {volumes(b, nb)}, "
                            f"{n_devices(obs) - n_frames} `devices` frame(s)")
        for p in everyone:
            await p.close()

    # The active device is told with or without the cap: naming it changes nothing.
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        old = Peer("Old", "old", ["receiver", "controller"])
        for p in (c, old):
            await p.connect(hub.url)
        await old.act(action="setQueue", tracks=tracks(2), index=0, play=True)
        await asyncio.sleep(0.3)
        check(failures, "target = an active device without the cap",
              await ask(c, action="volume", level=33, target="old"), True)
        await asyncio.sleep(0.2)
        if volumes(old) != [33]:
            failures.append("the active device is sent do:setVolume whether or not it "
                            f"has \"idleVolume\"; it got {volumes(old)}")
        for p in (c, old):
            await p.close()


# --------------------------------------------------------------------------- #
# 5. the command echo is left alone
# --------------------------------------------------------------------------- #
async def group_echo(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        a = Peer("A", "a", IDLE)
        b = Peer("B", "b", IDLE)
        for p in (c, a, b):
            await p.connect(hub.url)
        a.auto_echo = True
        await a.act(action="setQueue", tracks=tracks(4), index=0, play=True)
        await asyncio.sleep(0.3)
        a.model.update(index=0, positionMs=0, isPlaying=True)

        # ----- it takes no number --------------------------------------------- #
        await ask(c, action="pause")
        first = (a.last_do("pause") or {}).get("seq")
        for level in (20, 21, 22):
            await ask(c, action="volume", level=level, target="b")
        await ask(c, action="volume", level=70)              # the active device's own
        await ask(c, action="play")
        second = (a.last_do("play") or {}).get("seq")
        if type(first) is not int or second != first + 1:
            failures.append("do:setVolume must take no number from the echo's "
                            f"counter: the pause was seq {first!r}, four volumes "
                            f"later the play is seq {second!r} (expected {first!r} + 1)")
        stamped = [d for p in (a, b) for d in p.dos
                   if d.get("cmd") == "setVolume" and "seq" in d]
        if stamped or volumes(b) != [20, 21, 22] or volumes(a) != [70]:
            failures.append("do:setVolume never carries a `seq`, to the target or to "
                            f"the active device: stamped {stamped}; B got {volumes(b)}, "
                            f"A got {volumes(a)}")

        # ----- the playing device is still believed at once ------------------- #
        a.auto_echo = False
        await ask(c, action="volume", level=23, target="b")
        await ask(c, action="volume", level=71)
        await a.report(isPlaying=False)          # its own pause, with the play's number
        await until(lambda: c.is_playing is False)
        if c.is_playing is not False:
            failures.append("after a volume for B and one of its own, a report from "
                            "the playing device carrying the number of the last "
                            "NUMBERED directive it was sent must still be believed at "
                            f"once; the session says isPlaying={c.is_playing}")

        # ----- nor is the target distrusted when it starts playing ------------ #
        # B has never been sent a numbered directive, only volumes. It publishes
        # its own queue and reports with applied 0: the truth, and believed.
        await c.act(action="clear")
        await a.close()
        await until(lambda: c.active is None)
        await b.act(action="setQueue", tracks=tracks(4), index=0, play=True)
        await until(lambda: c.active == "b")
        await b.report(index=2, positionMs=1000, isPlaying=True, applied=0)
        await until(lambda: c.index == 2)
        if (c.active, c.index) != ("b", 2):
            failures.append("a device that was only ever sent volumes must be "
                            "believed when it starts playing and reports applied 0; "
                            f"the session says active={c.active} index={c.index}")
        h = await hub.health()
        if h.get("staleReports") != 0 or h.get("echoBackstops") != 0:
            failures.append("nothing in this case is a stale report, but /health says "
                            f"staleReports={h.get('staleReports')!r} "
                            f"echoBackstops={h.get('echoBackstops')!r}")
        for p in (c, b):
            await p.close()


# --------------------------------------------------------------------------- #
# 6. a transfer does not hold it
# --------------------------------------------------------------------------- #
async def group_not_held(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        a = Peer("A", "a", IDLE)
        b = Peer("B", "b", IDLE)
        d = Peer("D", "d", IDLE)
        for p in (c, a, b, d):
            await p.connect(hub.url)
        b.auto_echo = True

        async def slow_release(msg):
            if msg.get("cmd") == "release":
                await asyncio.sleep(0.8)
                await a.send(t="released", transferId=msg.get("transferId"),
                             index=0, positionMs=5000)
        a.on_do = slow_release

        async def loaded(msg):
            if msg.get("cmd") == "load" and msg.get("transferId"):
                await b.send(t="loaded", ok=True, transferId=msg["transferId"])
        b.on_do = loaded
        await a.act(action="setQueue", tracks=tracks(3), index=0, play=True)
        await asyncio.sleep(0.3)

        start = len(c.frames)
        t_cid, v_cid, w_cid = mint(c), mint(c), mint(c)
        await c.act(action="transfer", target="b", cid=t_cid)
        await asyncio.sleep(0.1)
        await c.act(action="volume", level=15, target="d", cid=v_cid)
        await c.act(action="volume", level=16, target="b", cid=w_cid)   # the new device
        await until(lambda: all(ack_of(c, x) for x in (t_cid, v_cid, w_cid)), timeout=4.0)
        await asyncio.sleep(0.3)
        frames = c.frames[start:]
        commit = next((i for i, f in enumerate(frames)
                       if f.get("t") == "session" and f.get("activeDeviceId") == "b"), None)
        for name, cid in (("D", v_cid), ("B, the transfer's target", w_cid)):
            ack = ack_of(c, cid)
            if not check(failures, f"a volume for {name} during a transfer", ack, True):
                continue
            at = next(i for i, f in enumerate(frames) if f is ack)
            if commit is None or at > commit:
                failures.append(f"a volume for {name} sent during a transfer's release "
                                "wait was held until the commit; it is not one of "
                                "the acts a transfer holds")
        if volumes(d) != [15] or volumes(b) != [16]:
            failures.append(f"during a transfer: D got {volumes(d)}, B got {volumes(b)}; "
                            "expected [15] and [16]")
        order = [x.get("cmd") for x in b.dos if x.get("cmd") in ("setVolume", "load")]
        if order != ["setVolume", "load"]:
            failures.append("the point of it: the new device is turned down BEFORE "
                            f"its load arrives; B got {order}")
        if c.active != "b":
            failures.append(f"setup: the transfer did not commit (active={c.active})")
        for p in (c, a, b, d):
            await p.close()


# --------------------------------------------------------------------------- #
# 7. the two tools
# --------------------------------------------------------------------------- #
async def group_tools(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        plain = FakeReceiver(hub.url, TOKEN, "Plain", "plain", volume=80, quiet=True)
        idle = FakeReceiver(hub.url, TOKEN, "Idle", "idle", volume=80, idle_volume=True,
                            quiet=True)
        runs = [asyncio.create_task(f.run()) for f in (plain, idle)]
        await until(lambda: row(c, "idle").get("volume") == 80
                    and row(c, "plain").get("volume") == 80, timeout=3.0)

        if "idleVolume" not in (row(c, "idle").get("caps") or []) \
                or "idleVolume" in (row(c, "plain").get("caps") or []):
            failures.append("fake_receiver: idle_volume adds the cap \"idleVolume\" and "
                            f"nothing else does: {row(c, 'idle').get('caps')} / "
                            f"{row(c, 'plain').get('caps')}")
        check(failures, "fake_receiver with idle_volume",
              await ask(c, action="volume", level=25, target="idle"), True)
        check(failures, "fake_receiver without it",
              await ask(c, action="volume", level=25, target="plain"), False, "not_supported")
        await until(lambda: idle.volume == 25)
        await asyncio.sleep(0.2)
        if (idle.volume, plain.volume) != (25, 80):
            failures.append("fake_receiver: with idle_volume a do:setVolume is applied "
                            f"while idle; levels are {idle.volume} / {plain.volume}, "
                            "expected 25 / 80")
        if (row(c, "idle").get("volume"), row(c, "plain").get("volume")) != (25, 80):
            failures.append("fake_receiver: rows should say 25 / 80, got "
                            f"{row(c, 'idle').get('volume')!r} / "
                            f"{row(c, 'plain').get('volume')!r}")

        # ----- the switch, and controller.py's `volume N --target I` ---------- #
        fake = subprocess.Popen(
            [sys.executable, os.path.join("tools", "fake_receiver.py"), "--url", hub.url,
             "--token", TOKEN, "--name", "Sw", "--id", "sw", "--idle-volume"],
            cwd=HUB_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await until(lambda: row(c, "sw").get("online") is True, timeout=5.0)
            if "idleVolume" not in (row(c, "sw").get("caps") or []):
                failures.append("fake_receiver --idle-volume: the row's caps are "
                                f"{row(c, 'sw').get('caps')}")
            cli = Cli(hub)
            await until(lambda: row(c, "cli-controller").get("online") is True, timeout=5.0)
            ids = list(rows(c))
            await cli.type(f"volume 41 --target {ids.index('sw')}")
            await cli.type(f"volume 42 --target {ids.index('plain')}")
            await cli.type("volume 43 --target 99")
            out = await cli.quit()
            if row(c, "sw").get("volume") != 41:
                failures.append("controller.py `volume 41 --target <Sw>`: the row "
                                f"says {row(c, 'sw').get('volume')!r}")
            if "ack c1 ok" not in out or "ack c2 refused: not_supported" not in out \
                    or "bad device index" not in out:
                failures.append("controller.py should print the ack of the first, the "
                                "refusal of the second and a hint for the third; it "
                                f"printed:\n{out}")
        finally:
            fake.terminate()
            await asyncio.to_thread(fake.wait, 5)
        for r in runs:
            r.cancel()
        await c.close()


async def main():
    failures = []
    for name, coro in (("target", group_target(failures)),
                       ("echo", group_echo(failures)),
                       ("not held", group_not_held(failures)),
                       ("tools", group_tools(failures))):
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one group must not hide the others
            failures.append(f"[{name}] raised {type(e).__name__}: {e}")

    if failures:
        print("FAIL (volume target):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - volume target: only-the-target-is-told/with-or-without-an-active-"
          "device/ack-after-devices/clamped/target-offline-unreachable-not_supported-"
          "with-and-without-cid/refusal-changes-nothing/no-target-and-active-id-as-"
          "before/bad-target-bad_request/no-seq-and-no-number-taken/echo-of-both-"
          "devices-untouched/not-held-by-a-transfer/fake_receiver---idle-volume/"
          "controller-volume---target")


if __name__ == "__main__":
    asyncio.run(main())
