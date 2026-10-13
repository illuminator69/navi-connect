#!/usr/bin/env python3
"""
Tests for the session's sleep timer (`act:sleepTimer`, `session.sleepTimer`,
PROTOCOL §4.2, §5.1).

Each client had a sleep timer of its own and it did not cross devices: set on the
phone while the desktop played, it counted down on the phone alone and stopped
nothing. There is now one, held by the hub.

  1. `act:sleepTimer {minutes}`: 1..1440 sets it and replaces a running one, 0
     clears it, anything else is `bad_request` and changes nothing. Setting and
     clearing bump `rev` and broadcast `session`; `session.sleepTimer` is null or
     `{remainingMs, minutes, setBy}`, with `remainingMs` computed for each frame
     (`welcome.session` too). Nothing is broadcast while it counts down, and it
     survives a pause, a queue change, a clear and a transfer.
  2. When it runs out the hub pauses the playing device with a `do:pause`, and the
     one `session` frame that says "paused" also says the timer is gone. With
     nothing playing it sends no directive and only says the timer is gone. A timer
     that was cleared does not fire, one that was replaced fires at its new end.
  3. Its pause is a `pause` act, so a transfer holds it: running out while the hub
     waits for the old device to release, it is not sent to that device and not
     undone by the commit. The new device loads paused, also when the transfer
     asked for `play:true` over a paused session, or the pause runs right behind
     the load if a skip arrived first.
  4. It survives a hub restart: the state file holds the wall-clock end. Still in
     the future, it runs out when it would have; passed while the hub was down, it
     is dropped and pauses nothing.

A "minute" lasts HUB_SLEEP_MINUTE_S seconds (60 unless set), which is how the cases
that wait for the end run in seconds.

Spins up real hub subprocesses, each on a free port. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time

from test_act_ack import ack_of, ask, check, mint
from test_cmd_echo import ECHO, Hub, Peer, tracks, until
from test_device_rows import row
from test_volume_target import Cli


def session_of(peer):
    """The newest session snapshot a peer was sent, in `welcome` or a `session`."""
    for f in reversed(peer.frames):
        if f.get("t") == "session":
            return f
        if f.get("t") == "welcome":
            return f.get("session", {})
    return {}


def timer_of(peer):
    return session_of(peer).get("sleepTimer")


def cmds(peer, since=0):
    return [d.get("cmd") for d in peer.dos[since:]]


def answer_loads(peer):
    """Answer a transfer's `do:load` with `loaded`, as a `loadAck` receiver must."""
    async def on_do(msg):
        if msg.get("cmd") == "load" and msg.get("transferId"):
            await peer.send(t="loaded", ok=True, transferId=msg["transferId"])
    peer.on_do = on_do


def slow_release(peer, delay):
    """Answer `do:release` after `delay`: the window a timer can run out in."""
    async def on_do(msg):
        if msg.get("cmd") == "release":
            await asyncio.sleep(delay)
            peer.model["isPlaying"] = False
            await peer.report()
            await peer.send(t="released", transferId=msg.get("transferId"),
                            index=peer.model["index"], positionMs=peer.model["positionMs"])
        elif msg.get("cmd") == "load" and msg.get("transferId"):
            await peer.send(t="loaded", ok=True, transferId=msg["transferId"])
    peer.auto_release = False
    peer.on_do = on_do


def expect_timer(failures, label, timer, minutes, set_by, minute_ms=60_000, slack=2500):
    """`timer` is a set timer with (nearly) all of `minutes` left."""
    if not isinstance(timer, dict):
        failures.append(f"{label}: session.sleepTimer is {timer!r}, expected a timer")
        return
    full = minutes * minute_ms
    left = timer.get("remainingMs")
    if set(timer) != {"remainingMs", "minutes", "setBy"} or timer.get("minutes") != minutes \
            or timer.get("setBy") != set_by or type(left) is not int \
            or not full - slack < left <= full:
        failures.append(f"{label}: expected {{remainingMs: just under {full}, minutes: "
                        f"{minutes}, setBy: {set_by!r}}}, got {timer!r}")


