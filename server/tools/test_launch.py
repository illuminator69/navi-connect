#!/usr/bin/env python3
"""
Worbus P4, lane H (H2): the Worbus Server launcher (`server/worbus_launch.py`).

Standard library only. The children are tiny Python scripts this test writes into a
scratch `hub/hub.py` and `lb-bot/listenbrainz_bot.py`; the restart rule runs on a fake
clock (its `sleep` advances fake time and yields a few real milliseconds).

  fills_both_halves_from_three     NAVIDROME_URL/USER/PASSWORD -> both halves' settings,
                                   one secret for both, the fixed data layout
  env_wins_where_set               every filled name the env sets keeps the env's value
  missing_required_refused         each of the three missing (or blank), or left at
                                   .env.example's placeholder -> ConfigRefusal naming it
  two_primary_accounts_refused     HUB_ND_USER=alice with NAVIDROME_USER=bob -> refusal
                                   naming both settings
  two_secrets_refused              HUB_TOKEN and LB_BOT_HUB_TOKEN set apart -> refusal
  secret_minted_once               64 hex, 0600, the same on the second run; HUB_TOKEN
                                   wins and nothing is written; a malformed file -> refusal
                                   naming the file, the file untouched
  crashed_child_restarts_alone     lb-bot exits 1; the hub's pid never changes; lb-bot is
                                   back after 1, 2, 4 ... capped at 60 s; a run of 600 s
                                   or more starts the backoff again at 1 s
  five_crashes_stop_container      five exits of one child within 600 s -> `main` returns
                                   non-zero, its last line naming the child, the hub
                                   stopped with SIGTERM
  sigterm_order                    stop: lb-bot gets SIGTERM first; the hub only after
                                   lb-bot exits, or after 25 s (lb-bot then killed);
                                   then the hub, 25 s; exit 0. Through a real SIGTERM to
                                   a launcher process too
  output_prefixed                  each child's lines on stdout as `[hub] ...`/`[lb-bot] ...`
  launcher_never_prints_secrets    planted password and secret (from HUB_TOKEN and from the
                                   file): none of the launcher's own lines carries them,
                                   in a normal start, a refusal and a crash loop; the
                                   children's own lines do (the plant reached them)

Exits non-zero on failure.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

import worbus_launch as wl  # noqa: E402

THREE = {"NAVIDROME_URL": "http://navidrome.test:4533", "NAVIDROME_USER": "owner",
         "NAVIDROME_PASSWORD": "planted-pass-Zq81"}
HEX64 = re.compile(r"[0-9a-f]{64}")


class FakeClock:
    def __init__(self) -> None:
        self._t = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, s: float) -> None:
        time.sleep(0.002)
        self.advance(s)

    def advance(self, s: float) -> None:
        with self._lock:
            self._t += s


class Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)


def wait_for(cond, timeout: float = 10.0, what: str = "condition") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- #
# The children
# --------------------------------------------------------------------------- #
HUB_SCRIPT = """
import os, signal, sys, time, pathlib
d = pathlib.Path(os.environ["T_DIR"])
with open(d / "hub-starts", "a") as f:
    f.write(f"{os.getpid()}\\n")
