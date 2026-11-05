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
    # R19 (B-323): the example ships the code's default, 7; 0 would keep every
    # device, and with it every device sign-in, forever.
    if env.get("HUB_DEVICE_TTL_DAYS") != "7":
        failures.append(f".env.example sets HUB_DEVICE_TTL_DAYS={env.get('HUB_DEVICE_TTL_DAYS')!r}, "
                        "not the default 7")
    return failures


def check_device_ttl() -> list[str]:
    """R19: HUB_DEVICE_TTL_DAYS unset or blank is the default, 7 (a blank line in a
    copied .env read as 0, keeping devices and their sign-ins forever); an
    explicit 0 still means keep."""
    failures: list[str] = []
    for label, value, want in (("unset", None, 7.0), ("blank", "", 7.0),
                               ("spaces", "  ", 7.0), ("0", "0", 0.0), ("3", "3", 3.0)):
        env = {k: v for k, v in os.environ.items() if k != "HUB_DEVICE_TTL_DAYS"}
        if value is not None:
            env["HUB_DEVICE_TTL_DAYS"] = value
        got = subprocess.run([sys.executable, "-c", "import hub; print(hub.DEVICE_TTL_DAYS)"],
                             cwd=HUB_DIR, env=env, capture_output=True, text=True, timeout=30)
        out = (got.stdout.strip().splitlines() or [""])[-1]
        if got.returncode != 0 or out != str(want):
            failures.append(f"HUB_DEVICE_TTL_DAYS {label}: DEVICE_TTL_DAYS is {out!r} "
                            f"(rc {got.returncode}), not {want}")
    return failures


def check_hub_name() -> list[str]:
    """Worbus P3, H1: HUB_NAME is optional, trimmed, 1 to 40 characters, default
    `Worbus Server`; longer, or blank while set, is refused naming the setting."""
    failures: list[str] = []
    r = hub.startup_config_refusal
    base = (REAL_32, "0.0.0.0", False, "", "")
    nd = dict(navidrome_url="http://127.0.0.1:9", nd_user="owner", nd_pass="p")

    def check(label, ok, got):
        if not ok:
            failures.append(f"{label}: {got!r}")

    # hub_name_too_long_refused
    for label, value in (("41 characters", "x" * 41), ("41 after trimming", "  " + "y" * 41 + " ")):
        got = r(*base, **nd, hub_name=value)
        check(f"hub_name_too_long_refused ({label})",
              got is not None and got.startswith("HUB_NAME") and "40" in got, got)
    check("hub_name_too_long_refused (exactly 40 is fine)",
          r(*base, **nd, hub_name="z" * 40) is None, r(*base, **nd, hub_name="z" * 40))
    check("hub_name_too_long_refused (40 plus padding is fine)",
          r(*base, **nd, hub_name="  " + "z" * 40 + "  ") is None, "refused")
    # hub_name_blank_refused
    for label, value in (("empty", ""), ("spaces", "   "), ("tab", "\t"), ("empty quotes", '""')):
        got = r(*base, **nd, hub_name=value)
        check(f"hub_name_blank_refused ({label})",
              got is not None and got.startswith("HUB_NAME"), got)
    # hub_name_default
    check("hub_name_default (unset is fine)", r(*base, **nd) is None, r(*base, **nd))
    check("hub_name_default (resolves to Worbus Server)",
          hub.resolve_hub_name(None) == "Worbus Server", hub.resolve_hub_name(None))
    check("hub_name_default (a set name is trimmed)",
          hub.resolve_hub_name("  Home  ") == "Home", hub.resolve_hub_name("  Home  "))
    check("hub_name_default (40 characters kept whole)",
          hub.resolve_hub_name("é" * 40) == "é" * 40, "length")
    return failures


