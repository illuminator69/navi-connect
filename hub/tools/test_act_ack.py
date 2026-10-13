#!/usr/bin/env python3
"""
Tests for acknowledged acts (`act.cid` / `ack`, PROTOCOL §5.1 and §5.4) and for
the acts a transfer holds (§7).

Until now a controller learnt what the hub made of an act from the broadcasts
alone, or not at all: a refused transfer was broadcast to every client, a
transfer to a device that cannot play was reported to that device instead of to
whoever asked, and an act that raised inside the hub was a log line. And one
class of act was accepted and then undone: a pause, a seek, a skip or a queue
edit arriving while the hub waited for the old device to release was applied,
sent to the device being abandoned, and overwritten when the transfer committed.

  1. An act carrying `cid` is answered `ack` to its sender, AFTER every `session`,
     `devices`, `savedQueues` and `mixes` frame it caused, with the session `rev`
     that socket has been sent. An act without `cid` gets none, as before.
  2. Every refusal is in the ack, with its code, and with a `cid` in the ack ONLY:
     no `error` frame reaches the sender or anybody else. `ok:false` means nothing
     changed, which needs an act's fields checked before anything is touched.
     Without a `cid` the old `error` frames go where they always went.
  3. While a transfer is under way the acts that change the session are held.
     `play`, `pause`, `playpause` and `seek` that arrived first are folded into
     the handoff, so the new device loads paused, at the sought position; the
     rest run after the load is written, in arrival order. Nothing is sent to the
     device being released, the sender's read loop is never blocked, and a
     refused or no-op transfer releases what it held.

Spins up real hub subprocesses, each on a free port. Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import sys
import time

from test_cmd_echo import ECHO, Hub, Peer, send_together, tracks, until

BROADCASTS = ("session", "devices", "savedQueues", "mixes")
_cid = 0


def mint(peer):
    global _cid
    _cid += 1
    return f"{peer.device_id}-{_cid}"


def ack_of(peer, cid):
    return next((a for a in peer.acks if a.get("cid") == cid), None)


async def ask(peer, timeout=3.0, **act):
    """Send an act with a fresh `cid` and wait for its ack (None if none came)."""
    cid = mint(peer)
    await peer.act(cid=cid, **act)
    await until(lambda: ack_of(peer, cid) is not None, timeout=timeout)
    return ack_of(peer, cid)


def rev_seen(frames):
    """The newest session rev a socket had been sent by the end of `frames`."""
    rev = None
    for f in frames:
        if f.get("t") == "welcome":
            rev = f["session"]["rev"]
        elif f.get("t") == "session":
            rev = f["rev"]
    return rev


def check(failures, label, ack, ok, code=None):
    if ack is None:
        failures.append(f"{label}: no ack")
        return False
    if ack.get("ok") is not ok or ack.get("code") != code:
        failures.append(f"{label}: expected ok={ok} code={code!r}, got "
                        f"ok={ack.get('ok')!r} code={ack.get('code')!r} "
                        f"({ack.get('message')!r})")
        return False
    if type(ack.get("rev")) is not int:
        failures.append(f"{label}: ack.rev is {ack.get('rev')!r}, not an int")
    if not ok and not ack.get("message"):
        failures.append(f"{label}: a refusal should say why (`message`)")
    return True


# --------------------------------------------------------------------------- #
# 1. the ack, and where it sits among the frames the act caused
# --------------------------------------------------------------------------- #
async def group_order(failures):
    async with Hub() as hub:
        r = Peer("Recv", "recv", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        await r.connect(hub.url)
        await c.connect(hub.url)
        r.auto_echo = r.auto_release = True
        for p in (r, c):
            if "actAck" not in ((p.welcome or {}).get("features") or []):
                failures.append(f"welcome.features of {p.name} lacks \"actAck\": "
                                f"{(p.welcome or {}).get('features')!r}")

        # No cid, no ack: the publisher's own setQueue, as every client sends today.
        await r.act(action="setQueue", tracks=tracks(6), index=0, play=True)
        await asyncio.sleep(0.3)
        r.model.update(index=0, positionMs=0, isPlaying=True)
        if r.acks:
            failures.append(f"an act without `cid` was acked: {r.acks}")

        # (act, the broadcasts it must have caused)
        script = [
            (dict(action="setQueue", tracks=tracks(6), index=0, play=True),
             ("session", "savedQueues")),
            (dict(action="pause"), ("session",)),
            (dict(action="play"), ("session",)),
            (dict(action="playpause"), ("session",)),
            (dict(action="seek", positionMs=30000), ("session",)),
            (dict(action="next"), ("session",)),
            (dict(action="previous"), ("session",)),
            (dict(action="jump", index=2), ("session",)),
            (dict(action="enqueue", tracks=tracks(1, "x")), ("session", "savedQueues")),
            (dict(action="move", **{"from": 0, "to": 1}), ("session", "savedQueues")),
            (dict(action="remove", index=5), ("session", "savedQueues")),
            (dict(action="repeat", mode="all"), ("session",)),
            (dict(action="shuffle", on=True), ("session",)),
            (dict(action="shuffle", on=False), ("session",)),
            (dict(action="volume", level=30), ("devices",)),
            (dict(action="favorite", id="t1", favorite=True), ()),
            (dict(action="saveMix", name="Late", kind="similar", seedId="t1"), ("mixes",)),
            (dict(action="clear"), ("session", "savedQueues")),
        ]
        for act, caused in script:
            label = f"act {act['action']}"
            start = len(c.frames)
            ack = await ask(c, **act)
            await asyncio.sleep(0.15)        # anything still to come would show now
            if not check(failures, label, ack, True):
                continue
            frames = c.frames[start:]
            at = next(i for i, f in enumerate(frames) if f is ack)
            before = [f.get("t") for f in frames[:at]]
            late = [f.get("t") for f in frames[at + 1:] if f.get("t") in BROADCASTS]
            if late:
                failures.append(f"{label}: the ack was written BEFORE {late}; it must "
                                "follow every broadcast the act caused")
            missing = [t for t in caused if t not in before]
            if missing:
                failures.append(f"{label}: expected {missing} ahead of the ack, the "
                                f"sender got {before}")
            seen = rev_seen(c.frames[:start + at])
            if ack["rev"] != seen:
                failures.append(f"{label}: ack.rev={ack['rev']} but the newest session "
                                f"this socket had been sent is rev {seen}")

        # ----- without a cid: exactly as before, no ack --------------------- #
        n = len(c.acks)
        await c.act(action="setQueue", tracks=tracks(4), index=0, play=True)
        await c.act(action="pause")
        await asyncio.sleep(0.3)
        if len(c.acks) != n:
            failures.append(f"acts without `cid` were acked: {c.acks[n:]}")
        if r.acks:
            failures.append(f"acks reached a socket that sent no cid: {r.acks[:2]}")

        # ----- the cid itself ----------------------------------------------- #
        long = "L" * 100
        await c.act(action="pause", cid=long)
        await c.act(action="pause", cid=12345)       # not a string: not a cid
        await asyncio.sleep(0.3)
        got = [a.get("cid") for a in c.acks[n:]]
        if got != [long[:64]]:
            failures.append("a cid is a string, capped at 64 characters; a 100-char "
                            f"one and an int gave acks for {got}")
        for p in (r, c):
            await p.close()


# --------------------------------------------------------------------------- #
# 2. every refusal, with a cid (the ack alone) and without (the old frames)
# --------------------------------------------------------------------------- #
async def group_refusals(failures):
    async with Hub() as hub:
        c = Peer("Ctl", "ctl", ["controller"])
        c2 = Peer("Ctl2", "ctl2", ["controller"])
        a = Peer("A", "a", ECHO)
        tv = Peer("TV", "tv", ["receiver"])
        for p in (c, c2, a, tv):
            await p.connect(hub.url)
        a.auto_echo = a.auto_release = True
        everyone = (c, c2, a, tv)

        def errors():
            return {p.name: p.err_codes() for p in everyone if p.errors}

        # ----- no active device --------------------------------------------- #
        check(failures, "pause, nothing active", await ask(c, action="pause"),
              True, "no_active_device")
        check(failures, "seek, nothing active",
              await ask(c, action="seek", positionMs=5000), True, "no_active_device")
        if c.position != 5000:
            failures.append("seek with nothing active is ok:true because the hub "
                            f"applies it to the session, but positionMs={c.position}")
        check(failures, "volume, nothing active",
              await ask(c, action="volume", level=10), False, "no_active_device")

        await a.act(action="setQueue", tracks=tracks(3), index=0, play=True)
        await asyncio.sleep(0.3)
        a.model.update(index=0, positionMs=0, isPlaying=True)
        await tv.send(t="deviceState", reachable=False)
        await asyncio.sleep(0.2)

        # ----- the rest ------------------------------------------------------ #
        check(failures, "unknown action", await ask(c, action="nope"), False, "bad_action")
        check(failures, "an action that is not a string",
              await ask(c, action=["pause"]), False, "bad_action")
        check(failures, "rename of a missing saved queue",
              await ask(c, action="renameSavedQueue", id="nope", name="x"),
              False, "unknown_saved_queue")
        check(failures, "a mix with no name",
              await ask(c, action="saveMix", name="", kind="similar"), False, "bad_mix")
        check(failures, "rename of a missing mix",
              await ask(c, action="renameMix", id="nope", name="x"), False, "unknown_mix")
        check(failures, "transfer to nobody",
              await ask(c, action="transfer", target="ghost"), False, "target_offline")
        check(failures, "transfer to an unreachable speaker",
              await ask(c, action="transfer", target="tv"), False, "target_unreachable")
        check(failures, "transfer to a controller",
              await ask(c, action="transfer", target="ctl2"), False, "not_a_receiver")
        await asyncio.sleep(0.3)
        if c.active != "a":
            failures.append(f"a refused transfer moved the active slot to {c.active}")
        if errors():
            failures.append("with a `cid` a refusal is in the ack ONLY, but `error` "
                            f"frames were sent: {errors()}")

        # ----- an act the hub cannot read: bad_request, and nothing changed -- #
        rev, qlen = c.rev, c.queue_len
        n_sessions = len(c.sessions)
        bad = [
            ("setQueue with a bad index", dict(action="setQueue", tracks=tracks(9), index="x")),
            ("setQueue with tracks that are not a list", dict(action="setQueue", tracks="abc")),
            ("enqueue with tracks that are not a list", dict(action="enqueue", tracks="abc")),
            ("seek with no position", dict(action="seek")),
            ("jump with no index", dict(action="jump", index=None)),
            ("remove with no index", dict(action="remove")),
        ]
        for label, act in bad:
            check(failures, label, await ask(c, **act), False, "bad_request")
        await asyncio.sleep(0.2)
        probe = Peer("Probe", "probe", ["controller"])
        await probe.connect(hub.url)
        if (probe.rev, probe.queue_len) != (rev, qlen) or len(c.sessions) != n_sessions:
            failures.append("ok:false must mean nothing changed, but after six "
                            f"bad_request acts the session went from rev {rev} / "
                            f"{qlen} tracks to rev {probe.rev} / {probe.queue_len} "
                            f"tracks, with {len(c.sessions) - n_sessions} session frame(s)")
        if errors():
            failures.append(f"bad_request acts produced `error` frames: {errors()}")
        await probe.close()

        # ----- without a cid: the frames that always were ------------------- #
        n = len(c.acks)
        await c.act(action="nope")
        await c.act(action="transfer", target="ghost")
        await c.act(action="transfer", target="ctl2")
        await asyncio.sleep(0.5)
        if len(c.acks) != n:
            failures.append(f"refusals without `cid` were acked: {c.acks[n:]}")
        if c.err_codes() != ["bad_action", "target_offline"]:
            failures.append("without a cid the sender should get bad_action and the "
                            f"broadcast target_offline, got {c.err_codes()}")
        if a.err_codes() != ["target_offline"] or tv.err_codes() != ["target_offline"]:
            failures.append("without a cid target_offline is still broadcast to "
                            f"everyone: A {a.err_codes()}, TV {tv.err_codes()}")
        if c2.err_codes() != ["target_offline", "not_a_receiver"]:
            failures.append("without a cid not_a_receiver still goes to the target: "
                            f"Ctl2 got {c2.err_codes()}")
        for p in everyone:
            await p.close()


# --------------------------------------------------------------------------- #
# 3. acts held by a transfer
# --------------------------------------------------------------------------- #
async def group_held(failures):
    async with Hub() as hub:
        a = Peer("A", "a", ECHO)
        b = Peer("B", "b", ECHO)
        c = Peer("Ctl", "ctl", ["controller"])
        for p in (a, b, c):
            await p.connect(hub.url)

        def slow_release(peer, delay=0.5):
            """Answer `do:release` after `delay`: the window an act can arrive in."""
            async def on_do(msg):
                if msg.get("cmd") == "release":
                    await asyncio.sleep(delay)
                    peer.model["isPlaying"] = False
                    await peer.report()
                    await peer.send(t="released", transferId=msg.get("transferId"),
                                    index=peer.model["index"],
                                    positionMs=peer.model["positionMs"])
                elif msg.get("cmd") == "load" and msg.get("transferId"):
                    await peer.send(t="loaded", ok=True, transferId=msg["transferId"])
            peer.auto_release = False
            peer.on_do = on_do

        for p in (a, b):
            p.auto_echo = True
            slow_release(p)
        await a.act(action="setQueue", tracks=tracks(5), index=1, positionMs=60000, play=True)
        await asyncio.sleep(0.3)
        a.model.update(index=1, positionMs=60000, isPlaying=True)
        await a.report()
        await asyncio.sleep(0.2)

        def commit_at(frames, target):
            return next((i for i, f in enumerate(frames)
                         if f.get("t") == "session" and f.get("activeDeviceId") == target),
                        None)

        # ----- (a) pause and seek during the release wait -------------------- #
        na, nb, start = len(a.dos), len(b.dos), len(c.frames)
        t_cid, p_cid, s_cid = mint(c), mint(c), mint(c)
        t0 = time.monotonic()
        await c.act(action="transfer", target="b", cid=t_cid)
        await asyncio.sleep(0.1)
        await c.act(action="pause", cid=p_cid)
        await c.act(action="seek", positionMs=90000, cid=s_cid)
        await until(lambda: all(ack_of(c, x) for x in (t_cid, p_cid, s_cid)), timeout=4.0)
        took = time.monotonic() - t0
        await asyncio.sleep(0.3)
        old_got = [d.get("cmd") for d in a.dos[na:]]
        new_got = [d.get("cmd") for d in b.dos[nb:]]
        load = b.last_do("load") or {}
        if old_got != ["release"]:
            failures.append("(a) a pause and a seek sent during the release wait were "
                            f"sent to the device being released: it got {old_got}")
        if (load.get("play"), load.get("positionMs"), load.get("index")) != (False, 90000, 1):
            failures.append("(a) the load must carry the pause and the seek: got "
                            f"play={load.get('play')} positionMs={load.get('positionMs')} "
                            f"index={load.get('index')}, expected False / 90000 / 1")
        if new_got != ["load"]:
            failures.append(f"(a) folded acts need no directive of their own; the new "
                            f"device got {new_got}")
        frames = c.frames[start:]
        commit = commit_at(frames, "b")
        for name, cid in (("transfer", t_cid), ("pause", p_cid), ("seek", s_cid)):
            ack = ack_of(c, cid)
            if not check(failures, f"(a) {name}", ack, True):
                continue
            at = next(i for i, f in enumerate(frames) if f is ack)
            if commit is None or at < commit:
                failures.append(f"(a) the ack of {name} came before the session frame "
                                "of the commit it was folded into")
        if took < 0.4:
            failures.append(f"(a) setup: the release wait lasted only {took:.2f} s")
        await asyncio.sleep(2.2)             # past every clock
        if (c.active, c.is_playing, c.position) != ("b", False, 90000):
            failures.append("(a) the pause and the seek must still stand after the "
                            f"commit: active={c.active} isPlaying={c.is_playing} "
                            f"positionMs={c.position}, expected b / False / 90000")

        # ----- (b) a skip (no cid) and a queue edit in the wait -------------- #
        await c.act(action="play")
        await asyncio.sleep(0.3)
        na, nb, start = len(a.dos), len(b.dos), len(c.frames)
        t_cid, e_cid = mint(c), mint(c)
        await c.act(action="transfer", target="a", cid=t_cid)
        await asyncio.sleep(0.1)
        await c.act(action="next")           # held with or without a cid
        await c.act(action="enqueue", tracks=tracks(1, "x"), cid=e_cid)
        await until(lambda: all(ack_of(c, x) for x in (t_cid, e_cid)), timeout=4.0)
        await asyncio.sleep(0.4)
        old_got = [d.get("cmd") for d in b.dos[nb:]]
        new_got = [(d.get("cmd"), d.get("index"), len(d.get("tracks") or []))
                   for d in a.dos[na:]]
        if old_got != ["release"]:
            failures.append(f"(b) the device being released was sent {old_got}")
        if new_got != [("load", 1, 5), ("jump", 2, 0), ("queueChanged", 2, 6)]:
            failures.append("(b) a skip and an enqueue sent during the wait must run "
                            "after the load, in arrival order, on the new device: "
                            f"it got {new_got}")
        if (c.active, c.index, c.queue_len) != ("a", 2, 6):
            failures.append(f"(b) the session should be a / index 2 / 6 tracks, is "
                            f"{c.active} / {c.index} / {c.queue_len}")
        frames = c.frames[start:]
        commit = commit_at(frames, "a")
        for name, cid in (("transfer", t_cid), ("enqueue", e_cid)):
            ack = ack_of(c, cid)
            if check(failures, f"(b) {name}", ack, True):
                at = next(i for i, f in enumerate(frames) if f is ack)
                if commit is None or at < commit:
                    failures.append(f"(b) the ack of {name} came before the commit")

        # ----- (c) a seek that arrives AFTER a skip is not folded ------------ #
        # Folding it would load the old track at 30 s and then skip: the next track
        # at 0, where the user asked for the next track at 30 s.
        await asyncio.sleep(0.3)
        nb = len(b.dos)
        t_cid, s_cid = mint(c), mint(c)
        await c.act(action="transfer", target="b", cid=t_cid)
        await asyncio.sleep(0.1)
        await c.act(action="next")
        await c.act(action="seek", positionMs=30000, cid=s_cid)
        await until(lambda: all(ack_of(c, x) for x in (t_cid, s_cid)), timeout=4.0)
        await asyncio.sleep(0.4)
        new_got = [(d.get("cmd"), d.get("index"), d.get("positionMs")) for d in b.dos[nb:]]
        if [g[0] for g in new_got] != ["load", "jump", "seek"] or new_got[0][1] != 2:
            failures.append("(c) [next, seek] in the wait must reach the new device as "
                            f"load (old index), jump, seek; it got {new_got}")
        if (c.active, c.index, c.position) != ("b", 3, 30000):
            failures.append(f"(c) the session should be b / index 3 / 30000 ms, is "
                            f"{c.active} / {c.index} / {c.position}")

        # ----- (d) the active device transfers away from itself -------------- #
        # Its own read loop carries the transfer, the pause AND the `released` the
        # transfer is waiting for. Held acts are queued, never awaited there.
        slow_release(b, delay=0.0)
        await c.act(action="play")
        await asyncio.sleep(0.3)
        na = len(a.dos)
        t_cid, p_cid = mint(b), mint(b)
        t0 = time.monotonic()
        await send_together(b, {"t": "act", "action": "transfer", "target": "a", "cid": t_cid},
                            {"t": "act", "action": "pause", "cid": p_cid})
        await until(lambda: c.active == "a", timeout=3.0)
        took = time.monotonic() - t0
        await until(lambda: all(ack_of(b, x) for x in (t_cid, p_cid)), timeout=2.0)
        await asyncio.sleep(0.3)
        if c.active != "a" or took > 1.0:
            failures.append(f"(d) a device transferring away from itself: active="
                            f"{c.active} after {took:.2f} s (1.5 s is the release "
                            "timeout: its read loop was blocked)")
        load = next((d for d in a.dos[na:] if d.get("cmd") == "load"), {})
        if load.get("play") is not False or c.is_playing is not False:
            failures.append("(d) the pause it sent right behind its transfer was "
                            f"undone: load.play={load.get('play')} "
                            f"session.isPlaying={c.is_playing}")
        check(failures, "(d) transfer", ack_of(b, t_cid), True)
        check(failures, "(d) pause", ack_of(b, p_cid), True)

        # ----- (e) a refused transfer releases what it held ------------------ #
        na = len(a.dos)
        t_cid, p_cid = mint(c), mint(c)
        await send_together(c, {"t": "act", "action": "transfer", "target": "ghost", "cid": t_cid},
                            {"t": "act", "action": "play", "cid": p_cid})
        await until(lambda: all(ack_of(c, x) for x in (t_cid, p_cid)), timeout=3.0)
        await asyncio.sleep(0.3)
        check(failures, "(e) refused transfer", ack_of(c, t_cid), False, "target_offline")
        check(failures, "(e) the play behind it", ack_of(c, p_cid), True)
        if [d.get("cmd") for d in a.dos[na:]] != ["play"] or c.is_playing is not True:
            failures.append("(e) an act held by a transfer that was then refused must "
                            f"still run: A got {[d.get('cmd') for d in a.dos[na:]]}, "
                            f"isPlaying={c.is_playing}")

        # ----- (f) so does a no-op transfer ---------------------------------- #
        na = len(a.dos)
        t_cid, s_cid = mint(c), mint(c)
        await send_together(c, {"t": "act", "action": "transfer", "target": "a", "cid": t_cid},
                            {"t": "act", "action": "seek", "positionMs": 12345, "cid": s_cid})
        await until(lambda: all(ack_of(c, x) for x in (t_cid, s_cid)), timeout=3.0)
        await asyncio.sleep(0.3)
        check(failures, "(f) no-op transfer", ack_of(c, t_cid), True)
        check(failures, "(f) the seek behind it", ack_of(c, s_cid), True)
        if [d.get("cmd") for d in a.dos[na:]] != ["seek"] or c.position != 12345:
            failures.append("(f) an act held by a no-op transfer must still run: A got "
                            f"{[d.get('cmd') for d in a.dos[na:]]}, positionMs={c.position}")

        for p in (a, b, c):
            await p.close()


async def main():
    failures = []
    for name, coro in (("order", group_order(failures)),
                       ("refusals", group_refusals(failures)),
                       ("held", group_held(failures))):
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one group must not hide the others
            failures.append(f"[{name}] raised {type(e).__name__}: {e}")

    if failures:
        print("FAIL (act ack):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - act ack: ack-after-its-broadcasts/rev/no-cid-no-ack/cid-capped/"
          "every-refusal-in-the-ack-only/bad_request-changes-nothing/legacy-errors-"
          "unchanged/held-pause-and-seek-folded/held-edits-after-the-load/"
          "seek-after-skip-not-folded/self-transfer/refused-and-no-op-drain")


if __name__ == "__main__":
    asyncio.run(main())