mode = os.environ.get("T_HUB", "normal")
def term(*_):
    with open(d / "hub-term", "a") as f:
        f.write(f"{time.time_ns()}\\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "ignore-term" else term)
print("hub says hello", flush=True)
print("hub saw password " + os.environ.get("HUB_ND_PASS", ""), flush=True)
print("hub saw token " + os.environ.get("HUB_TOKEN", ""), flush=True)
while True:
    time.sleep(0.05)
"""

LB_SCRIPT = """
import os, signal, sys, time, pathlib
d = pathlib.Path(os.environ["T_DIR"])
with open(d / "lb-starts", "a") as f:
    f.write(f"{os.getpid()}\\n")
mode = os.environ.get("T_LB", "normal")
print("lb-bot says hello", flush=True)
print("lb-bot saw token " + os.environ.get("LB_BOT_HUB_TOKEN", ""), flush=True)
if mode == "crash":
    print("lb-bot boom", flush=True)
    sys.exit(1)
def term(*_):
    with open(d / "lb-term", "a") as f:
        f.write(f"{time.time_ns()}\\n")
    time.sleep(0.3)
    with open(d / "lb-exit", "a") as f:
        f.write(f"{time.time_ns()}\\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "ignore-term" else term)
while True:
    die = d / "die"
    if die.exists():
        try:
            die.unlink()
        except FileNotFoundError:
            pass
        sys.exit(1)
    time.sleep(0.01)
"""


class Scratch:
    """A scratch app dir (hub/, lb-bot/) and data root, and the launcher's env."""

    def __init__(self, **env: str) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="worbus-launch-"))
        (self.tmp / "app" / "hub").mkdir(parents=True)
        (self.tmp / "app" / "lb-bot").mkdir(parents=True)
        (self.tmp / "app" / "hub" / "hub.py").write_text(textwrap.dedent(HUB_SCRIPT))
        (self.tmp / "app" / "lb-bot" / "listenbrainz_bot.py").write_text(
            textwrap.dedent(LB_SCRIPT))
        self.root = self.tmp / "data"
        self.t_dir = self.tmp / "t"
        self.t_dir.mkdir()
        self.env = {"PATH": os.environ.get("PATH", ""), **THREE,
                    "WORBUS_DATA": str(self.root),
                    "WORBUS_HUB_DIR": str(self.tmp / "app" / "hub"),
                    "WORBUS_LBBOT_DIR": str(self.tmp / "app" / "lb-bot"),
                    "T_DIR": str(self.t_dir), **env}

    def lines(self, name: str) -> list[str]:
        p = self.t_dir / name
        return p.read_text().split() if p.exists() else []

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_main_in_thread(sc: Scratch, clock, out: Capture):
    wl.LAST_SUPERVISOR = None
    result: dict = {}

    def target():
        try:
            result["rc"] = wl.main([], environ=sc.env, clock=clock, out=out,
                                   install_signals=False)
        except BaseException as e:  # noqa: BLE001
            result["error"] = traceback.format_exc()
            result["rc"] = e
    th = threading.Thread(target=target, daemon=True)
    th.start()
    return th, result


# --------------------------------------------------------------------------- #
# fill_env
# --------------------------------------------------------------------------- #
def t_fills_both_halves_from_three() -> list[str]:
    f: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        got = wl.fill_env(dict(THREE), tmp)
        hub, lb = got["hub"], got["lb-bot"]
        secret = (tmp / "hub" / "service-secret").read_text()
        want_hub = {"NAVIDROME_URL": THREE["NAVIDROME_URL"], "HUB_ND_USER": "owner",
                    "HUB_ND_PASS": THREE["NAVIDROME_PASSWORD"], "HUB_TOKEN": secret,
                    "LBBOT_URL": "http://127.0.0.1:8899",
                    "HUB_STATE": str(tmp / "hub" / "state.json"),
                    "HUB_ACCEPT_SHARED_TOKEN": "false"}
        want_lb = {"NAVIDROME_URL": THREE["NAVIDROME_URL"], "NAVIDROME_USER": "owner",
                   "NAVIDROME_PASSWORD": THREE["NAVIDROME_PASSWORD"],
                   "LB_BOT_HUB_TOKEN": secret, "LB_BOT_HUB_URL": "http://127.0.0.1:4790",
                   "LB_BOT_STATE": str(tmp / "lb-bot" / "lb_bot_state.json"),
                   "LB_BOT_REVIEW_FILE": str(tmp / "lb-bot" / "missing_album_review.json"),
                   "LB_BOT_LIBRARY_INDEX": str(tmp / "lb-bot" / "library_index.db"),
                   "LB_BOT_WEB": "1", "LB_BOT_WEB_HOST": "0.0.0.0",
                   "LB_BOT_MUSIC_DIR": "/music",
                   "MBZ_CONTACT": "https://github.com/illuminator69/navi-connect"}
        for half, got_env, want in (("hub", hub, want_hub), ("lb-bot", lb, want_lb)):
            for k, v in want.items():
                if got_env.get(k) != v:
                    f.append(f"{half}: {k} = {'<set>' if k in got_env else '<unset>'}"
                             f"{'' if 'TOKEN' in k or 'PASS' in k else ' ' + repr(got_env.get(k))}"
                             f", expected {'the secret' if 'TOKEN' in k else repr(v)}")
        if hub.get("HUB_TOKEN") != lb.get("LB_BOT_HUB_TOKEN"):
            f.append("the two halves hold different secrets")
        if not HEX64.fullmatch(secret):
            f.append("the minted secret is not 64 lower-case hex characters")
        # The image's root, as the fixed values spell it (nothing written: HUB_TOKEN set).
        at_data = wl.fill_env({**THREE, "HUB_TOKEN": "x" * 64}, Path("/data"))
        for half, k, v in (("hub", "HUB_STATE", "/data/hub/state.json"),
                           ("lb-bot", "LB_BOT_STATE", "/data/lb-bot/lb_bot_state.json"),
                           ("lb-bot", "LB_BOT_REVIEW_FILE",
                            "/data/lb-bot/missing_album_review.json"),
                           ("lb-bot", "LB_BOT_LIBRARY_INDEX", "/data/lb-bot/library_index.db")):
            if at_data[half].get(k) != v:
                f.append(f"root /data: {half} {k} = {at_data[half].get(k)!r}, expected {v!r}")
        # Ruling R4: the rest of the env reaches both halves as it is.
        both = wl.fill_env({**THREE, "HUB_TOKEN": "x" * 64, "TELEGRAM_TOKEN": "tg",
                            "SLSKD_URL": "http://slskd:5030", "HUB_PUBLIC_URL": "wss://h.example"},
                           tmp)
        for half in ("hub", "lb-bot"):
            for k in ("TELEGRAM_TOKEN", "SLSKD_URL", "HUB_PUBLIC_URL"):
                if k not in both[half]:
                    f.append(f"{half}: {k} from the env was not passed through")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