# --------------------------------------------------------------------------- #
# 1. the act and the frames
# --------------------------------------------------------------------------- #
async def group_frames(failures):
    async with Hub() as hub:
        r = Peer("Recv", "recv", ECHO)
        b = Peer("B", "b", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        c2 = Peer("Ctl2", "ctl2", ["controller"])
        everyone = (r, b, c, c2)
        for p in everyone:
            await p.connect(hub.url)
        for p in (r, b):
            p.auto_echo = p.auto_release = True
            answer_loads(p)

        if "sleepTimer" not in ((c.welcome or {}).get("features") or []):
            failures.append("welcome.features lacks \"sleepTimer\": "
                            f"{(c.welcome or {}).get('features')!r}")
        first = (c.welcome or {}).get("session", {})
        if "sleepTimer" not in first or first["sleepTimer"] is not None:
            failures.append("with no timer set, welcome.session carries `sleepTimer: "
                            f"null`; got {first.get('sleepTimer', '<no key>')!r}")

        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.3)
        r.model.update(index=0, positionMs=0, isPlaying=True)

        # ----- set ------------------------------------------------------------ #
        rev = c.rev
        seen = {p.name: len(p.sessions) for p in everyone}
        start = len(c.frames)
        ack = await ask(c, action="sleepTimer", minutes=30)
        await asyncio.sleep(0.2)
        for p in everyone:
            new = p.sessions[seen[p.name]:]
            if len(new) != 1:
                failures.append(f"set: {p.name} should get one `session` frame, got "
                                f"{len(new)}")
                continue
            expect_timer(failures, f"set ({p.name})", new[0].get("sleepTimer"), 30, "ctl")
            if new[0].get("rev") != rev + 1:
                failures.append(f"set: the frame's rev is {new[0].get('rev')}, expected "
                                f"{rev + 1} (one bump)")
        if check(failures, "set", ack, True):
            frames = c.frames[start:]
            at = next(i for i, f in enumerate(frames) if f is ack)
            if "session" not in [f.get("t") for f in frames[:at]] or ack["rev"] != rev + 1:
                failures.append("set: the ack follows the `session` frame and names its "
                                f"rev ({rev + 1}); ahead of it came "
                                f"{[f.get('t') for f in frames[:at]]}, ack.rev={ack['rev']}")
        s = session_of(c)
        if (s.get("isPlaying"), s.get("activeDeviceId"), len(s.get("queue", []))) != (True, "recv", 5):
            failures.append("set: nothing else in the session changes; got isPlaying="
                            f"{s.get('isPlaying')} active={s.get('activeDeviceId')} "
                            f"queue={len(s.get('queue', []))}")
        if r.dos:
            failures.append(f"set: setting a timer sent the receiver {cmds(r)}")
        set_left = (timer_of(c) or {}).get("remainingMs", 0)

        # ----- it counts down in silence, and each frame says what is left ---- #
        n = len(c2.sessions)
        await asyncio.sleep(1.2)
        if len(c2.sessions) != n:
            failures.append("nothing is broadcast while the timer counts down, but "
                            f"{len(c2.sessions) - n} `session` frame(s) arrived in 1.2 s")
        late = Peer("Late", "late", ["controller"])
        await late.connect(hub.url)
        joined = (late.welcome or {}).get("session", {}).get("sleepTimer") or {}
        if joined.get("minutes") != 30 or joined.get("setBy") != "ctl" \
                or not 1000 <= set_left - joined.get("remainingMs", 0) <= 2500:
            failures.append("a device that connects mid-way gets the timer in "
                            "welcome.session with what is left NOW (about 1.2 s less "
                            f"than the {set_left} at the set): got {joined!r}")
        await late.close()
        await c.act(action="repeat", mode="all")
        await asyncio.sleep(0.3)
        again = (timer_of(c2) or {}).get("remainingMs", 0)
        if not 1200 <= set_left - again <= 3000:
            failures.append("remainingMs is computed when a frame is built: a later "
                            f"`session` frame should say about 1.7 s less than {set_left}, "
                            f"it says {again}")

        # ----- replaced, by another device, without a cid ---------------------- #
        rev, n = c.rev, len(c.sessions)
        await c2.act(action="sleepTimer", minutes=10)
        await asyncio.sleep(0.3)
        if len(c.sessions) != n + 1 or c.rev != rev + 1:
            failures.append(f"replace: one `session` frame and one bump; got "
                            f"{len(c.sessions) - n} frame(s), rev {rev} -> {c.rev}")
        expect_timer(failures, "replace", timer_of(c), 10, "ctl2")
        if c2.acks:
            failures.append(f"replace: an act without `cid` was acked: {c2.acks}")

        # ----- it survives everything that is not about it -------------------- #
        script = [dict(action="pause"), dict(action="play"), dict(action="seek", positionMs=5000),
                  dict(action="next"), dict(action="enqueue", tracks=tracks(1, "x")),
                  dict(action="setQueue", tracks=tracks(3, "n"), index=0, play=True),
                  dict(action="transfer", target="b"), dict(action="clear")]
        for act in script:
            await ask(c, **act)
            if act["action"] == "transfer":
                await until(lambda: c.active == "b", timeout=3.0)
            await asyncio.sleep(0.2)
            t = timer_of(c2) or {}
            if (t.get("minutes"), t.get("setBy")) != (10, "ctl2"):
                failures.append(f"the timer must survive act {act['action']}; after it "
                                f"session.sleepTimer is {timer_of(c2)!r}")
        if c.active != "b" or c.queue_len != 0:
            failures.append(f"setup: the survival script should end on b with an empty "
                            f"queue, is {c.active} / {c.queue_len}")

        # ----- cleared --------------------------------------------------------- #
        rev, n = c.rev, len(c2.sessions)
        ack = await ask(c, action="sleepTimer", minutes=0)
        await asyncio.sleep(0.2)
        check(failures, "clear", ack, True)
        if len(c2.sessions) != n + 1 or timer_of(c2) is not None or c2.rev != rev + 1 \
                or "sleepTimer" not in session_of(c2):
            failures.append("clear: one `session` frame with `sleepTimer: null` and one "
                            f"bump; got {len(c2.sessions) - n} frame(s), sleepTimer="
                            f"{timer_of(c2)!r}, rev {rev} -> {c2.rev}")
        rev, n = c.rev, len(c2.sessions)
        check(failures, "clear with none set", await ask(c, action="sleepTimer", minutes=0), True)
        await asyncio.sleep(0.2)
        if len(c2.sessions) != n or c2.rev != rev:
            failures.append("clearing when no timer is set changes nothing, so nothing "
                            f"is broadcast; got {len(c2.sessions) - n} frame(s), rev "
                            f"{rev} -> {c2.rev}")

        # ----- the range -------------------------------------------------------- #
        for minutes in (1440, 1):
            check(failures, f"set {minutes}", await ask(c, action="sleepTimer", minutes=minutes), True)
            await asyncio.sleep(0.2)
            expect_timer(failures, f"set {minutes}", timer_of(c2), minutes, "ctl")

        # ----- what cannot be read changes nothing ------------------------------ #
        rev, n = c.rev, len(c2.sessions)
        for bad in (-1, 1441, "30", 30.5, True, None, [5]):
            check(failures, f"minutes {bad!r}",
                  await ask(c, action="sleepTimer", minutes=bad), False, "bad_request")
        check(failures, "no minutes at all", await ask(c, action="sleepTimer"), False, "bad_request")
        await c2.act(action="sleepTimer", minutes=-5)          # and without a cid
        await asyncio.sleep(0.3)
        probe = Peer("Probe", "probe", ["controller"])
        await probe.connect(hub.url)
        kept = (probe.welcome or {}).get("session", {})
        if len(c2.sessions) != n or kept.get("rev") != rev \
                or (kept.get("sleepTimer") or {}).get("minutes") != 1 \
                or (kept.get("sleepTimer") or {}).get("setBy") != "ctl":
            failures.append("ok:false must mean nothing changed, but after nine unreadable "
                            f"sleepTimer acts there are {len(c2.sessions) - n} new session "
                            f"frame(s), rev {rev} -> {kept.get('rev')}, and the timer is "
                            f"{kept.get('sleepTimer')!r} (expected 1 min set by ctl)")
        if c2.acks or any(p.errors for p in everyone):
            failures.append("unreadable sleepTimer acts: no ack without a cid and no "
                            f"`error` frames; got acks {c2.acks}, errors "
                            f"{ {p.name: p.err_codes() for p in everyone if p.errors} }")
        await probe.close()

        # ----- tools/controller.py: `sleep N`, and the timer it prints --------- #
        cli = Cli(hub)
        await until(lambda: row(c, "cli-controller").get("online") is True, timeout=5.0)
        await cli.type("sleep 5")
        expect_timer(failures, "controller.py `sleep 5`", timer_of(c), 5, "cli-controller")
        await cli.type("sleep 0")
        out = await cli.quit()
        if timer_of(c) is not None:
            failures.append(f"controller.py `sleep 0` left the timer at {timer_of(c)!r}")
        if not re.search(r"sleepTimer (4:59|5:00) left of 5 min, set by CLI", out) \
                or "ack c1 ok" not in out or "ack c2 ok" not in out:
            failures.append("controller.py should print the timer of the session frame "
                            f"its `sleep 5` caused, and both acks; it printed:\n{out}")
        for p in everyone:
            await p.close()


