#!/usr/bin/env python3
"""
Tests for the `progress` fan-out around a pause.

A position-only `report` is fanned out as `progress` at most once per
PROGRESS_THROTTLE (1 s) — that is what keeps a playing receiver's 1 Hz stream
from becoming a 1 Hz broadcast per report. But after a controller's `act:pause`
the receiver sends ONE report of the position it actually stopped at, and it
lands well inside a second of the last tick. Throttled like the rest, it updated
`session.positionMs` on the hub and reached nobody: every controller sat on the
position from the `session` frame of the pause — up to a second early — until
playback resumed.

  1. A paused report ~100 ms after `act:pause`, at a different position, reaches
     the controller as exactly one `progress` frame carrying that position.
  2. The throttle still holds while playing: five reports inside one second
     yield at most two `progress` frames.

Spins up a real hub subprocess; asserts on the frames a controller receives.
Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from test_edits import TOKEN, Client, _free_port

HUB_DIR = os.path.join(os.path.dirname(__file__), "..")


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


async def main():
    port, failures = _free_port(), []
    state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    url = f"ws://127.0.0.1:{port}"
    hub = spawn(port, state)
    try:
        await asyncio.sleep(1.2)
        r = Client("Recv", device_id="recv")
        c = Client("Ctl", device_id="ctl", caps=["controller"])
        await r.connect(url)
        await c.connect(url)
        tracks = [{"id": f"t{i}", "title": f"T{i}", "durationMs": 180000}
                  for i in range(3)]
        await r.act(action="setQueue", tracks=tracks, index=0, play=True)
        await asyncio.sleep(0.3)

        # ----- 1. the paused position reaches the controller ---------------- #
        # The receiver's ordinary tick. It goes out (nothing was sent before it),
        # and that is what arms the throttle the paused report then lands inside.
        await r.report(positionMs=5000, index=0, isPlaying=True)
        await asyncio.sleep(0.2)
        if [p.get("positionMs") for p in c.progress] != [5000]:
            failures.append(
                f"setup: the playing tick was not fanned out once: {c.progress} "
                "— without it the throttle is not armed and case 1 proves nothing")
        c.progress.clear()

        await c.act(action="pause")
        await asyncio.sleep(0.1)
        # Where the player really stopped: it kept running until do:pause arrived.
        await r.report(positionMs=5420, index=0, isPlaying=False)
        await asyncio.sleep(0.4)
        if c.is_playing is not False:
            failures.append(f"setup: the pause did not reach the session: {c.is_playing}")
        if len(c.progress) != 1:
            failures.append(
                f"the paused report produced {len(c.progress)} progress frame(s), "
                f"not 1: {c.progress} — the controller is left on {c.position} ms "
                "(the position in the pause's own session frame) until playback "
                "resumes")
        else:
            frame = c.progress[0]
            if frame.get("positionMs") != 5420:
                failures.append(f"the progress frame carries {frame.get('positionMs')}, "
                                "not the paused position 5420")
            if frame.get("isPlaying") is not False:
                failures.append(f"the progress frame says isPlaying={frame.get('isPlaying')}")

        # ----- 2. playing reports are still throttled ----------------------- #
        await c.act(action="play")
        # Past the throttle window of the frame above, so the count below starts
        # from a known place: the first report of the burst goes out.
        await asyncio.sleep(1.2)
        c.progress.clear()
        for i in range(5):
            await r.report(positionMs=6000 + i * 150, index=0, isPlaying=True)
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.3)
        if not 1 <= len(c.progress) <= 2:
            failures.append(
                f"five playing reports inside a second produced {len(c.progress)} "
                "progress frames — the throttle must still hold while playing "
                "(expected 1, at most 2)")
    finally:
        await stop(hub)
        os.unlink(state)

    if failures:
        print("FAIL (progress while paused):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS - progress: paused-report-fanned-out-once-at-its-position/"
          "playing-reports-still-throttled")


if __name__ == "__main__":
    asyncio.run(main())
