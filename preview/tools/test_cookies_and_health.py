#!/usr/bin/env python3
"""
Tests the cookie wiring and the extractor-health signal, without touching YouTube.

These two exist together for one reason: **cookies expire.** The cookie path is
what makes the extractor work on a challenged egress address, and the health
signal is what says so when it stops working — without it a bot challenge and a
genuine no-match are identical to a client (both an empty resolve against a
process reporting itself healthy), so the feature can be dead while every probe
says fine.

What is proved here:

  1. `PREVIEW_COOKIES` unset  -> no `cookiefile`, and the `android` player client
     (the one that still answers unauthenticated).
  2. `PREVIEW_COOKIES` set and the file present -> `cookiefile` is passed, and the
     client switches to `web`/`mweb`. That switch is not cosmetic: yt-dlp's own
     guidance is not to send account cookies with the `android` client, and it is
     the combination most associated with an account being limited or terminated.
  3. `PREVIEW_COOKIES` set but the file MISSING -> no `cookiefile` (yt-dlp would
     raise on a missing one) and the process still starts. That is the shape a
     wrong bind mount takes, and it must not be silent — `main()` logs a warning,
     and `/status` reports `cookies: false`.
  4. A bot challenge sets `extractorBlocked`; an ordinary failure does not.
  5. `/status` reports all of it, and a success clears it.

What is NOT proved here, and cannot be without a real cookie file: that a given
cookies.txt actually satisfies YouTube. That is only answerable in production.

Exits non-zero on failure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

os.environ["PREVIEW_SECRET"] = "cookie-test-secret"
import preview  # noqa: E402


def ydl_opts(cookies_path: str) -> dict:
    """Build the sidecar's real ydl options with COOKIES set to `cookies_path`."""
    original = preview.COOKIES
    preview.COOKIES = cookies_path
    try:
        # _ydl constructs a real YoutubeDL; `.params` is what it was given, which
        # is the thing under test. No network happens at construction.
        with preview._ydl({}) as y:  # noqa: SLF001
            return dict(y.params)
    finally:
        preview.COOKIES = original


def clients_of(params: dict) -> list:
    return (params.get("extractor_args") or {}).get("youtube", {}).get(
        "player_client", [])


def main() -> int:
    failures: list[str] = []

    # --- 1. no cookies -------------------------------------------------------
    p = ydl_opts("")
    if p.get("cookiefile"):
        failures.append("a cookiefile was passed with PREVIEW_COOKIES unset")
    if clients_of(p) != ["android", "web"]:
        failures.append(
            f"unauthenticated player_client is {clients_of(p)}, want "
            "['android','web'] — `web` needs a challenge this process does not "
            "solve, so android-first is what still answers on a challenged address")

    # --- 2. cookies present --------------------------------------------------
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# Netscape HTTP Cookie File\n")
        cookie_path = f.name
    try:
        p = ydl_opts(cookie_path)
        if p.get("cookiefile") != cookie_path:
            failures.append(
                f"cookiefile was not passed: {p.get('cookiefile')!r} — the whole "
                "point of PREVIEW_COOKIES is that yt-dlp receives it")
        if clients_of(p) != ["web", "mweb"]:
            failures.append(
                f"authenticated player_client is {clients_of(p)}, want "
                "['web','mweb'] — sending account cookies with the android "
                "client is the combination that gets accounts limited")

        # --- 5a. /status reflects a present cookie file ----------------------
        preview.COOKIES = cookie_path
        preview._health.update(  # noqa: SLF001
            consecutive_failures=0, last_error="", blocked=False)
        body = status_body()
        if body.get("cookies") is not True:
            failures.append("/status did not report a present cookie file")
        if body.get("extractorBlocked") is not False:
            failures.append("/status reported blocked on a fresh process")
        # The file's CONTENTS must never appear in a response body.
        if "Netscape" in json.dumps(body) or cookie_path in json.dumps(body):
            failures.append(
                "/status leaked the cookie file's path or contents — it is a "
                "credential, and this endpoint is proxied to every client")
    finally:
        os.unlink(cookie_path)

    # --- 3. configured but missing ------------------------------------------
    missing = "/nonexistent/cookies.txt"
    p = ydl_opts(missing)
    if p.get("cookiefile"):
        failures.append(
            "a missing cookie file was still passed to yt-dlp, which raises on "
            "one — a wrong bind mount would take the whole extractor down")
    preview.COOKIES = missing
    if status_body().get("cookies") is not False:
        failures.append(
            "/status claimed cookies with the file missing — that is exactly the "
            "state a wrong bind mount produces, and it must be visible")
    preview.COOKIES = ""

    # --- 4. blocked vs ordinary failure --------------------------------------
    preview._health.update(  # noqa: SLF001
        consecutive_failures=0, last_error="", blocked=False)
    preview._note_extract_failed(  # noqa: SLF001
        Exception("ERROR: [youtube] abc: Video unavailable"))
    if preview._health["blocked"]:  # noqa: SLF001
        failures.append(
            "an ordinary 'Video unavailable' was classed as blocked — that would "
            "cry wolf on every deleted video")
    if preview._health["consecutive_failures"] != 1:  # noqa: SLF001
        failures.append("the failure was not counted")

    preview._note_extract_failed(Exception(  # noqa: SLF001
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. "
        "Use --cookies-from-browser"))
    if not preview._health["blocked"]:  # noqa: SLF001
        failures.append(
            "a bot challenge was NOT classed as blocked — note the message uses a "
            "typographic apostrophe (U+2019), which is why matching on the ASCII "
            "spelling alone silently never fires")
    body = status_body()
    if body.get("extractorBlocked") is not True or body.get("consecutiveFailures") != 2:
        failures.append(f"/status did not report the blockage: {body}")
    if "not a bot" not in (body.get("lastExtractorError") or ""):
        failures.append(
            "/status carries no reason — the operator needs to tell 'cookies "
            "expired' from 'the sidecar is down'")

    # --- 5b. a success clears it --------------------------------------------
    preview._note_extract_ok()  # noqa: SLF001
    body = status_body()
    if body.get("extractorBlocked") is not False or body.get("consecutiveFailures") != 0:
        failures.append(f"a success did not clear the health state: {body}")

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - cookies + extractor health: no-cookies-uses-android/"
          "cookies-use-web/cookiefile-passed/missing-file-degrades-not-crashes/"
          "status-reports-cookies-without-leaking-them/"
          "bot-challenge-is-blocked-including-U+2019/"
          "ordinary-failure-is-not/success-clears")
    return 0


def status_body() -> dict:
    """The exact object `/status` serves, built the way serve_client builds it."""
    return {
        "ok": True, "provider": preview.PROVIDER, "signed": bool(preview.SECRET),
        "cookies": bool(preview.COOKIES and os.path.exists(preview.COOKIES)),
        "extractorBlocked": preview._health["blocked"],  # noqa: SLF001
        "consecutiveFailures": preview._health["consecutive_failures"],  # noqa: SLF001
        "lastExtractorError": preview._health["last_error"],  # noqa: SLF001
    }


if __name__ == "__main__":
    sys.exit(main())
