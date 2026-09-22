#!/usr/bin/env python3
"""
Tests the two things the sidecar exists to do, without touching yt-dlp.

  1. **Range and 206.** `/stream` must answer a ranged request with 206, a
     correct `Content-Range` and exactly the requested bytes, and an unranged one
     with 200 + `Accept-Ranges: bytes`. This is not decoration: iOS refuses to
     play a resource that does not answer Range, and seeking *is* this header.
     It is also the property the hub structurally cannot provide, which is the
     whole reason this is a separate process.
  2. **The capability, verified against the hub's own signer.** `hub.py`'s
     `preview_sign` and this file's `sign` must agree byte for byte, or every
     minted URL is refused at playback and the failure looks exactly like a
     broken extractor. The hub's function is imported here rather than
     reimplemented, so a change on either side fails this test.

The extractor is stubbed by seeding `MEDIA_CACHE` with a URL pointing at a local
static file server — so what is exercised is the relay, which is the part that
had to be written by hand.

Exits non-zero on failure.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SECRET = "range-test-secret"
os.environ["PREVIEW_SECRET"] = SECRET
os.environ.setdefault("PREVIEW_HOST", "127.0.0.1")

import preview  # noqa: E402

# 512 KB of distinguishable bytes: big enough to cross CHUNK (64 KB) several
# times, so a relay that drops or duplicates a chunk boundary is visible.
PAYLOAD = bytes((i * 7 + 13) % 256 for i in range(512 * 1024))


class Upstream(http.server.BaseHTTPRequestHandler):
    """A static origin that honours Range — i.e. what a real media host is."""

    protocol_version = "HTTP/1.1"

    def _send(self, status, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", "audio/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if not rng:
            self._send(200, PAYLOAD)
            return
        spec = rng.split("=", 1)[1]
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else len(PAYLOAD) - 1
        chunk = PAYLOAD[start:end + 1]
        self._send(206, chunk,
                   {"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"})

    def log_message(self, *_args):
        pass


def fetch(url, headers=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main() -> int:
    failures: list[str] = []

    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    origin_url = f"http://127.0.0.1:{origin.server_address[1]}/audio.webm"

    # Start the sidecar in this process, on its own loop thread.
    import asyncio

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    port_box: list[int] = []

    async def run():
        preview._extract_sem = asyncio.Semaphore(3)   # noqa: SLF001
        preview._stream_sem = asyncio.Semaphore(8)    # noqa: SLF001
        server = await asyncio.start_server(preview.serve_client, "127.0.0.1", 0)
        port_box.append(server.sockets[0].getsockname()[1])
        ready.set()
        async with server:
            await server.serve_forever()

    threading.Thread(target=lambda: (loop.run_until_complete(run())),
                     daemon=True).start()
    if not ready.wait(10):
        print("FAIL - the sidecar did not start")
        return 1
    base = f"http://127.0.0.1:{port_box[0]}"

    # Seed the media cache so no extractor runs. This is the ONLY stub: the
    # relay, the Range handling and the capability check are all the real code.
    video_id = "dQw4w9WgXcQ"
    track_id = f"ext:yt:{video_id}"
    preview.MEDIA_CACHE.put(video_id, (origin_url, "audio/webm"))

    # --- the capability, minted by the HUB ---------------------------------- #
    # Imported, not reimplemented: this is the contract between two processes,
    # and the point is that a change to either signer fails here.
    # `hub/` is a sibling of `preview/` in both the working tree and the
    # umbrella publish clone, so this resolves in both.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "hub"))
    os.environ.setdefault("HUB_TOKEN", "test-token")
    try:
        import hub as hubmod
    except Exception as e:  # noqa: BLE001
        print(f"FAIL - could not import the hub to check signature interop: {e}")
        return 1
    hubmod.PREVIEW_SECRET = SECRET
    hubmod.PREVIEW_PUBLIC_URL = base
    hubmod.PREVIEW_TTL = 3600
    stream_url = hubmod.preview_stream_url(track_id)
    if not stream_url.startswith(base + "/stream?"):
        failures.append(f"the hub minted an unexpected URL: {stream_url}")

    # --- 1. unranged: 200, whole body, Accept-Ranges ----------------------- #
    status, body, headers = fetch(stream_url)
    if status != 200:
        failures.append(f"an unranged /stream should be 200, got {status}: {body[:120]!r}")
    elif body != PAYLOAD:
        failures.append(
            f"the relayed body differs from the origin's "
            f"({len(body)} vs {len(PAYLOAD)} bytes) — the chunked relay is "
            "dropping or duplicating across CHUNK boundaries")
    if headers.get("Accept-Ranges") != "bytes":
        failures.append(
            "Accept-Ranges: bytes is missing — a player that reads it decides "
            "up front whether the resource is seekable at all")

    # --- 2. ranged: 206, exact bytes, correct Content-Range ---------------- #
    start, end = 100_000, 199_999
    status, body, headers = fetch(stream_url, {"Range": f"bytes={start}-{end}"})
    if status != 206:
        failures.append(
            f"a ranged /stream must answer 206, got {status} — iOS will not play "
            "a resource that answers 200 to a Range request, and seeking is "
            "nothing but this")
    if body != PAYLOAD[start:end + 1]:
        failures.append(
            f"the ranged body is wrong ({len(body)} bytes) — a seek would land "
            "somewhere other than where the user asked")
    want_cr = f"bytes {start}-{end}/{len(PAYLOAD)}"
    if headers.get("Content-Range") != want_cr:
        failures.append(
            f"Content-Range is {headers.get('Content-Range')!r}, want {want_cr!r}")

    # --- 3. an open-ended range (what a seek actually sends) --------------- #
    status, body, _h = fetch(stream_url, {"Range": "bytes=400000-"})
    if status != 206 or body != PAYLOAD[400000:]:
        failures.append(
            f"an open-ended range failed: {status}, {len(body)} bytes — this is "
            "the form a player sends when the user drags the scrubber")

    # --- 4. HEAD: headers without a body ----------------------------------- #
    status, body, headers = fetch(stream_url, method="HEAD")
    if status != 200:
        failures.append(f"HEAD /stream should be 200, got {status}")
    if body:
        failures.append("HEAD returned a body")
    if headers.get("Accept-Ranges") != "bytes":
        failures.append(
            "HEAD did not advertise Range — this is how a Chromecast and an iOS "
            "player ask whether a resource is worth committing to")

    # --- 5. the refusals, before a single upstream request ----------------- #
    parts = urllib.parse.urlparse(stream_url)
    q = urllib.parse.parse_qs(parts.query)
    exp = q["exp"][0]
    bad = f"{base}/stream?id={urllib.parse.quote(track_id)}&exp={exp}&sig=deadbeef"
    status, body, _h = fetch(bad)
    if status != 403:
        failures.append(f"a bad signature must be 403, got {status}")

    past = int(time.time()) - 10_000
    expired = (f"{base}/stream?id={urllib.parse.quote(track_id)}&exp={past}"
               f"&sig={preview.sign(track_id, past)}")
    status, body, _h = fetch(expired)
    if status != 403 or b"expired" not in body:
        failures.append(f"an expired capability must be 403 expired, got {status} {body[:80]!r}")

    status, _b, _h = fetch(f"{base}/stream?id=ext:zz:abc&exp={exp}"
                           f"&sig={preview.sign('ext:zz:abc', int(exp))}")
    if status != 404:
        failures.append(
            f"a validly-signed id from an unknown provider must be 404, got "
            f"{status} — a capability outlives a deploy")

    # --- 6. /status ---------------------------------------------------------#
    status, body, _h = fetch(f"{base}/status")
    if status != 200 or json.loads(body).get("ok") is not True:
        failures.append(f"/status should answer ok: {status} {body[:80]!r}")

    origin.shutdown()
    if failures:
        for f in failures:
            print(f"FAIL - {f}")
        return 1
    print("PASS - preview stream: unranged-200-whole-body/ranged-206-exact-bytes/"
          "content-range/open-ended-range/HEAD-advertises-range/"
          "hub-minted-signature-verifies/bad-sig-403/expired-403/"
          "unknown-provider-404")
    return 0


if __name__ == "__main__":
    sys.exit(main())
