#!/usr/bin/env python3
"""
Worbus P4, lane H (H1): the server's version in `welcome.server`.

The image's build args set WORBUS_VERSION and WORBUS_COMMIT; the hub joins them into
the display form `0.4.0 (678af87)` and sends it as `welcome.server.version`, beside
project 3's `id`, `name` and `publicUrl` (design §6.7). `/hub/info` does not change.

  welcome_server_version      WORBUS_VERSION=0.4.0, WORBUS_COMMIT=678af87 ->
                              `welcome.server.version == "0.4.0 (678af87)"`, the same
                              for a device secret and the legacy token; the hub's first
                              log line is `Worbus Server 0.4.0 (678af87)`
  welcome_no_version_when_unset   WORBUS_VERSION unset (or blank) -> no `version` key,
                              whatever WORBUS_COMMIT says; no `Worbus Server` line
  version_forms               a dev build `0.4.0-dev`; a full sha cut to 7; no commit
                              -> the version alone
  hub_info_has_no_version     `/hub/info`'s bytes are identical with and without
                              WORBUS_VERSION (same state dir, so the same serverId)

Real hub subprocesses against the fake Navidrome (test_hub_info's HubProc).
Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _k in [k for k in os.environ if k.startswith("WORBUS_")]:
    os.environ.pop(_k)

from test_hub_info import TOKEN, USERS, HubProc  # noqa: E402
from fake_navidrome import FakeNavidrome  # noqa: E402


def first_log_line(h: HubProc) -> str:
    for line in h.log().splitlines():
        if line.strip():
            return line
    return ""


async def server_of(nd: FakeNavidrome, **env: str) -> tuple[dict, dict, str]:
    """(welcome.server by device secret, by legacy token, first log line)."""
    h = HubProc(nd.url, **env)
    try:
        await h.up()
        secret = h.signin("owner")
        by_secret = (await h.welcome(secret=secret)).get("server") or {}
        by_token = (await h.welcome(token=TOKEN, device_id="dev-2")).get("server") or {}
        return by_secret, by_token, first_log_line(h)
    finally:
        h.cleanup()


async def t_welcome_server_version(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    by_secret, by_token, first = await server_of(
        nd, WORBUS_VERSION="0.4.0", WORBUS_COMMIT="678af87")
    for label, server in (("device secret", by_secret), ("legacy token", by_token)):
        if server.get("version") != "0.4.0 (678af87)":
            f.append(f"{label}: welcome.server.version = {server.get('version')!r}, "
                     "expected \"0.4.0 (678af87)\"")
        if not {"id", "name"} <= set(server):
            f.append(f"{label}: project 3's keys went missing: {sorted(server)}")
    if "Worbus Server 0.4.0 (678af87)" not in first:
        f.append(f"first log line is {first!r}, expected it to carry "
                 "\"Worbus Server 0.4.0 (678af87)\"")
    return f


async def t_welcome_no_version_when_unset(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    for env in ({}, {"WORBUS_COMMIT": "678af87"}, {"WORBUS_VERSION": "  "}):
        by_secret, by_token, first = await server_of(nd, **env)
        for label, server in (("device secret", by_secret), ("legacy token", by_token)):
            if "version" in server:
                f.append(f"env {env}: {label}: welcome.server carries version "
                         f"{server['version']!r}")
            if set(server) - {"id", "name", "publicUrl"}:
                f.append(f"env {env}: {label}: unexpected keys {sorted(server)}")
        if "Worbus Server" in first:
            f.append(f"env {env}: first log line names a version: {first!r}")
    return f


async def t_version_forms(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    for env, want in (({"WORBUS_VERSION": "0.4.0-dev", "WORBUS_COMMIT": "abc1234"},
                       "0.4.0-dev (abc1234)"),
                      ({"WORBUS_VERSION": "0.4.0",
                        "WORBUS_COMMIT": "678af875d3c1e0a9b8f7e6d5c4b3a29180716253"},
                       "0.4.0 (678af87)"),
                      ({"WORBUS_VERSION": " 0.4.1 ", "WORBUS_COMMIT": " "}, "0.4.1")):
        by_secret, _t, _first = await server_of(nd, **env)
        if by_secret.get("version") != want:
            f.append(f"env {env}: version {by_secret.get('version')!r}, expected {want!r}")
    return f


async def t_hub_info_has_no_version(nd: FakeNavidrome) -> list[str]:
    f: list[str] = []
    tmp = tempfile.mkdtemp(prefix="welcome-server-")
    bodies = []
    try:
        for env in ({}, {"WORBUS_VERSION": "0.4.0", "WORBUS_COMMIT": "678af87"}):
            h = HubProc(nd.url, tmp=tmp, **env)
            try:
                await h.up()
                status, _hd, raw = h.info()
                if status != 200:
                    f.append(f"env {env}: /hub/info answered {status}")
                bodies.append(raw)
            finally:
                h.stop()
        if len(bodies) == 2 and bodies[0] != bodies[1]:
            f.append(f"/hub/info changed with WORBUS_VERSION: {bodies[0]!r} vs {bodies[1]!r}")
        if any(b"0.4.0" in b or b"version" in b for b in bodies):
            f.append(f"/hub/info carries a version: {bodies!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


async def main_async() -> tuple[list[str], list[str]]:
    nd = FakeNavidrome(USERS).start()
    failures: list[str] = []
    names: list[str] = []
    try:
        for name, fn in (("welcome_server_version", t_welcome_server_version),
                         ("welcome_no_version_when_unset", t_welcome_no_version_when_unset),
                         ("version_forms", t_version_forms),
                         ("hub_info_has_no_version", t_hub_info_has_no_version)):
            try:
                got = await fn(nd)
            except Exception:  # noqa: BLE001
                got = [f"raised:\n{traceback.format_exc()}"]
            names.append(name)
            failures.extend(f"{name}: {x}" for x in got)
    finally:
        nd.stop()
    return failures, names


def main() -> int:
    failures, names = asyncio.run(main_async())
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - welcome server: " + "/".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