FILLED = {"hub": ("HUB_ND_USER", "HUB_ND_PASS", "HUB_TOKEN", "LBBOT_URL", "HUB_STATE",
                  "HUB_ACCEPT_SHARED_TOKEN"),
          "lb-bot": ("LB_BOT_HUB_TOKEN", "LB_BOT_HUB_URL", "LB_BOT_STATE",
                     "LB_BOT_REVIEW_FILE", "LB_BOT_LIBRARY_INDEX", "LB_BOT_WEB",
                     "LB_BOT_WEB_HOST", "LB_BOT_MUSIC_DIR", "MBZ_CONTACT")}


def t_env_wins_where_set() -> list[str]:
    f: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        for half, names in FILLED.items():
            for name in names:
                if name in ("HUB_ND_USER",):
                    value = "owner"          # anything else is two primary accounts
                elif name in ("HUB_TOKEN", "LB_BOT_HUB_TOKEN"):
                    value = "e" * 64
                else:
                    value = f"env-value-of-{name.lower()}"
                got = wl.fill_env({**THREE, name: value}, tmp)
                if got[half].get(name) != value:
                    f.append(f"{half}: {name} set in the env became "
                             f"{'<set>' if 'TOKEN' in name or 'PASS' in name else repr(got[half].get(name))}")
                if name in ("HUB_TOKEN", "LB_BOT_HUB_TOKEN"):
                    other = "LB_BOT_HUB_TOKEN" if name == "HUB_TOKEN" else "HUB_TOKEN"
                    other_half = "lb-bot" if half == "hub" else "hub"
                    if got[other_half].get(other) != value:
                        f.append(f"{name} set alone: {other} is not the same value")
        # A secret in the env (either name): nothing is minted.
        for name in ("HUB_TOKEN", "LB_BOT_HUB_TOKEN"):
            fresh = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
            try:
                wl.fill_env({**THREE, name: "e" * 64}, fresh)
                if (fresh / "hub" / "service-secret").exists():
                    f.append(f"{name} set, but a secret file was minted")
            finally:
                shutil.rmtree(fresh, ignore_errors=True)
        # The ports the internal addresses use follow the env when it moves them.
        moved = wl.fill_env({**THREE, "HUB_TOKEN": "e" * 64, "HUB_PORT": "4800",
                             "LB_BOT_WEB_PORT": "8900"}, tmp)
        if moved["lb-bot"].get("LB_BOT_HUB_URL") != "http://127.0.0.1:4800":
            f.append(f"HUB_PORT=4800: LB_BOT_HUB_URL {moved['lb-bot'].get('LB_BOT_HUB_URL')!r}")
        if moved["hub"].get("LBBOT_URL") != "http://127.0.0.1:8900":
            f.append(f"LB_BOT_WEB_PORT=8900: LBBOT_URL {moved['hub'].get('LBBOT_URL')!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


def _refusal(env: dict, root: Path) -> str | None:
    try:
        wl.fill_env(env, root)
    except wl.ConfigRefusal as e:
        return str(e)
    return None


def t_missing_required_refused() -> list[str]:
    f: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        for name in THREE:
            for env in ({k: v for k, v in THREE.items() if k != name}, {**THREE, name: "  "}):
                msg = _refusal(env, tmp)
                if msg is None:
                    f.append(f"{name} {'blank' if name in env else 'missing'}: not refused")
                elif name not in msg:
                    f.append(f"{name} missing: the refusal does not name it: {msg!r}")
        # .env.example's placeholders, copied as they are, are refused by name.
        examples = {"NAVIDROME_URL": "https://navidrome.example.com/",
                    "NAVIDROME_USER": "your-navidrome-username",
                    "NAVIDROME_PASSWORD": '"your-navidrome-password"'}
        for name, value in examples.items():
            msg = _refusal({**THREE, name: value}, tmp)
            if msg is None or name not in msg or "example" not in msg:
                f.append(f"{name} left at .env.example's value: refusal {msg!r}")
        if (tmp / "hub" / "service-secret").exists():
            f.append("a refused start minted a secret")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


def t_two_primary_accounts_refused() -> list[str]:
    f: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        msg = _refusal({**THREE, "NAVIDROME_USER": "bob", "HUB_ND_USER": "alice"}, tmp)
        if msg is None:
            f.append("HUB_ND_USER=alice with NAVIDROME_USER=bob was not refused")
        elif "HUB_ND_USER" not in msg or "NAVIDROME_USER" not in msg:
            f.append(f"the refusal does not name both settings: {msg!r}")
        # The same account in another case is the same Navidrome account.
        if _refusal({**THREE, "HUB_ND_USER": "Owner"}, tmp) is not None:
            f.append("HUB_ND_USER=Owner with NAVIDROME_USER=owner was refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


def t_two_secrets_refused() -> list[str]:
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        msg = _refusal({**THREE, "HUB_TOKEN": "a" * 64, "LB_BOT_HUB_TOKEN": "b" * 64}, tmp)
        if msg is None:
            return ["HUB_TOKEN and LB_BOT_HUB_TOKEN set apart was not refused"]
        if "HUB_TOKEN" not in msg or "LB_BOT_HUB_TOKEN" not in msg or "a" * 64 in msg:
            return [f"the refusal should name both settings and no value: {msg!r}"]
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_secret_minted_once() -> list[str]:
    f: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
    try:
        path = tmp / "hub" / "service-secret"
        first = wl.fill_env(dict(THREE), tmp)["hub"]["HUB_TOKEN"]
        if not path.exists():
            return ["no service-secret file was written"]
        raw = path.read_text()
        if not HEX64.fullmatch(raw):
            f.append(f"file holds {len(raw)} characters, not 64 lower-case hex")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            f.append(f"file mode {oct(mode)}, expected 0o600")
        if first != raw:
            f.append("the secret handed out is not the file's")
        second = wl.fill_env(dict(THREE), tmp)["hub"]["HUB_TOKEN"]
        if second != first or path.read_text() != raw:
            f.append("the second run did not read the same secret")
        # HUB_TOKEN in the env wins, and nothing is written.
        other = Path(tempfile.mkdtemp(prefix="worbus-fill-"))
        try:
            got = wl.fill_env({**THREE, "HUB_TOKEN": "f" * 64}, other)
            if got["hub"]["HUB_TOKEN"] != "f" * 64 or got["lb-bot"]["LB_BOT_HUB_TOKEN"] != "f" * 64:
                f.append("HUB_TOKEN from the env did not win in both halves")
            if (other / "hub" / "service-secret").exists():
                f.append("HUB_TOKEN set, but a secret file was written")
        finally:
            shutil.rmtree(other, ignore_errors=True)
        # A malformed file stops the start, names the file, is never replaced.
        for bad in ("abc", "G" * 64, "A" * 64, "a" * 63, ""):
            path.write_text(bad)
            msg = _refusal(dict(THREE), tmp)
            if msg is None:
                f.append(f"a {len(bad)}-character file {bad[:4]!r}... was accepted")
            elif str(path) not in msg:
                f.append(f"the refusal does not name the file: {msg!r}")
            if path.read_text() != bad:
                f.append("a malformed file was rewritten")
        # A trailing newline (someone wrote it with `echo`) is the same secret.
        path.write_text("c" * 64 + "\n")
        if wl.fill_env(dict(THREE), tmp)["hub"]["HUB_TOKEN"] != "c" * 64:
            f.append("a file with a trailing newline did not read as its 64 characters")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #
def t_crashed_child_restarts_alone() -> list[str]:
    f: list[str] = []
    sc = Scratch()
    clock = FakeClock()
    out = Capture()
    th, result = run_main_in_thread(sc, clock, out)
    sup = None
    try:
        wait_for(lambda: wl.LAST_SUPERVISOR is not None, what="the supervisor")
        sup = wl.LAST_SUPERVISOR
        wait_for(lambda: len(sc.lines("hub-starts")) == 1 and len(sc.lines("lb-starts")) == 1,
                 what="both children up")
        hub_pid = sc.lines("hub-starts")[0]
        want = [1, 2, 4, 8, 16, 32, 60, 60]
        for i, delay in enumerate(want):
            clock.advance(300)            # the child ran five minutes, then crashed
            (sc.t_dir / "die").write_text("")
            wait_for(lambda: len(sc.lines("lb-starts")) == i + 2, timeout=15,
                     what=f"lb-bot restart {i + 1}")
        # A run of 600 s or more: the backoff starts again at 1 s.
        clock.advance(601)
        (sc.t_dir / "die").write_text("")
        wait_for(lambda: len(sc.lines("lb-starts")) == len(want) + 2, timeout=15,
                 what="lb-bot restart after a long run")
        want.append(1)
        delays = [d for (_t, kind, name, d) in sup.events if kind == "restart" and name == "lb-bot"]
        if delays != want:
            f.append(f"lb-bot restart delays {delays}, expected {want}")
        exits = [t for (t, kind, name, _d) in sup.events if kind == "exit" and name == "lb-bot"]
        starts = [t for (t, kind, name, _d) in sup.events if kind == "start" and name == "lb-bot"]
        for n, (ex, st, d) in enumerate(zip(exits, starts[1:], want)):
            if st - ex < d:
                f.append(f"restart {n + 1} came {st - ex:.1f} s after the exit, before its {d} s")
        if sc.lines("hub-starts") != [hub_pid]:
            f.append(f"the hub was restarted: starts {sc.lines('hub-starts')}")
        if str(sup.children[0].proc.pid) != hub_pid or sup.children[0].proc.poll() is not None:
            f.append("the hub child is not the process it started as")
        if sc.lines("hub-term"):
            f.append("the hub was sent SIGTERM during lb-bot's restarts")
        if not any("lb-bot exited" in ln and "restarting in 1 s" in ln for ln in out.snapshot()):
            f.append("no launcher line says why lb-bot restarted: "
                     f"{[ln for ln in out.snapshot() if ln.startswith('[worbus]')][-3:]}")
    finally:
        if sup is not None:
            sup.request_stop()
        th.join(60)
        sc.cleanup()
    if th.is_alive():
        f.append("main did not return after the stop")
    elif result.get("rc") != 0:
        f.append(f"main returned {result.get('rc')!r} after a stop: {result.get('error', '')}")
    return f


def t_five_crashes_stop_container() -> list[str]:
    f: list[str] = []
    sc = Scratch(T_LB="crash")
    out = Capture()
    th, result = run_main_in_thread(sc, FakeClock(), out)
    th.join(60)
    try:
        if th.is_alive():
            wl.LAST_SUPERVISOR.request_stop()
            th.join(30)
            return ["main kept running through five lb-bot crashes"]
        rc = result.get("rc")
        if not isinstance(rc, int) or rc == 0:
            f.append(f"main returned {rc!r}, expected non-zero: {result.get('error', '')}")
        if len(sc.lines("lb-starts")) != 5:
            f.append(f"lb-bot started {len(sc.lines('lb-starts'))} times, expected 5")
        lines = out.snapshot()
        last = lines[-1] if lines else ""
        if not last.startswith("[worbus]") or "lb-bot" not in last:
            f.append(f"the last line does not name the child: {last!r}")
        if not sc.lines("hub-term"):
            f.append("the hub was not stopped with SIGTERM")
        sup = wl.LAST_SUPERVISOR
        hub_proc = sup.children[0].proc
        if hub_proc is not None and hub_proc.poll() is None:
            f.append("the hub process is still running")
            hub_proc.kill()
    finally:
        sc.cleanup()
    return f


def _term_order(lb_mode: str, hub_mode: str) -> list[str]:
    f: list[str] = []
    sc = Scratch(T_LB=lb_mode, T_HUB=hub_mode)
    clock = FakeClock()
    th, result = run_main_in_thread(sc, clock, Capture())
    try:
        wait_for(lambda: wl.LAST_SUPERVISOR is not None, what="the supervisor")
        sup = wl.LAST_SUPERVISOR
        wait_for(lambda: sc.lines("hub-starts") and sc.lines("lb-starts"), what="both up")
        time.sleep(0.3)        # let both install their handlers
        sup.request_stop()
        th.join(60)
        if th.is_alive():
            return [f"lb={lb_mode} hub={hub_mode}: main did not return"]
        if result.get("rc") != 0:
            f.append(f"lb={lb_mode} hub={hub_mode}: main returned {result.get('rc')!r}")
        ev = [(t, kind, name) for (t, kind, name, _d) in sup.events
              if kind in ("term", "kill", "exit")]
        first_term = next((e for e in ev if e[1] == "term"), None)
        if first_term is None or first_term[2] != "lb-bot":
            f.append(f"lb={lb_mode}: the first SIGTERM went to {first_term}, not lb-bot")
        hub_term = next((e for e in ev if e[1] == "term" and e[2] == "hub"), None)
        lb_gone = next((e for e in ev if e[1] in ("exit", "kill") and e[2] == "lb-bot"), None)
        if hub_term is None:
            f.append(f"lb={lb_mode}: the hub was never sent SIGTERM")
        elif lb_gone is None or ev.index(hub_term) < ev.index(lb_gone):
            f.append(f"lb={lb_mode}: the hub got SIGTERM before lb-bot was gone: {ev}")
        if lb_mode == "normal" and hub_mode == "normal":
            lb_exit = sc.lines("lb-exit")
            hub_term_wall = sc.lines("hub-term")
            if not lb_exit or not hub_term_wall or int(hub_term_wall[0]) < int(lb_exit[0]):
                f.append(f"wall clock: lb-bot exit {lb_exit}, hub SIGTERM {hub_term_wall}")
        if lb_mode == "ignore-term":
            waited = hub_term[0] - first_term[0] if hub_term and first_term else -1
            if not 25 <= waited < 27:
                f.append(f"lb-bot ignoring SIGTERM: the hub's SIGTERM came {waited:.1f} s "
                         "after lb-bot's, expected 25 s")
            if not any(e[1] == "kill" and e[2] == "lb-bot" for e in ev):
                f.append("lb-bot ignoring SIGTERM was not killed after 25 s")
        if hub_mode == "ignore-term":
            hub_kill = next((e for e in ev if e[1] == "kill" and e[2] == "hub"), None)
            if hub_kill is None or not 25 <= hub_kill[0] - hub_term[0] < 27:
                f.append(f"the hub ignoring SIGTERM: killed at {hub_kill}, term at {hub_term}")
        for c in sup.children:
            if c.proc is not None and c.proc.poll() is None:
                f.append(f"{c.name} still running after main returned")
                c.proc.kill()
    finally:
        sc.cleanup()
    return f


def _real_sigterm() -> list[str]:
    """A launcher process, a real clock, a real SIGTERM: rc 0, lb-bot stopped first."""
    f: list[str] = []
    sc = Scratch()
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "worbus_launch.py")],
                            env=sc.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: sc.lines("hub-starts") and sc.lines("lb-starts"), what="both up")
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(40)
        output = proc.stdout.read().decode()
        if rc != 0:
            f.append(f"launcher exited {rc} after SIGTERM: {output[-400:]!r}")
        lb_exit, hub_term = sc.lines("lb-exit"), sc.lines("hub-term")
        if not lb_exit or not hub_term or int(hub_term[0]) < int(lb_exit[0]):
            f.append(f"real SIGTERM: lb-bot exit {lb_exit}, hub SIGTERM {hub_term}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        sc.cleanup()
    return f


def t_sigterm_order() -> list[str]:
    return (_term_order("normal", "normal") + _term_order("ignore-term", "normal")
            + _term_order("normal", "ignore-term") + _real_sigterm())


def t_output_prefixed() -> list[str]:
    f: list[str] = []
    sc = Scratch()
    out = Capture()
    th, _result = run_main_in_thread(sc, FakeClock(), out)
    try:
        wait_for(lambda: any(ln == "[hub] hub says hello" for ln in out.snapshot())
                 and any(ln == "[lb-bot] lb-bot says hello" for ln in out.snapshot()),
                 what="both children's lines")
        for ln in out.snapshot():
            if not ln.startswith(("[hub] ", "[lb-bot] ", "[worbus] ")):
                f.append(f"an unprefixed line: {ln!r}")
    except AssertionError as e:
        f.append(f"{e}: {out.snapshot()}")
    finally:
        if wl.LAST_SUPERVISOR is not None:
            wl.LAST_SUPERVISOR.request_stop()
        th.join(60)
        sc.cleanup()
    return f


def _launcher_output(env_extra: dict, write_secret: str | None = None,
                     run_s: float = 2.0) -> tuple[str, int | None]:
    sc = Scratch(**env_extra)
    if write_secret is not None:
        (sc.root / "hub").mkdir(parents=True)
        p = sc.root / "hub" / "service-secret"
        p.write_text(write_secret)
        p.chmod(0o600)
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "worbus_launch.py")],
                            env=sc.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        try:
            proc.wait(run_s)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            proc.wait(40)
        return proc.stdout.read().decode(errors="replace"), proc.returncode
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        sc.cleanup()


