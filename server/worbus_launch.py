#!/usr/bin/env python3
"""
The Worbus Server launcher (Worbus P4, design §6.1, §6.2). Standard library only.

Runs under tini as PID 1 in the `worbus-server` image, or by hand outside one
(`WORBUS_DATA` moves the data root). It

  1. fills the settings the two halves need from each other out of the three a person
     sets (NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD), only where the env does
     not already set them, and mints the service secret once into
     `<root>/hub/service-secret`;
  2. starts the hub and lb-bot as two child processes and prefixes every line they
     print with `[hub] ` or `[lb-bot] `, so `docker logs worbus-server` reads as one log;
     its own lines start `[worbus] `. Every child line first goes through the shared
     redaction rule (`worbus_redact.py`, ruling R17), so no password, token or Subsonic
     login a child prints reaches `docker logs`;
  3. restarts a child that exits, after 1, 2, 4 ... 60 s, without touching the other
     (playback lives in the hub, and an lb-bot crash must not drop a client socket);
     five exits of one child within 600 s stop the whole server, with a last line
     naming the child, so Docker's restart policy and the person see it;
  4. on SIGTERM (or SIGINT) stops lb-bot first and the hub last, up to 25 s each, then
     exits 0. The compose file gives it `stop_grace_period: 60s`.

It never prints a setting's value: only names, paths and exit statuses.
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# worbus_redact.py sits beside this file (/app/server in the image). Put that folder on
# the path explicitly, so the import holds under `python -I` or PYTHONSAFEPATH too.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from worbus_redact import redact  # noqa: E402

REQUIRED = ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD")
# server/.env.example's placeholders, copied as they are: refused by name.
EXAMPLE_VALUES = {"NAVIDROME_URL": "https://navidrome.example.com",
                  "NAVIDROME_USER": "your-navidrome-username",
                  "NAVIDROME_PASSWORD": "your-navidrome-password"}
MBZ_CONTACT_DEFAULT = "https://github.com/illuminator69/worbus"
SECRET_NAME = "service-secret"
_SECRET_RE = re.compile(r"[0-9a-f]{64}")

BACKOFF_FIRST = 1.0
BACKOFF_CAP = 60.0
CRASH_LIMIT = 5             # exits of one child ...
CRASH_WINDOW = 600.0        # ... within this many seconds stop the server
STABLE_RUN = 600.0          # a run this long starts the backoff again at 1 s
STOP_WAIT = 25.0            # per child, on SIGTERM
TICK = 0.2
# One child line kept at most (bytes), before it is redacted (fix round 2, ruling R18):
# the rest of a longer line is read and dropped, and the kept part ends with a marker.
LINE_CAP = 64 * 1024
_CUT_KEEPS = frozenset("\"',;&")     # a cut line is trimmed back to one of these or a space

# The newest Supervisor, for the suite (tools/test_launch.py).
LAST_SUPERVISOR: Optional["Supervisor"] = None


class ConfigRefusal(Exception):
    """A setting that stops the start. The message names settings and files only."""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _set(env: dict, name: str) -> bool:
    return bool(str(env.get(name) or "").strip())


def _service_secret(env: dict, root: Path) -> str:
    """HUB_TOKEN when set, else LB_BOT_HUB_TOKEN when set, else the side file,
    minted (32 random bytes as 64 hex, mode 0600) when absent. Both set apart is a
    refusal: the two halves would answer each other 401 (B-263)."""
    hub_token = str(env.get("HUB_TOKEN") or "").strip()
    lb_token = str(env.get("LB_BOT_HUB_TOKEN") or "").strip()
    if hub_token and lb_token and hub_token != lb_token:
        raise ConfigRefusal(
            "HUB_TOKEN and LB_BOT_HUB_TOKEN are both set, to different values. Inside the "
            "Worbus Server they are one secret: remove LB_BOT_HUB_TOKEN (or set both the same).")
    if hub_token or lb_token:
        return hub_token or lb_token
    path = root / "hub" / SECRET_NAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass                       # another start won the race: read its file
        else:
            with os.fdopen(fd, "w") as f:
                f.write(value)
            os.chmod(path, 0o600)
            return value
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigRefusal(f"{path} cannot be read ({type(e).__name__}); fix its "
                            "permissions, or delete it to mint a new service secret.") from None
    if not _SECRET_RE.fullmatch(value):
        raise ConfigRefusal(
            f"{path} is not a service secret (64 lower-case hex characters). The launcher "
            "never replaces it: delete it to mint a new one, or set HUB_TOKEN.")
    return value


def fill_env(env: dict, root: Path) -> dict:
    """The hub's and lb-bot's environments, as `{"hub": {...}, "lb-bot": {...}}`.

    Each is the whole of `env` (ruling R4: every other setting reaches both halves as
    it is) plus the filled names of design §6.2, each only where `env` does not set
    it. Raises ConfigRefusal for a missing required setting, two primary accounts or
    two secrets, and for a malformed secret file. Mints the secret file only when it
    is absent and no secret is in `env`."""
    root = Path(root)
    missing = [n for n in REQUIRED if not _set(env, n)]
    if missing:
        raise ConfigRefusal(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. The Worbus "
            "Server needs three settings: NAVIDROME_URL, NAVIDROME_USER (a Navidrome admin) "
            "and NAVIDROME_PASSWORD. See .env.example.")
    examples = [n for n in REQUIRED
                if env[n].strip().strip("\"'").rstrip("/").lower() == EXAMPLE_VALUES[n]]
    if examples:
        raise ConfigRefusal(
            f"{', '.join(examples)} {'is' if len(examples) == 1 else 'are'} still the example "
            "value from .env.example; set your own.")
    user = env["NAVIDROME_USER"].strip()
    if _set(env, "HUB_ND_USER") and env["HUB_ND_USER"].strip().lower() != user.lower():
        raise ConfigRefusal(
            "HUB_ND_USER and NAVIDROME_USER name different Navidrome accounts. The Worbus "
            "Server has one primary account: remove HUB_ND_USER and keep NAVIDROME_USER.")
    secret = _service_secret(env, root)
    hub_port = str(env.get("HUB_PORT") or "").strip() or "4790"
    lb_port = str(env.get("LB_BOT_WEB_PORT") or "").strip() or "8899"
    hub_fill = {
        "HUB_ND_USER": user,
        "HUB_ND_PASS": env["NAVIDROME_PASSWORD"],
        "HUB_TOKEN": secret,
        "LBBOT_URL": f"http://127.0.0.1:{lb_port}",
        "HUB_STATE": str(root / "hub" / "state.json"),
        "HUB_ACCEPT_SHARED_TOKEN": "false",
    }
    lb_fill = {
        "LB_BOT_HUB_TOKEN": secret,
        "LB_BOT_HUB_URL": f"http://127.0.0.1:{hub_port}",
        "LB_BOT_STATE": str(root / "lb-bot" / "lb_bot_state.json"),
        "LB_BOT_REVIEW_FILE": str(root / "lb-bot" / "missing_album_review.json"),
        "LB_BOT_LIBRARY_INDEX": str(root / "lb-bot" / "library_index.db"),
        "LB_BOT_WEB": "1",
        "LB_BOT_WEB_HOST": "0.0.0.0",
        "LB_BOT_MUSIC_DIR": "/music",
        "MBZ_CONTACT": MBZ_CONTACT_DEFAULT,
    }
    out = {}
    for half, fill in (("hub", hub_fill), ("lb-bot", lb_fill)):
        e = dict(env)
        for k, v in fill.items():
            if not _set(e, k):
                e[k] = v
        out[half] = e
    return out


def filled_names(env: dict, filled: dict) -> list[str]:
    """The names `fill_env` added to one half, for the start-up line (names only)."""
    return sorted(k for k in filled if not _set(env, k))


# --------------------------------------------------------------------------- #
# Children
# --------------------------------------------------------------------------- #
class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)


class Child:
    def __init__(self, name: str, argv: list[str], env: dict, cwd: str) -> None:
        self.name = name
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.reader: Optional[threading.Thread] = None
        self.started_at = 0.0
        self.restart_at: Optional[float] = None
        self.exits: list[float] = []
        self.quick_exits = 0          # exits in a row of runs shorter than STABLE_RUN


class Supervisor:
    """Runs `children` (started in order, stopped in reverse) on `clock`.

    `events` records (time, kind, name, detail) for every start, exit, restart
    (detail: the delay), term and kill, on the supervisor's clock."""

    def __init__(self, children: list[Child], clock=None,
                 out: Callable[[str], None] = None) -> None:
        self.children = children
        self.clock = clock or RealClock()
        self._out = out or _stdout_line
        self._out_lock = threading.Lock()
        # A plain flag, not an Event: SIGTERM's handler runs on the main thread, maybe
        # while that thread is setting the flag itself (the fifth crash), and an
        # Event's non-reentrant lock would then deadlock it. The loop polls on TICK.
        self._stop_requested = False
        self.events: list[tuple[float, str, str, float]] = []
        self.failed: Optional[str] = None

    # -- output -------------------------------------------------------------- #
    def _write(self, line: str) -> None:
        """One line out. A write that fails (a closed stdout, a locale that cannot
        encode it) drops that line and nothing else: a reader that died here would
        leave its child blocked on a full pipe."""
        with self._out_lock:
            try:
                self._out(line)
            except Exception:  # noqa: BLE001
                pass

    def say(self, line: str) -> None:
        self._write(f"[worbus] {line}")

    def _pump(self, child: Child, stream) -> None:
        prefix = f"[{child.name}] "
        while True:
            raw = stream.readline(LINE_CAP)
            if not raw:
                break
            dropped = -1
            if len(raw) >= LINE_CAP and not raw.endswith(b"\n"):
                dropped = 0                # longer than the cap: drain the rest of it
                while True:
                    more = stream.readline(LINE_CAP)
                    if not more:
                        break
                    dropped += len(more)
                    if more.endswith(b"\n"):
                        dropped -= 1
                        break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            marker = ""
            if dropped >= 0:
                line, tail = _trim_cut(line)
                marker = ("" if not line or line[-1].isspace() else " ") + (
                          f"[... line cut by the Worbus launcher at {LINE_CAP // 1024} KB: "
                          f"{dropped + len(tail.encode('utf-8', 'replace'))} more bytes not shown]")
            try:
                line = redact(line)
            except Exception:  # noqa: BLE001
                line = "(a line the redaction rule could not read was dropped)"
            self._write(prefix + line + marker)
        stream.close()

    def _event(self, kind: str, child: Child, detail: float = 0.0) -> None:
        self.events.append((self.clock.monotonic(), kind, child.name, detail))

    # -- lifecycle ----------------------------------------------------------- #
    def request_stop(self) -> None:
        """Safe from a signal handler: one attribute store, no lock."""
        self._stop_requested = True

    def _stopping(self) -> bool:
        return self._stop_requested

    def _start(self, child: Child) -> None:
        try:
            child.proc = subprocess.Popen(
                child.argv, env=child.env, cwd=child.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Its own session: a terminal's Ctrl-C reaches the launcher alone,
                # which then stops the children in order.
                start_new_session=True)
        except OSError as e:
            child.proc = None
            self.say(f"{child.name} could not start: {type(e).__name__}: {e.strerror or e}")
            self._exited(child, None)
            return
        child.started_at = self.clock.monotonic()
        child.restart_at = None
        child.reader = threading.Thread(target=self._pump, args=(child, child.proc.stdout),
                                        name=f"pump-{child.name}", daemon=True)
        child.reader.start()
        self._event("start", child)

    def _exited(self, child: Child, status: Optional[int]) -> None:
        now = self.clock.monotonic()
        ran = now - child.started_at if status is not None else 0.0
        if child.reader is not None:
            child.reader.join(2)       # its last lines before ours
        self._event("exit", child, float(status if status is not None else -1))
        child.proc = None
        child.exits = [t for t in child.exits if now - t < CRASH_WINDOW] + [now]
        how = (f"exited with status {status}" if status is not None and status >= 0
               else f"was killed by signal {-status}" if status is not None
               else "did not start")
        if len(child.exits) >= CRASH_LIMIT:
            self.failed = child.name
            self.say(f"{child.name} {how}: {CRASH_LIMIT} exits within "
                     f"{int(CRASH_WINDOW // 60)} minutes; stopping the Worbus Server")
            self._stop_requested = True
            return
        child.quick_exits = 1 if ran >= STABLE_RUN else child.quick_exits + 1
        delay = min(BACKOFF_CAP, BACKOFF_FIRST * 2 ** (child.quick_exits - 1))
        delay = int(delay) if float(delay).is_integer() else delay
        child.restart_at = now + delay
        self._event("restart", child, delay)
        self.say(f"{child.name} {how}; restarting in {delay} s "
                 f"(exit {len(child.exits)} within {int(CRASH_WINDOW // 60)} minutes; "
                 f"{CRASH_LIMIT} stop the server)")

    def run(self) -> int:
        for child in self.children:
            self._start(child)
            if self._stopping():
                break
        while not self._stopping():
            for child in self.children:
                if self._stopping():
                    break
                if child.proc is not None:
                    status = child.proc.poll()
                    if status is not None:
                        self._exited(child, status)
                elif child.restart_at is not None and self.clock.monotonic() >= child.restart_at:
                    self._start(child)
            if not self._stopping():
                self.clock.sleep(TICK)
        self._shutdown()
        if self.failed:
            self.say(f"stopped: {self.failed} exited {CRASH_LIMIT} times within "
                     f"{int(CRASH_WINDOW // 60)} minutes (its lines above say why)")
            return 1
        return 0

    def _shutdown(self) -> None:
        """lb-bot first, the hub last; up to STOP_WAIT each, then SIGKILL."""
        for child in reversed(self.children):
            child.restart_at = None
            proc = child.proc
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    self._event("exit", child, float(proc.returncode))
                continue
            self.say(f"stopping {child.name}")
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._event("term", child)
            deadline = self.clock.monotonic() + STOP_WAIT
            while proc.poll() is None and self.clock.monotonic() < deadline:
                self.clock.sleep(0.1)
            if proc.poll() is None:
                self.say(f"{child.name} did not stop within {int(STOP_WAIT)} s; killing it")
                proc.kill()
                self._event("kill", child)
                proc.wait()
            else:
                self._event("exit", child, float(proc.returncode))
            if child.reader is not None:
                child.reader.join(2)