# --------------------------------------------------------------------------- #
# 2. running out
# --------------------------------------------------------------------------- #
async def group_fires(failures):
    async with Hub(env={"HUB_SLEEP_MINUTE_S": "1"}) as hub:
        r = Peer("Recv", "recv", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        tmp = Peer("Tmp", "tmp", ["controller"])
        for p in (r, c, tmp):
            await p.connect(hub.url)
        r.auto_echo = r.auto_release = True
        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.3)
        r.model.update(index=0, positionMs=0, isPlaying=True)

        # ----- it pauses what is playing, through do:pause --------------------- #
        # Set by a device that is gone by the time it runs out: the timer is the
        # hub's, not its setter's.
        await ask(tmp, action="sleepTimer", minutes=1)
        t0 = time.monotonic()
        await tmp.close()
        expect_timer(failures, "a one-second minute", timer_of(c), 1, "tmp",
                     minute_ms=1000, slack=400)
        rev, n, nr = c.rev, len(c.sessions), len(r.dos)
        await until(lambda: len(r.dos) > nr, timeout=3.0)
        took = time.monotonic() - t0
        await asyncio.sleep(0.4)
        new = c.sessions[n:]
        if cmds(r, nr) != ["pause"] or type(r.dos[-1].get("seq")) is not int:
            failures.append("running out while playing: the receiver should get one "
                            "do:pause, numbered like any other (it has cmdEcho); it got "
                            f"{r.dos[nr:]}")
        if not 0.7 <= took <= 1.8:
            failures.append(f"a 1-minute timer at 1 s per minute ran out after {took:.2f} s")
        if len(new) != 1 or new[0].get("isPlaying") is not False \
                or new[0].get("sleepTimer") is not None or new[0].get("rev") != rev + 1:
            failures.append("running out while playing: ONE `session` frame that says "
                            "both \"not playing\" and `sleepTimer: null`; got "
                            f"{[(s.get('rev'), s.get('isPlaying'), s.get('sleepTimer')) for s in new]}"
                            f" (rev was {rev})")
        await asyncio.sleep(2.3)                 # past every clock
        if c.is_playing is not False or len(c.sessions) != n + 1:
            failures.append("the timer's pause must stand: 2.3 s later isPlaying="
                            f"{c.is_playing}, {len(c.sessions) - n - 1} further frame(s)")

        # ----- with nothing playing it only says it is gone -------------------- #
        rev, n, nr = c.rev, len(c.sessions), len(r.dos)
        await ask(c, action="sleepTimer", minutes=1)
        await until(lambda: len(c.sessions) >= n + 2, timeout=3.0)
        await asyncio.sleep(0.3)
        new = c.sessions[n:]
        if cmds(r, nr):
            failures.append("running out with the session paused: no directive is "
                            f"sent, but the receiver got {cmds(r, nr)}")
        if len(new) != 2 or new[-1].get("sleepTimer") is not None \
                or new[-1].get("isPlaying") is not False or new[-1].get("rev") != rev + 2:
            failures.append("running out with the session paused: one frame for the "
                            "set, one that says `sleepTimer: null`; got "
                            f"{[(s.get('rev'), s.get('isPlaying'), s.get('sleepTimer')) for s in new]}")

        # ----- with no device active at all ------------------------------------ #
        await r.close()
        await until(lambda: c.active is None)
        n = len(c.sessions)
        await ask(c, action="sleepTimer", minutes=1)
        await until(lambda: len(c.sessions) >= n + 2, timeout=3.0)
        await asyncio.sleep(0.3)
        if timer_of(c) is not None or c.errors:
            failures.append("running out with no device active: `sleepTimer: null` and "
                            f"no `error` to anybody; got sleepTimer={timer_of(c)!r}, "
                            f"errors {c.err_codes()}")

        # ----- cleared: it does not fire --------------------------------------- #
        r = Peer("Recv", "recv", ECHO)
        await r.connect(hub.url)
        r.auto_echo = r.auto_release = True
        await r.act(action="setQueue", tracks=tracks(5), index=0, play=True)
        await asyncio.sleep(0.3)
        r.model.update(index=0, positionMs=0, isPlaying=True)
        await ask(c, action="sleepTimer", minutes=1)
        await asyncio.sleep(0.3)
        await ask(c, action="sleepTimer", minutes=0)
        await asyncio.sleep(1.5)
        if r.dos or c.is_playing is not True:
            failures.append("a cleared timer must not fire: 1.5 s later the receiver "
                            f"has been sent {cmds(r)}, isPlaying={c.is_playing}")

        # ----- replaced: it fires at the new end, once ------------------------- #
        await ask(c, action="sleepTimer", minutes=1)
        await asyncio.sleep(0.4)
        await ask(c, action="sleepTimer", minutes=3)
        t0 = time.monotonic()
        await asyncio.sleep(1.4)                 # the first timer's end is behind us
        if r.dos or c.is_playing is not True or (timer_of(c) or {}).get("minutes") != 3:
            failures.append("a replaced timer must not fire at its old end: the "
                            f"receiver got {cmds(r)}, isPlaying={c.is_playing}, "
                            f"sleepTimer={timer_of(c)!r}")
        await until(lambda: len(r.dos) > 0, timeout=4.0)
        took = time.monotonic() - t0
        await asyncio.sleep(0.4)
        if cmds(r) != ["pause"] or not 2.7 <= took <= 3.9 or timer_of(c) is not None:
            failures.append("a timer replaced by a 3-minute one should pause once, 3 s "
                            f"after the replacement: the receiver got {cmds(r)} after "
                            f"{took:.2f} s, sleepTimer={timer_of(c)!r}")

        log = hub.log()
        if "sleep timer (1 min, set by Tmp) ran out: pausing" not in log \
                or "ran out with nothing playing" not in log:
            failures.append("the hub's log should say when the timer ran out, who set "
                            "it, and whether it paused anything")
        for p in (r, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 3. running out during a transfer
# --------------------------------------------------------------------------- #
async def group_transfer(failures):
    async with Hub(debug=True, env={"HUB_SLEEP_MINUTE_S": "2"}) as hub:
        a = Peer("A", "a", ECHO)
        b = Peer("B", "b", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        for p in (a, b, c):
            await p.connect(hub.url)
        for p in (a, b):
            p.auto_echo = True
            slow_release(p, 1.0)
        await a.act(action="setQueue", tracks=tracks(5), index=1, positionMs=60000, play=True)
        await asyncio.sleep(0.3)
        a.model.update(index=1, positionMs=60000, isPlaying=True)
        await a.report()
        await asyncio.sleep(0.2)

        def commit(frames, target):
            return next((f for f in frames if f.get("t") == "session"
                         and f.get("activeDeviceId") == target), None)

        async def run_out_in_the_wait(label, old, new, play=None, then=None):
            """Set a 2 s timer, start a transfer 1.5 s in (its release wait lasts
            1 s), and return what each device was sent and the commit's frame."""
            await ask(c, action="sleepTimer", minutes=1)
            t0 = time.monotonic()
            await asyncio.sleep(1.5)
            n_old, n_new, start, held = len(old.dos), len(new.dos), len(c.frames), hub.log().count("HELD")
            cid = mint(c)
            extra = {} if play is None else {"play": play}
            await c.act(action="transfer", target=new.device_id, cid=cid, **extra)
            if then is not None:
                await asyncio.sleep(0.1)
                await c.act(**then)
            await until(lambda: c.active == new.device_id, timeout=4.0)
            took = time.monotonic() - t0
            await until(lambda: ack_of(c, cid) is not None, timeout=2.0)
            await asyncio.sleep(0.5)
            check(failures, f"{label}: the transfer", ack_of(c, cid), True)
            if not 2.2 <= took <= 3.4:
                failures.append(f"{label}: setup: the commit came {took:.2f} s after the "
                                "timer was set; the timer (2 s) has to run out inside "
                                "the release wait (1.5 s to 2.5 s)")
            if "ACT pause from sleep timer/" not in hub.log() \
                    or hub.log().count("HELD") <= held:
                failures.append(f"{label}: the hub's log shows no pause from the sleep "
                                "timer HELD behind the transfer, so this case did not "
                                "test the hold")
            if cmds(old, n_old) != ["release"]:
                failures.append(f"{label}: the timer's pause must not be sent to the "
                                f"device being released; it got {cmds(old, n_old)}")
            return new.dos[n_new:], commit(c.frames[start:], new.device_id) or {}

        # ----- (a) a playing session: the load carries the pause --------------- #
        got, frame = await run_out_in_the_wait("(a)", a, b)
        load = got[0] if got else {}
        if [d.get("cmd") for d in got] != ["load"] or load.get("play") is not False \
                or (load.get("index"), load.get("positionMs")) != (1, 60000):
            failures.append("(a) the timer ran out during the release wait: the new "
                            "device should get one load, paused, at index 1 / 60000; "
                            f"it got {[(d.get('cmd'), d.get('play'), d.get('index'), d.get('positionMs')) for d in got]}")
        if frame.get("isPlaying") is not False or frame.get("sleepTimer") is not None:
            failures.append("(a) the commit's `session` frame should say \"not playing\" "
                            f"and `sleepTimer: null`; it says isPlaying="
                            f"{frame.get('isPlaying')} sleepTimer={frame.get('sleepTimer')!r}")
        await asyncio.sleep(2.2)                 # past every clock
        if (c.active, c.is_playing, timer_of(c)) != ("b", False, None):
            failures.append("(a) the pause must still be in force after the commit: "
                            f"active={c.active} isPlaying={c.is_playing} "
                            f"sleepTimer={timer_of(c)!r}")
        if "FOLDED into transfer" not in hub.log():
            failures.append("(a) the hub's log does not show the pause folded into "
                            "the transfer")

        # ----- (b) a paused session and a transfer that asks for play:true ----- #
        # Nothing is playing when the timer runs out, so by itself it would only
        # clear. But the transfer is about to start playback: the pause is queued
        # behind it all the same, and paused is how it comes out.
        got, frame = await run_out_in_the_wait("(b)", b, a, play=True)
        load = got[0] if got else {}
        if [d.get("cmd") for d in got] != ["load"] or load.get("play") is not False:
            failures.append("(b) a transfer with play:true over a paused session, the "
                            "timer running out in its release wait: the new device "
                            "must load paused; it got "
                            f"{[(d.get('cmd'), d.get('play')) for d in got]}")
        await asyncio.sleep(2.2)
        if (c.active, c.is_playing, timer_of(c)) != ("a", False, None):
            failures.append("(b) after the commit nothing may be playing: active="
                            f"{c.active} isPlaying={c.is_playing} sleepTimer={timer_of(c)!r}")

        # ----- (c) behind a skip it is not folded: it runs after the load ------ #
        await ask(c, action="play")
        await asyncio.sleep(0.3)
        got, frame = await run_out_in_the_wait("(c)", a, b, then=dict(action="next"))
        await until(lambda: c.is_playing is False, timeout=2.0)
        seen = [(d.get("cmd"), d.get("play"), d.get("index")) for d in got]
        if seen != [("load", True, 1), ("jump", None, 2), ("pause", None, None)]:
            failures.append("(c) a skip arrived in the wait before the timer ran out: "
                            "the new device gets the load (playing, old index), the "
                            f"jump, then the timer's pause; it got {seen}")
        await asyncio.sleep(2.2)
        if (c.active, c.index, c.is_playing, timer_of(c)) != ("b", 2, False, None):
            failures.append(f"(c) the session should end b / index 2 / paused / no "
                            f"timer; it is {c.active} / {c.index} / isPlaying="
                            f"{c.is_playing} / {timer_of(c)!r}")
        for p in (a, b, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 4. a hub restart
# --------------------------------------------------------------------------- #
def read_state(hub):
    with open(hub.state, encoding="utf-8") as f:
        return json.load(f)


async def playing_receiver(hub):
    r = Peer("Recv", "recv", ECHO)
    await r.connect(hub.url)
    r.auto_echo = r.auto_release = True
    await r.act(action="setQueue", tracks=tracks(4), index=0, play=True)
    await asyncio.sleep(0.3)
    r.model.update(index=0, positionMs=0, isPlaying=True)
    return r


async def group_restart(failures):
    # ----- the end is still ahead: it runs out when it would have ------------- #
    async with Hub(env={"HUB_SLEEP_MINUTE_S": "5"}) as hub:
        r = await playing_receiver(hub)
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        await ask(c, action="sleepTimer", minutes=1)
        t0, wall0 = time.monotonic(), time.time() * 1000
        for p in (r, c):
            await p.close()
        await hub.stop()
        state = read_state(hub)
        ends = state.get("sleepTimerEndsAt")
        if type(ends) is not int or not wall0 + 4000 <= ends <= wall0 + 5500 \
                or (state.get("sleepTimerMinutes"), state.get("sleepTimerSetBy")) != (1, "ctl"):
            failures.append("the state file should hold the timer's wall-clock end "
                            f"(about {wall0 + 5000:.0f}), its minutes and who set it; "
                            f"got sleepTimerEndsAt={ends!r} sleepTimerMinutes="
                            f"{state.get('sleepTimerMinutes')!r} sleepTimerSetBy="
                            f"{state.get('sleepTimerSetBy')!r}")
        if "sleepTimer" in state.get("session", {}):
            failures.append("the state file's session should not carry the computed "
                            "`sleepTimer` (remainingMs means nothing to the next process)")
        await hub.start()
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url, settle=0.1)
        gone = (time.monotonic() - t0) * 1000
        back = (c.welcome or {}).get("session", {}).get("sleepTimer") or {}
        if (back.get("minutes"), back.get("setBy")) != (1, "ctl") \
                or abs(gone + back.get("remainingMs", -9999) - 5000) > 700:
            failures.append("after a restart welcome.session should carry the same "
                            f"timer with what is left of the ORIGINAL 5 s ({gone:.0f} ms "
                            f"have passed); got {back!r}")
        r = await playing_receiver(hub)
        await until(lambda: len(r.dos) > 0, timeout=6.0)
        took = time.monotonic() - t0
        await asyncio.sleep(0.4)
        if cmds(r) != ["pause"] or not 4.5 <= took <= 5.8:
            failures.append("a timer restored from the state file should pause at its "
                            "original end (5 s after it was set, not 5 s after the "
                            f"restart): the receiver got {cmds(r)} after {took:.2f} s")
        if c.is_playing is not False or timer_of(c) is not None:
            failures.append(f"after the restored timer ran out: isPlaying={c.is_playing} "
                            f"sleepTimer={timer_of(c)!r}")
        if "sleep timer restored" not in hub.log():
            failures.append("the hub's log should say a timer was restored")
        for p in (r, c):
            await p.close()

    # ----- the end passed while the hub was down: dropped, pauses nothing ----- #
    async with Hub(env={"HUB_SLEEP_MINUTE_S": "1.5"}) as hub:
        r = await playing_receiver(hub)
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        await ask(c, action="sleepTimer", minutes=1)
        for p in (r, c):
            await p.close()
        await hub.stop()
        if type(read_state(hub).get("sleepTimerEndsAt")) is not int:
            failures.append("setup: the stopped hub's state file holds no timer end")
        await asyncio.sleep(2.0)
        await hub.start()
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        first = (c.welcome or {}).get("session", {})
        if "sleepTimer" not in first or first["sleepTimer"] is not None:
            failures.append("a timer whose end passed while the hub was down is "
                            f"dropped; welcome.session.sleepTimer is "
                            f"{first.get('sleepTimer', '<no key>')!r}")
        r = await playing_receiver(hub)
        await asyncio.sleep(2.0)
        if r.dos or c.is_playing is not True:
            failures.append("a timer that ran out while the hub was down pauses "
                            f"nothing: the receiver got {cmds(r)}, isPlaying={c.is_playing}")
        if "ran out while the hub was down" not in hub.log():
            failures.append("the hub's log should say it dropped a timer that ran out "
                            "while it was down")
        for p in (r, c):
            await p.close()
        await asyncio.sleep(0.2)

        # ----- and the file no longer holds it; nor does a bad value hurt ----- #
        await hub.stop()
        state = read_state(hub)
        if state.get("sleepTimerEndsAt") is not None:
            failures.append("once dropped, the timer's end leaves the state file; it "
                            f"holds {state.get('sleepTimerEndsAt')!r}")
        state["sleepTimerEndsAt"] = "soon"
        with open(hub.state, "w", encoding="utf-8") as f:
            json.dump(state, f)
        await hub.start()
        c = Peer("Ctl", "ctl", ["controller"])
        await c.connect(hub.url)
        first = (c.welcome or {}).get("session", {})
        if first.get("sleepTimer") is not None or len(first.get("queue", [])) != 4 \
                or not row(c, "recv"):
            failures.append("a state file with an unreadable timer end must load "
                            "everything else and no timer: sleepTimer="
                            f"{first.get('sleepTimer')!r}, {len(first.get('queue', []))} "
                            f"queued, recv row {row(c, 'recv')!r}")
        await c.close()


async def main():
    failures = []
    for name, coro in (("frames", group_frames(failures)),
                       ("fires", group_fires(failures)),
                       ("transfer", group_transfer(failures)),
                       ("restart", group_restart(failures))):
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one group must not hide the others
            failures.append(f"[{name}] raised {type(e).__name__}: {e}")

    if failures:
        print("FAIL (sleep timer):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - sleep timer: set-replace-clear-frames-and-rev/remainingMs-per-frame/"
          "silent-countdown/survives-pause-queue-clear-transfer/range/bad_request-"
          "changes-nothing/pauses-through-do-pause/one-frame/only-clears-when-nothing-"
          "plays/cleared-does-not-fire/replaced-fires-at-the-new-end/held-by-a-transfer-"
          "and-folded/play-true-transfer-still-ends-paused/behind-a-skip-runs-after-"
          "the-load/restart-future-end-re-arms/restart-past-end-drops/controller-sleep")


if __name__ == "__main__":
    asyncio.run(main())