def check_public_url() -> list[str]:
    """Worbus P3, H2: HUB_PUBLIC_URL, normalised and refused."""
    failures: list[str] = []
    n = hub.normalise_public_url

    def same(label, got, want):
        if got != want:
            failures.append(f"{label}: {got!r}, want {want!r}")

    def refused(label, value):
        try:
            got = n(value)
        except hub.ConfigRefusal as e:
            msg = str(e)
            if not msg.startswith("HUB_PUBLIC_URL"):
                failures.append(f"{label}: the refusal does not name the setting: {msg!r}")
            if "\n" in msg:
                failures.append(f"{label}: the refusal is not one line: {msg!r}")
            return
        failures.append(f"{label}: accepted as {got!r}, must be refused")

    # unset
    same("unset", n(None), None)
    same("blank is unset", n("   "), None)
    # https_public_url_becomes_wss
    same("https_public_url_becomes_wss (wss kept)", n("wss://hub.example.org"), "wss://hub.example.org")
    same("https_public_url_becomes_wss (https becomes wss)", n("https://hub.example.org"), "wss://hub.example.org")
    same("https_public_url_becomes_wss (scheme case)", n("HTTPS://hub.example.org"), "wss://hub.example.org")
    same("https_public_url_becomes_wss (port kept)", n("https://hub.example.org:8443"), "wss://hub.example.org:8443")
    same("https_public_url_becomes_wss (host lower-cased)", n("wss://Hub.Example.ORG"), "wss://hub.example.org")
    same("https_public_url_becomes_wss (quoted (docker env-file))", n("'wss://hub.example.org'"), "wss://hub.example.org")
    same("https_public_url_becomes_wss (ipv6 host)", n("wss://[2001:db8::1]:8443"), "wss://[2001:db8::1]:8443")
    # trailing_connect_and_slash_dropped
    same("trailing_connect_and_slash_dropped (trailing slash)", n("wss://hub.example.org/"), "wss://hub.example.org")
    same("trailing_connect_and_slash_dropped (trailing /connect)", n("wss://hub.example.org/connect"), "wss://hub.example.org")
    same("trailing_connect_and_slash_dropped (trailing /connect/)", n("wss://hub.example.org/connect/"), "wss://hub.example.org")
    same("trailing_connect_and_slash_dropped (https with /connect)", n("https://hub.example.org:8443/connect"),
         "wss://hub.example.org:8443")
    same("trailing_connect_and_slash_dropped (padding)", n("  wss://hub.example.org/connect \n"), "wss://hub.example.org")
    # path_kept
    same("path_kept (path kept)", n("wss://example.org/worbus"), "wss://example.org/worbus")
    same("path_kept (path kept, slash dropped)", n("https://example.org/a/b/"), "wss://example.org/a/b")
    same("path_kept (path kept, /connect dropped)", n("wss://example.org/worbus/connect"),
         "wss://example.org/worbus")
    same("path_kept (path case kept)", n("wss://example.org/Worbus"), "wss://example.org/Worbus")
    same("path_kept (/connect only as the last segment)", n("wss://example.org/connectx"),
         "wss://example.org/connectx")
    # public_url_ws_refused / http / no host / placeholder
    refused("public_url_ws_refused", "ws://hub.example.org")
    refused("public_url_ws_refused (upper case)", "WS://hub.example.org")
    refused("public_url_http_refused", "http://hub.example.org")
    refused("public_url_malformed_refused (other scheme)", "ftp://hub.example.org")
    refused("public_url_malformed_refused (no scheme)", "hub.example.org")
    refused("public_url_no_host_refused (scheme only)", "wss://")
    refused("public_url_no_host_refused (path only)", "wss:///worbus")
    refused("public_url_no_host_refused (port only)", "https://:8443")
    refused("public_url_malformed_refused (bad port)", "wss://hub.example.org:notaport")
    refused("public_url_malformed_refused (credentials in the url)", "wss://user:pw@hub.example.org")
    refused("public_url_malformed_refused (query)", "wss://hub.example.org/?x=1")
    refused("public_url_malformed_refused (spaces)", "wss://hub .example.org")
    refused("public_url_placeholder_refused", "wss://hub.example.com")
    refused("public_url_placeholder_refused (https form)", "https://hub.example.com/")
    refused("public_url_placeholder_refused (/connect, upper case)", "WSS://Hub.Example.com/connect")
    # The refusal never repeats the whole value (it names the setting).
    try:
        n("ws://hub.example.org/with-a-very-long-secret-looking-path-1234567890")
    except hub.ConfigRefusal as e:
        if "1234567890" in str(e):
            failures.append(f"the refusal repeats the value: {e}")
    # Through startup_config_refusal: a refusal is returned, not raised.
    r = hub.startup_config_refusal
    nd = dict(navidrome_url="http://127.0.0.1:9", nd_user="owner", nd_pass="p")
    got = r(REAL_32, "0.0.0.0", False, "", "", **nd, public_url="ws://hub.example.org")
    if not (got or "").startswith("HUB_PUBLIC_URL"):
        failures.append(f"startup_config_refusal let ws:// through: {got!r}")
    if r(REAL_32, "0.0.0.0", False, "", "", **nd, public_url="https://hub.example.org") is not None:
        failures.append("startup_config_refusal refused a good HUB_PUBLIC_URL")
    return failures


def check_discovery_subprocess() -> list[str]:
    """The same refusals, wired into start-up: non-zero exit, one line naming the
    setting, no state file."""
    failures: list[str] = []
    common = dict(HUB_TOKEN=REAL_32, NAVIDROME_URL="http://127.0.0.1:9", HUB_ND_USER="owner",
                  HUB_ND_PASS="test-pass")
    cases = (("HUB_NAME too long", dict(HUB_NAME="n" * 41),
              "HUB_NAME is longer than 40 characters; shorten it."),
             ("HUB_NAME blank", dict(HUB_NAME="  "),
              "HUB_NAME is set but blank; give it a name of 1 to 40 characters, or unset it "
              "for the default ('Worbus Server')."),
             ("HUB_PUBLIC_URL ws://", dict(HUB_PUBLIC_URL="ws://hub.example.org"),
              "HUB_PUBLIC_URL must be wss:// (or https://): ws:// would send the device "
              "secret across the internet in plain text."),
             ("HUB_PUBLIC_URL placeholder", dict(HUB_PUBLIC_URL="wss://hub.example.com"),
              "HUB_PUBLIC_URL is still the example value; set your own address, or leave "
              "it unset."))
    for label, over, line in cases:
        with tempfile.TemporaryDirectory() as tmp:
            failures += _refused(tmp, label, line, **common, **over)
    with tempfile.TemporaryDirectory() as tmp:
        failures += _starts(tmp, "HUB_NAME and HUB_PUBLIC_URL good", **common,
                            HUB_NAME="Home", HUB_PUBLIC_URL="https://hub.example.org")
    return failures


def main() -> int:
    failures = check_pure() + check_env_example() + check_device_ttl() + check_subprocess()\
        + check_hub_name() + check_public_url() + check_discovery_subprocess()
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
          "env-example-service-account-placeholders-refused/"
          "device-ttl-example-7-blank-is-default/"
          "hub-name-too-long-and-blank-refused-default-and-trim/"
          "public-url-https-becomes-wss-slash-and-connect-dropped-path-kept/"
          "public-url-ws-http-no-host-placeholder-refused/discovery-refusals-wired-into-start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
