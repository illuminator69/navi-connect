#!/usr/bin/env python3
"""
End-to-end test for the preview sidecar's control plane: `/preview/resolve` and
`/preview/status`.

Serves a stub sidecar and drives the real `PreviewProxy.handle`, for the things a
unit test of the route table cannot prove:

  - the route is reachable, and reaches the right upstream path;
  - only whitelisted params are forwarded (the whitelist IS the control);
  - the hub SIGNS the stream URL on the way out — the sidecar returns a bare id
    and no URL, so a resolve that arrives unsigned is a track that cannot play;
  - the signature verifies against the same HMAC the sidecar computes, and `exp`
    is in the FUTURE (a URL minted already-expired is the failure that looks
    exactly like a broken extractor);
  - a cached resolution is re-signed rather than served with its old signature —
    the resolution is cached for six hours and a capability lives six hours from
    its own minting, so caching the two together would serve dead URLs for the
    back half of every entry's life;
  - `{}` — "no preview found" — is a 200 that passes through untouched, never an
    error and never something the signer mangles;
  - `previewCastable` is false when PREVIEW_PUBLIC_URL is unset, and the probe
    still answers when the proxy is disabled entirely.

There is deliberately NO test here for proxying audio, because the hub
deliberately does not: see the `PreviewProxy` docstring.

Exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse

os.environ.setdefault("HUB_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub  # noqa: E402

HITS: list[str] = []
BLOCKED = [False]
SECRET = "preview-test-secret"
PUBLIC = "https://preview.example.test"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(self.path.partition("?")[2])
        if path == "/resolve":
            if (query.get("title") or [""])[0] == "nothing":
                body = {}  # the legitimate "no preview found" answer
            else:
                body = {
                    "id": "ext:yt:dQw4w9WgXcQ",
                    "title": "Song", "artist": "Band", "album": "Record",
                    "durationMs": 213000,
                    "imageUrl": "https://coverartarchive.org/x/front-500",
                    # The sidecar returns this EMPTY on purpose — the hub is what
                    # fills it in, which is how PREVIEW_SECRET never leaves the
                    # two servers.
                    "streamUrl": "",
                    "mime": "audio/webm", "provider": "yt", "confidence": 0.82,
                }
        elif path == "/status":
            body = {"ok": True, "provider": "yt",
                    "extractorBlocked": BLOCKED[0], "cookies": True,
                    "consecutiveFailures": 7 if BLOCKED[0] else 0,
                    # The sidecar reports this; the hub must NOT relay it — it is
                    # a diagnostic for the operator's log, not for every client.
                    "lastExtractorError": "Sign in to confirm you're not a bot"}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def sidecar_sign(track_id: str, exp: int) -> str:
    """The sidecar's own verification, reimplemented here rather than imported.

    Importing `preview.py` would make this test prove only that one function
    equals itself. Written out, it proves the two processes agree on the bytes
    that get signed — which is the actual contract, and the thing a refactor on
    either side would break.
    """
    return hmac.new(SECRET.encode(), f"{track_id}\n{exp}".encode(),
                    hashlib.sha256).hexdigest()


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    saved = (hub.PREVIEW_URL, hub.PREVIEW_PUBLIC_URL, hub.PREVIEW_SECRET,
             hub.PREVIEW_TTL)
    hub.PREVIEW_URL = f"http://127.0.0.1:{port}"
    hub.PREVIEW_PUBLIC_URL = PUBLIC
    hub.PREVIEW_SECRET = SECRET
    hub.PREVIEW_TTL = 6 * 3600
    proxy = hub.PreviewProxy()
    headers = {"Authorization": f"Bearer {hub.TOKEN}"}
    failures: list[str] = []

    def call(path: str):
        return asyncio.run(proxy.handle(None, path, headers, "GET"))

    try:
        # --- reachable, right upstream path, param whitelist ----------------
        HITS.clear()
        status, _h, body = call(
            "/preview/resolve?artist=Band&title=Song&album=Record"
            "&durationMs=213000&secret=leak")
        if status != 200:
            failures.append(f"/preview/resolve should answer 200, got {status}")
        if not HITS or not HITS[0].startswith("/resolve"):
            failures.append(f"/preview/resolve reached the wrong upstream path: {HITS}")
        if HITS and "secret" in HITS[0]:
            failures.append(
                "an un-whitelisted param was forwarded — the param list IS the "
                "control, and a passthrough defeats it")
        for want in ("artist=Band", "title=Song", "album=Record", "durationMs=213000"):
            if HITS and want not in HITS[0]:
                failures.append(f"{want!r} was not forwarded: {HITS[0]}")

        # --- the answer is queue-track-shaped, and SIGNED -------------------
        payload = json.loads(body) if status == 200 else {}
        if payload.get("id") != "ext:yt:dQw4w9WgXcQ":
            failures.append("the ext: id did not survive the proxy")
        stream_url = payload.get("streamUrl") or ""
        if not stream_url:
            failures.append(
                "streamUrl is empty — the sidecar deliberately returns a bare id "
                "and the hub is what signs it, so an unsigned answer is a track "
                "that cannot play")
        elif not stream_url.startswith(PUBLIC + "/stream?"):
            failures.append(
                f"streamUrl was not rewritten onto PREVIEW_PUBLIC_URL: {stream_url}")
        else:
            q = urllib.parse.parse_qs(stream_url.partition("?")[2])
            sig = (q.get("sig") or [""])[0]
            exp = int((q.get("exp") or ["0"])[0])
            tid = (q.get("id") or [""])[0]
            if tid != "ext:yt:dQw4w9WgXcQ":
                failures.append("the signed URL names a different track")
            if not hmac.compare_digest(sig, sidecar_sign(tid, exp)):
                failures.append(
                    "the signature does not verify against the sidecar's own "
                    "HMAC — the two processes disagree on what gets signed, and "
                    "every preview would be refused at playback")
            if exp <= int(time.time()):
                failures.append(
                    f"exp is not in the future ({exp}) — a capability minted "
                    "already-expired fails exactly like a broken extractor")

        # --- every queue-track field the hub's saved-queue whitelist keeps ---
        # This is why the resolve answer is shaped like a track at all: an `ext:`
        # track has to survive SQ_TRACK_FIELDS sanitisation, syncSavedQueues and
        # a state reload unchanged, or transfer and Continue Listening lose it.
        missing = [f for f in ("id", "title", "artist", "album", "durationMs",
                               "imageUrl", "streamUrl", "mime")
                   if not payload.get(f)]
        if missing:
            failures.append(f"the resolve answer is missing {missing} — an ext: "
                            "track must be queue-shaped to survive saved-queue "
                            "sanitisation")
        outside = [f for f in payload if f not in hub.Hub.SQ_TRACK_FIELDS
                   and f not in ("provider", "confidence")]
        if outside:
            failures.append(
                f"the resolve answer carries {outside}, which SQ_TRACK_FIELDS "
                "would silently drop on the way into a saved queue")

        # --- cached upstream, but RE-SIGNED per answer ----------------------
        before = len(HITS)
        _s, _h, again = call(
            "/preview/resolve?artist=Band&title=Song&album=Record"
            "&durationMs=213000&secret=leak")
        if len(HITS) != before:
            failures.append(
                "the second identical resolve went upstream — an ext: id is "
                "stable and this route is cached for PROXY_CACHE_TTL_LONG")
        second_url = json.loads(again).get("streamUrl") or ""
        if not second_url:
            failures.append(
                "a CACHED resolve came back unsigned — the signing has to happen "
                "after the cache, or every hit past PREVIEW_TTL serves a URL the "
                "sidecar refuses")
        q2 = urllib.parse.parse_qs(second_url.partition("?")[2])
        exp2 = int((q2.get("exp") or ["0"])[0])
        if exp2 <= int(time.time()):
            failures.append("the cached answer's exp is not in the future")
        if not hmac.compare_digest((q2.get("sig") or [""])[0],
                                   sidecar_sign((q2.get("id") or [""])[0], exp2)):
            failures.append("the cached answer's signature does not verify")

        # --- `{}` is an ANSWER ---------------------------------------------
        status, _h, body = call("/preview/resolve?artist=Band&title=nothing")
        if status != 200:
            failures.append(
                f"'no preview found' must be a 200, got {status} — it is a "
                "legitimate answer, on the same rule as lb-bot's strict=False chain")
        elif json.loads(body) != {}:
            failures.append(f"an empty resolve was mangled by the signer: {body!r}")

        # --- the probe, castable -------------------------------------------
        status, _h, body = call("/preview/status")
        probe = json.loads(body)
        if status != 200 or probe.get("configured") is not True:
            failures.append(f"/preview/status should report configured: {probe}")
        if probe.get("upstreamReachable") is not True:
            failures.append("a reachable sidecar was reported unreachable")
        if probe.get("previewCastable") is not True:
            failures.append(
                "previewCastable must be true with PREVIEW_PUBLIC_URL set")

        # --- ...and NOT castable without a public URL ------------------------
        hub.PREVIEW_PUBLIC_URL = ""
        proxy._cache.clear()  # noqa: SLF001 — the probe is cached like any route
        status, _h, body = call("/preview/status")
        probe = json.loads(body)
        if probe.get("previewCastable") is not False:
            failures.append(
                "previewCastable must be false without PREVIEW_PUBLIC_URL — both "
                "clients gate the cast refusal on it, and without that a transfer "
                "to a speaker plays silence instead of saying why")
        if probe.get("upstreamReachable") is not True:
            failures.append(
                "castability is a CONFIGURATION fact and reachability a health "
                "one; losing the public URL must not report the sidecar as down")

        # --- the extractor's own health rides on the probe -------------------
        # A bot challenge and a genuine no-match are otherwise identical to a
        # client: both are an empty resolve against a process reporting itself
        # healthy. Cookies expire, so this is the field that will say so.
        hub.PREVIEW_PUBLIC_URL = PUBLIC
        BLOCKED[0] = True
        proxy._cache.clear()  # noqa: SLF001
        status, _h, body = call("/preview/status")
        probe = json.loads(body)
        if probe.get("extractorBlocked") is not True:
            failures.append(
                "`extractorBlocked` did not survive the proxy — without it the "
                "whole feature can be dead while every probe reports healthy")
        if probe.get("upstreamReachable") is not True:
            failures.append(
                "a blocked extractor is not an unreachable sidecar; the two are "
                "different faults with different fixes")
        if "lastExtractorError" in probe:
            failures.append(
                "the raw extractor message was relayed to clients — that is an "
                "operator diagnostic, and the hub whitelists what it passes on")
        BLOCKED[0] = False
        proxy._cache.clear()  # noqa: SLF001
        probe = json.loads(call("/preview/status")[2])
        if probe.get("extractorBlocked") is not False:
            failures.append("`extractorBlocked` stayed true after recovery")

        # --- disabled: the probe still answers, everything else is a 503 -----
        hub.PREVIEW_SECRET = ""
        off = hub.PreviewProxy()
        status, _h, body = asyncio.run(
            off.handle(None, "/preview/status", headers, "GET"))
        probe = json.loads(body)
        if status != 200 or probe.get("configured") is not False:
            failures.append(
                f"a disabled preview proxy must answer the probe with "
                f"configured:false, got {status} {probe}")
        if probe.get("previewCastable") is not False:
            failures.append("a disabled proxy must not claim castability")
        status, _h, _b = asyncio.run(
            off.handle(None, "/preview/resolve?title=x", headers, "GET"))
        if status != 503:
            failures.append(
                f"a disabled preview proxy must 503 a real route, got {status}")
        if off.enabled:
            failures.append(
                "the proxy claims to be enabled without PREVIEW_SECRET — every "
                "stream URL would be signed with an empty key and refused at "
                "playback rather than at configuration")
    finally:
        (hub.PREVIEW_URL, hub.PREVIEW_PUBLIC_URL, hub.PREVIEW_SECRET,
         hub.PREVIEW_TTL) = saved
        server.shutdown()

    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - preview routes: reachable/param-whitelist/signed-stream-url/"
          "sig-verifies-against-the-sidecar/exp-in-the-future/cached-but-re-signed/"
          "queue-track-shaped/empty-is-a-200/castable-flag/extractor-blocked-"
          "passes-through/raw-error-does-not/disabled-probe-answers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
