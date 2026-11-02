#!/usr/bin/env python3
"""
The hub refuses its example secrets (B-303, bug run H1).

`.env.example` ships `HUB_TOKEN=change-me-…` and `PREVIEW_SECRET=change-me-…`. A
copied, unedited file used to start a hub whose administrative token is printed in a
public repository (and lb-bot P1 accepts that token as its trusted service caller).
Start-up now refuses a token that starts with `change-me`, and, when `PREVIEW_URL` is
set, a `PREVIEW_SECRET` that is empty or starts with `change-me`. It never refuses a
value for being short: the owner's token is 32 characters, and whatever its owner chose
is a real token.

Two halves:
  - in process: `startup_config_refusal`, the pure decision, over every case;
  - subprocess: `hub.py` itself, so the decision is proved wired into start-up — the
    placeholder cases exit non-zero with the one line, the 32-character token binds.

Exits non-zero on failure.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("HUB_TOKEN", "test-token")
HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB_DIR)

import hub  # noqa: E402

EXAMPLE_TOKEN = "change-me-to-a-long-random-secret"           # hub/.env.example
EXAMPLE_PREVIEW = "change-me-to-a-different-long-random-secret"  # hub/.env.example
REAL_32 = "0123456789abcdef0123456789abcdef"                  # 32 chars, like the owner's
TOKEN_LINE = ("HUB_TOKEN is still the example value; set your own "
              "(for example `openssl rand -hex 16`).")
PREVIEW_LINE = ("PREVIEW_SECRET is still the example value; set your own "
                "(for example `openssl rand -hex 16`).")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_pure() -> list[str]:
    failures: list[str] = []
    r = hub.startup_config_refusal

    def expect(label, got, want_none=False, want_line=None, want_prefix=None):
        if want_none:
            if got is not None:
                failures.append(f"{label}: refused a good config: {got!r}")
            return
        if got is None:
            failures.append(f"{label}: accepted, must be refused")
            return
        if want_line is not None and got != want_line:
            failures.append(f"{label}: wrong line {got!r}")
        if want_prefix is not None and not got.startswith(want_prefix):
            failures.append(f"{label}: wrong message {got!r}")

    # HUB_TOKEN
    expect("example token", r(EXAMPLE_TOKEN, "0.0.0.0", False, "", ""), want_line=TOKEN_LINE)
    expect("any change-me token", r("change-me", "0.0.0.0", False, "", ""), want_line=TOKEN_LINE)
    expect("CHANGE-ME, padded", r("  CHANGE-ME-x ", "0.0.0.0", False, "", ""),
           want_line=TOKEN_LINE)
    expect("example token with the insecure flag",
           r(EXAMPLE_TOKEN, "127.0.0.1", True, "", ""), want_line=TOKEN_LINE)
    expect("empty token", r("", "0.0.0.0", False, "", ""), want_prefix="HUB_TOKEN is not set.")
    expect("empty token, insecure on a reachable host", r("", "0.0.0.0", True, "", ""),
           want_prefix="HUB_ALLOW_INSECURE_NO_AUTH is set")
    expect("empty token, insecure on loopback", r("", "127.0.0.1", True, "", ""),
           want_none=True)
    expect("32-character token", r(REAL_32, "0.0.0.0", False, "", ""), want_none=True)
    expect("short token is not refused", r("t", "0.0.0.0", False, "", ""), want_none=True)
    expect("token containing change-me later", r("my-change-me", "0.0.0.0", False, "", ""),
           want_none=True)
    # review finding 2: whitespace only is empty, refused the same way
    expect("whitespace-only token", r("   ", "0.0.0.0", False, "", ""),
           want_prefix="HUB_TOKEN is not set.")
    expect("whitespace-only token, insecure on a reachable host",
           r(" \t\n", "0.0.0.0", True, "", ""), want_prefix="HUB_ALLOW_INSECURE_NO_AUTH is set")
    # review finding 3: `docker run --env-file` keeps quotes literally
    expect("double-quoted example token", r(f'"{EXAMPLE_TOKEN}"', "0.0.0.0", False, "", ""),
           want_line=TOKEN_LINE)
    expect("single-quoted example token, padded",
           r(f"  '{EXAMPLE_TOKEN}' ", "0.0.0.0", False, "", ""), want_line=TOKEN_LINE)
    expect("quoted real token", r(f'"{REAL_32}"', "0.0.0.0", False, "", ""), want_none=True)
    expect("unmatched quote before change-me", r('"change-me', "0.0.0.0", False, "", ""),
           want_none=True)

    # PREVIEW_SECRET, only when PREVIEW_URL is set
    url = "http://navi-connect-preview:4792"
    expect("example preview secret", r(REAL_32, "0.0.0.0", False, url, EXAMPLE_PREVIEW),
           want_line=PREVIEW_LINE)
    expect("empty preview secret", r(REAL_32, "0.0.0.0", False, url, ""),
           want_prefix="PREVIEW_SECRET is not set but PREVIEW_URL is")
    expect("32-character preview secret", r(REAL_32, "0.0.0.0", False, url, REAL_32[::-1]),
           want_none=True)
    expect("example preview secret, PREVIEW_URL unset",
           r(REAL_32, "0.0.0.0", False, "", EXAMPLE_PREVIEW), want_none=True)
    expect("empty preview secret, PREVIEW_URL unset", r(REAL_32, "0.0.0.0", False, "", ""),
           want_none=True)
    expect("whitespace-only preview secret", r(REAL_32, "0.0.0.0", False, url, "  "),
           want_prefix="PREVIEW_SECRET is not set but PREVIEW_URL is")
    expect("quoted example preview secret",
           r(REAL_32, "0.0.0.0", False, url, f"'{EXAMPLE_PREVIEW}'"), want_line=PREVIEW_LINE)
    return failures


def _env(tmp: str, port: int, **over: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HUB_", "PREVIEW_", "AUDIOMUSE_", "LBBOT_", "NAVIDROME_"))}
    env.update(HUB_HOST="127.0.0.1", HUB_PORT=str(port), HUB_HEALTH_PORT=str(_free_port()),
               HUB_STATE=os.path.join(tmp, "state.json"), HUB_MIRROR_PLAYQUEUE="false")
    env.update(over)
    return env


def _refused(tmp: str, label: str, line: str, **over: str) -> list[str]:
    """Refused before anything writes: non-zero exit, the line, and no state file
    (HUB_STATE or any side file beside it: .history, .stations, .bak, .tmp)."""
    env = _env(tmp, _free_port(), **over)
    state = env["HUB_STATE"]
    try:
        proc = subprocess.run([sys.executable, "hub.py"], env=env,
                              cwd=HUB_DIR, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:  # run() has killed it
        return [f"subprocess {label}: still running after 8 s, it started instead of refusing"]
    out = (proc.stdout + proc.stderr).strip().splitlines()
    fails = []
    if proc.returncode == 0:
        fails.append(f"subprocess {label}: exit 0, must be non-zero")
    if line not in out:
        fails.append(f"subprocess {label}: the line is missing; output {out!r}")
    written = [p for p in os.listdir(tmp) if p.startswith(os.path.basename(state))]
    if written:
        fails.append(f"subprocess {label}: a refused start wrote {written!r}")
    return fails


def _starts(tmp: str, label: str, **over: str) -> list[str]:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "hub.py"], env=_env(tmp, port, **over),
                            cwd=HUB_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                return [f"subprocess {label}: exited {proc.returncode}: "
                        f"{proc.stdout.read().strip()!r}"]
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return []
            except OSError:
                time.sleep(0.2)
        return [f"subprocess {label}: never bound 127.0.0.1:{port}"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def check_subprocess() -> list[str]:
    failures: list[str] = []
    # A fresh directory per refusal, so "no state file" is about that start alone.
    with tempfile.TemporaryDirectory() as tmp:
        failures += _refused(tmp, "example HUB_TOKEN", TOKEN_LINE, HUB_TOKEN=EXAMPLE_TOKEN)
    with tempfile.TemporaryDirectory() as tmp:
        failures += _refused(tmp, "example PREVIEW_SECRET", PREVIEW_LINE, HUB_TOKEN=REAL_32,
                             PREVIEW_URL="http://127.0.0.1:9", PREVIEW_SECRET=EXAMPLE_PREVIEW)
    with tempfile.TemporaryDirectory() as tmp:
        # Worbus P2: with the service account it requires (nothing calls it here).
        failures += _starts(tmp, "32-character HUB_TOKEN", HUB_TOKEN=REAL_32,
                            NAVIDROME_URL="http://127.0.0.1:9", HUB_ND_USER="owner",
                            HUB_ND_PASS="test-pass")
    return failures


def check_env_example() -> list[str]:
    """H2: the shipped .env.example, read the way docker's env_file reads it (a
    `#` line is a comment), turns on no optional service, and its HUB_TOKEN is the
    placeholder start-up refuses."""
    failures: list[str] = []
    env: dict[str, str] = {}
    with open(os.path.join(HUB_DIR, ".env.example"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # HUB_ND_USER/PASS: the AudioMuse proxy forwards them whenever set (review finding 6)
    for key in ("PREVIEW_URL", "PREVIEW_PUBLIC_URL", "AUDIOMUSE_URL", "NAVIDROME_URL",
                "HUB_ND_USER", "HUB_ND_PASS"):
        if env.get(key):
            failures.append(f".env.example sets {key}={env[key]!r}; it must be commented out")
    if hub.startup_config_refusal(env.get("HUB_TOKEN", ""), "0.0.0.0", False,
                                  env.get("PREVIEW_URL", ""),
                                  env.get("PREVIEW_SECRET", "")) != TOKEN_LINE:
        failures.append(".env.example's HUB_TOKEN is not refused as the example value")
    # Worbus P2, ruling R13: the service account's lines stay commented, and each
    # placeholder, uncommented as it is, is refused by name (never by value).
    placeholders: dict[str, str] = {}
    with open(os.path.join(HUB_DIR, ".env.example"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^#\s*(NAVIDROME_URL|HUB_ND_USER|HUB_ND_PASS)=(\S+)\s*$", line.strip())
            if m:
                placeholders[m.group(1)] = m.group(2)
    if set(placeholders) != {"NAVIDROME_URL", "HUB_ND_USER", "HUB_ND_PASS"}:
        failures.append(f".env.example's commented service account lines: {sorted(placeholders)}")
    good = {"navidrome_url": "http://navidrome:4533", "nd_user": "owner", "nd_pass": "pw"}
    kw = {"NAVIDROME_URL": "navidrome_url", "HUB_ND_USER": "nd_user", "HUB_ND_PASS": "nd_pass"}
    for name, value in placeholders.items():
        for v in (value, f'"{value}"', value.upper()):
            got = hub.startup_config_refusal(REAL_32, "0.0.0.0", False, "", "",
                                             **{**good, kw[name]: v})
            if got != f"{name} is still the example value; set your own.":
                failures.append(f".env.example's {name} placeholder ({v!r}) is not refused "
                                f"by name: {got!r}")
    if hub.startup_config_refusal(REAL_32, "0.0.0.0", False, "", "", **good) is not None:
        failures.append("a filled-in service account is refused")
    return failures


def main() -> int:
    failures = check_pure() + check_env_example() + check_subprocess()
    for f in failures:
        print(f"FAIL - {f}")
    if failures:
        return 1
    print("PASS - startup config: example-token-refused/empty-token-refused/"
          "insecure-loopback-only/32-char-token-accepted/short-not-refused/"
          "example-or-empty-preview-secret-refused-with-PREVIEW_URL/"
          "preview-secret-ignored-without-PREVIEW_URL/"
          "whitespace-only-is-empty/quoted-placeholder-refused/"
          "subprocess-exit-and-line-no-state-file/"
          "subprocess-32-char-binds/env-example-enables-no-service-or-nd-creds/"
          "env-example-service-account-placeholders-refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