def t_launcher_never_prints_secrets() -> list[str]:
    f: list[str] = []
    password = THREE["NAVIDROME_PASSWORD"]
    planted_token = "9d1c" + "7" * 56 + "e2a0"
    file_secret = "4b7e" + "1" * 56 + "c3d9"
    runs = {
        "HUB_TOKEN from the env": ({"HUB_TOKEN": planted_token}, None, 3.0,
                                   (password, planted_token)),
        "secret from the file": ({}, file_secret, 3.0, (password, file_secret)),
        "crash loop": ({"T_LB": "crash", "HUB_TOKEN": planted_token}, None, 30.0,
                       (password, planted_token)),
        "two primary accounts": ({"HUB_ND_USER": "alice", "HUB_TOKEN": planted_token}, None,
                                 10.0, (password, planted_token)),
        "two secrets": ({"HUB_TOKEN": planted_token, "LB_BOT_HUB_TOKEN": file_secret}, None,
                        10.0, (password, planted_token, file_secret)),
        "malformed file": ({}, password, 10.0, (password,)),
    }
    for label, (extra, secret_file, run_s, planted) in runs.items():
        output, rc = _launcher_output(extra, secret_file, run_s)
        own = [ln for ln in output.splitlines() if not ln.startswith(("[hub] ", "[lb-bot] "))]
        for value in planted:
            for ln in own:
                if value in ln:
                    f.append(f"{label}: the launcher printed a planted value: line "
                             f"{ln.replace(value, '<PLANTED>')!r}")
        if label in ("HUB_TOKEN from the env", "secret from the file"):
            kids = "\n".join(ln for ln in output.splitlines()
                             if ln.startswith(("[hub] ", "[lb-bot] ")))
            if not all(v in kids for v in planted):
                f.append(f"{label}: positive control failed, the children never saw the "
                         "planted values (the check above proves nothing)")
            if rc != 0:
                f.append(f"{label}: launcher exited {rc}")
        elif rc in (0, None):
            f.append(f"{label}: launcher exited {rc}, expected a refusal or a stop")
        if not own:
            f.append(f"{label}: the launcher printed nothing of its own")
    return f


# --------------------------------------------------------------------------- #
TESTS = (
    ("fills_both_halves_from_three", t_fills_both_halves_from_three),
    ("env_wins_where_set", t_env_wins_where_set),
    ("missing_required_refused", t_missing_required_refused),
    ("two_primary_accounts_refused", t_two_primary_accounts_refused),
    ("two_secrets_refused", t_two_secrets_refused),
    ("secret_minted_once", t_secret_minted_once),
    ("crashed_child_restarts_alone", t_crashed_child_restarts_alone),
    ("five_crashes_stop_container", t_five_crashes_stop_container),
    ("sigterm_order", t_sigterm_order),
    ("output_prefixed", t_output_prefixed),
    ("launcher_never_prints_secrets", t_launcher_never_prints_secrets),
)


def main() -> int:
    failures: list[str] = []
    names: list[str] = []
    for name, fn in TESTS:
        wl.LAST_SUPERVISOR = None
        try:
            got = fn()
        except Exception:  # noqa: BLE001
            got = [f"raised:\n{traceback.format_exc()}"]
        names.append(name)
        failures.extend(f"{name}: {x}" for x in got)
    for x in failures:
        print(f"FAIL - {x}")
    if failures:
        return 1
    print("PASS - worbus launcher: " + "/".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