def _trim_cut(text: str) -> tuple[str, str]:
    """A line cut at the cap may end inside a credential (`https://bob:hun`,
    `"password":"hun`), which no rule then recognises: drop that last unfinished
    piece, back to a space or one of `"',;&`. Returns (kept, dropped)."""
    k = len(text)
    while k > 0 and not (text[k - 1].isspace() or text[k - 1] in _CUT_KEEPS):
        k -= 1
    return text[:k], text[k:]


def _stdout_line(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
def version_line(env: dict) -> Optional[str]:
    v = str(env.get("WORBUS_VERSION") or "").strip()
    if not v:
        return None
    c = str(env.get("WORBUS_COMMIT") or "").strip()[:7]
    return f"Worbus Server {v} ({c})" if c else f"Worbus Server {v}"


def main(argv: list[str], *, environ: Optional[dict] = None, clock=None,
         out: Optional[Callable[[str], None]] = None, install_signals: bool = True) -> int:
    global LAST_SUPERVISOR
    env = dict(os.environ if environ is None else environ)
    emit = out or _stdout_line

    def say(line: str) -> None:
        emit(f"[worbus] {line}")

    if argv:
        say("worbus_launch.py takes no arguments; every setting comes from the environment")
        return 2
    first = version_line(env)
    say(first or "Worbus Server (no WORBUS_VERSION: a build from source)")
    root = Path(str(env.get("WORBUS_DATA") or "").strip() or "/data")
    secret_file = root / "hub" / SECRET_NAME
    try:
        for sub in ("hub", "lb-bot"):
            folder = root / sub
            folder.mkdir(parents=True, exist_ok=True)
            # Both halves write here; a folder this uid cannot search and write is
            # refused now, by name, not later as a child's crash loop.
            if not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(folder))
        # Inside the try: Path.exists() raises PermissionError on EACCES (3.12).
        had_secret_file = secret_file.exists()
        envs = fill_env(env, root)
    except ConfigRefusal as e:
        say(f"refusing to start: {e}")
        return 1
    except OSError as e:
        where = e.filename if e.filename else root
        say(f"refusing to start: the data folder {where} cannot be read or written by this "
            f"user ({type(e).__name__}: {e.strerror or e}). Check the volume's owner and the "
            "compose `user:`.")
        return 1
    for half in ("hub", "lb-bot"):
        names = filled_names(env, envs[half])
        say(f"filled for {half}: {', '.join(names) if names else 'nothing (all set)'}")
    if _set(env, "HUB_TOKEN") or _set(env, "LB_BOT_HUB_TOKEN"):
        say("service secret: from the environment")
    else:
        say(f"service secret: {'read from' if had_secret_file else 'minted into'} {secret_file}")
    hub_dir = str(env.get("WORBUS_HUB_DIR") or "").strip() or "/app/hub"
    lb_dir = str(env.get("WORBUS_LBBOT_DIR") or "").strip() or "/app/lb-bot"
    for half in ("hub", "lb-bot"):
        envs[half].setdefault("PYTHONUNBUFFERED", "1")
    py = sys.executable or "python"
    children = [
        Child("hub", [py, os.path.join(hub_dir, "hub.py")], envs["hub"], hub_dir),
        Child("lb-bot", [py, "-u", os.path.join(lb_dir, "listenbrainz_bot.py")],
              envs["lb-bot"], lb_dir),
    ]
    sup = Supervisor(children, clock=clock, out=emit)
    LAST_SUPERVISOR = sup
    if install_signals:
        def on_signal(signum, _frame):
            sup.request_stop()
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
