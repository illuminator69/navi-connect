#!/usr/bin/env python3
"""
navi-connect preview sidecar — "what does this album I don't own sound like".

Two endpoints, and deliberately nothing else:

    GET /resolve?artist=&title=&album=&durationMs=   -> a queue-track-shaped object
    GET /stream?id=&exp=&sig=                        -> the audio, with HTTP Range

**This process is the media origin, and the hub is not.** The hub proxies only
`/resolve` and `/status` — ordinary buffered JSON, which is what its `HttpProxy`
is for. It cannot serve the audio: `HttpProxy.handle` ends in a single `bytes`
body coerced through `AbortHandshake` with a library-computed `Content-Length`,
`PROXY_MAX_RESPONSE` is 4 MB, `PROXY_MAX_INFLIGHT` is 4 slots shared with every
lb-bot call, and the WebSocket handshake deadline truncates a long body
mid-stream. See `PreviewProxy` in `hub.py` and PROTOCOL.md §16.

There is a second, independent reason this is its own process: extractors break
when a site changes, often and without warning. A broken extractor here must be
incapable of taking the session relay down with it.

**Nothing is written to disk.** `/stream` opens the upstream media URL with the
client's own `Range` header and relays the bytes, so seeking is the upstream's
Range support rather than a cache this would have to bound. Only the two
*resolutions* are cached, in memory, and both are small.

Stdlib + `yt-dlp`. Python 3.11+.
"""
from __future__ import annotations

import asyncio
import difflib
import hmac
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Config (env, like the hub — the container is 12-factor)
# --------------------------------------------------------------------------- #
HOST = os.environ.get("PREVIEW_HOST", "0.0.0.0")
PORT = int(os.environ.get("PREVIEW_PORT", "4792"))
# The capability secret, shared with the hub and NOT HUB_TOKEN. The hub mints
# signed stream URLs with it; this process verifies them. Empty = refuse to run:
# an unsigned /stream is an open media relay pointed at a third-party extractor.
SECRET = os.environ.get("PREVIEW_SECRET", "")
# Clock skew allowance when checking `exp`. Small: the two processes are normally
# the same host, and this is the only slack an expired capability gets.
CLOCK_SKEW = int(os.environ.get("PREVIEW_CLOCK_SKEW", "60"))

# How long a resolution (artist+title -> ext: id) is remembered. An `ext:` id is
# stable for a given track, so this is long; the hub caches it for six hours too.
RESOLVE_TTL = float(os.environ.get("PREVIEW_RESOLVE_TTL", str(6 * 3600)))
# How long a *media URL* is remembered, which is a different and much shorter
# fact: the extractor's direct URLs are themselves signed and expire, and serving
# an expired one reads to the user as a track that plays for zero seconds.
MEDIA_TTL = float(os.environ.get("PREVIEW_MEDIA_TTL", "1800"))
CACHE_MAX = 512  # entries per cache, evicted soonest-to-expire first

# Concurrent EXTRACTOR runs. Streams are not counted here: an extraction is a
# burst of upstream requests, a stream is a long idle relay, and one ceiling for
# both would either throttle playback or let searches stampede.
MAX_EXTRACTIONS = int(os.environ.get("PREVIEW_MAX_EXTRACTIONS", "3"))
# Concurrent streams, which bound memory (one CHUNK buffer each) and sockets.
MAX_STREAMS = int(os.environ.get("PREVIEW_MAX_STREAMS", "8"))

SEARCH_RESULTS = int(os.environ.get("PREVIEW_SEARCH_RESULTS", "5"))
SEARCH_TIMEOUT = float(os.environ.get("PREVIEW_SEARCH_TIMEOUT", "30"))
# Below this, a candidate is not the track that was asked for and `{}` is the
# honest answer. Tuned against the duration gate below rather than alone.
MIN_CONFIDENCE = float(os.environ.get("PREVIEW_MIN_CONFIDENCE", "0.45"))
# A candidate whose length differs by more than this is rejected outright,
# whatever its title says — it is an album upload, an extended edit or a
# different recording. Only applied when the caller supplied `durationMs`.
DURATION_TOLERANCE_MS = int(os.environ.get("PREVIEW_DURATION_TOLERANCE_MS", "20000"))

CHUNK = 64 * 1024
# Upstream socket timeout for the media relay. Generous: this is a read on a
# stream the client is consuming at playback rate, not a page fetch.
STREAM_TIMEOUT = float(os.environ.get("PREVIEW_STREAM_TIMEOUT", "30"))
REQUEST_HEADER_MAX = 16 * 1024

# A Netscape-format cookies.txt for the extractor, which is how yt-dlp answers
# "Sign in to confirm you're not a bot". Unset = no cookies, which is correct
# wherever the egress address is not being challenged.
#
# Measured here 2026-09-23: this host's IPv4 is challenged and its IPv6 is not,
# so the same code passed on the workstation (which has ISP IPv6) and failed in
# the container (which does not). Cookies are the fix that does not depend on
# the network.
#
# It is a CREDENTIAL. Never logged, never in a response body, and the file is
# bind-mounted rather than baked into the image. Use a throwaway account: yt-dlp
# traffic can get an account rate-limited or terminated, and these cookies are
# bearer access to whatever account exported them.
COOKIES = os.environ.get("PREVIEW_COOKIES", "")

PROVIDER = "yt"  # the only provider today; `ext:<provider>:<id>` is the format
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def log(*a: Any) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# --------------------------------------------------------------------------- #
# The capability token
#
# A Chromecast fetches `streamUrl` itself and sends no headers, so the credential
# has to be in the URL — and the hub's own token must never be, because it is
# that hub's entire administrative surface. This signature authorises exactly one
# thing: reading one `ext:` id's audio until `exp`.
# --------------------------------------------------------------------------- #
_EXT_ID_RE = re.compile(r"^ext:[a-z0-9]{1,16}:[A-Za-z0-9_\-]{1,64}$")


def sign(track_id: str, exp: int) -> str:
    """Must stay byte-identical to `preview_sign` in the hub's `hub.py`."""
    return hmac.new(SECRET.encode(), f"{track_id}\n{exp}".encode(),
                    hashlib.sha256).hexdigest()


def verify(track_id: str, exp_raw: str, supplied: str) -> Optional[str]:
    """None if the capability is good, else a short reason for the log/response."""
    if not _EXT_ID_RE.match(track_id or ""):
        return "malformed id"
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return "malformed exp"
    # Signature BEFORE expiry, so a wrong key and an old URL are indistinguishable
    # to a caller — and, more usefully, so an unsigned request costs no clock read.
    if not hmac.compare_digest(sign(track_id, exp), supplied or ""):
        return "bad signature"
    if exp + CLOCK_SKEW < int(time.time()):
        return "expired"
    return None


# --------------------------------------------------------------------------- #
# Caches — two, because they expire for different reasons
# --------------------------------------------------------------------------- #
class TTLCache:
    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        hit = self._data.get(key)
        if hit is None:
            return None
        expiry, value = hit
        if expiry < time.monotonic():
            del self._data[key]
            return None
        return value

    def drop(self, key: str) -> None:
        self._data.pop(key, None)

    def put(self, key: str, value: Any) -> None:
        if len(self._data) >= CACHE_MAX:
            for old in sorted(self._data, key=lambda k: self._data[k][0])[:CACHE_MAX // 4]:
                del self._data[old]
        self._data[key] = (time.monotonic() + self._ttl, value)


RESOLVE_CACHE = TTLCache(RESOLVE_TTL)
MEDIA_CACHE = TTLCache(MEDIA_TTL)

_extract_sem: Optional[asyncio.Semaphore] = None
_stream_sem: Optional[asyncio.Semaphore] = None

# Extractor health, so a blocked extractor is VISIBLE rather than silent.
#
# Without this a bot challenge and a genuine no-match are the same thing to a
# client — both `{}`, with /status still reporting a healthy process — so the
# deployment looks perfectly fine while every preview answers "not found". That
# is the same trap as a silently truncated 200. It matters more with cookies
# than without: cookies expire, and when they do this is the only thing that
# will say so.
_health = {"consecutive_failures": 0, "last_error": "", "blocked": False}
# Substrings that mean "the extractor was refused", not "this video is gone".
# Matched case-insensitively against the extractor's own message.
_BLOCKED_MARKERS = ("confirm you\u2019re not a bot", "confirm you're not a bot",
                    "sign in to confirm", "cookies", "age-restricted",
                    "this content isn\u2019t available", "429")


def _note_extract_ok() -> None:
    _health.update(consecutive_failures=0, last_error="", blocked=False)


def _note_extract_failed(err: BaseException) -> None:
    msg = str(err)
    _health["consecutive_failures"] += 1
    # Truncated, and it is the EXTRACTOR's message — it never contains cookie
    # material. Kept so /status can say why rather than only that.
    _health["last_error"] = msg[:300]
    low = msg.lower()
    if any(m in low for m in _BLOCKED_MARKERS):
        _health["blocked"] = True


# --------------------------------------------------------------------------- #
# Matching
#
# A search engine will always answer. The question is whether the thing it
# answered with is the recording that was asked for — and getting that wrong is
# worse than answering nothing, because a wrong preview is indistinguishable from
# a broken library to the person listening.
# --------------------------------------------------------------------------- #
_NOISE = re.compile(
    r"\b(official|video|audio|hd|hq|lyrics?|lyric|visuali[sz]er|mv|full|album|"
    r"remaster(ed)?|explicit|clean|4k|1080p|720p)\b", re.I)
_BRACKETS = re.compile(r"[\[(][^\])]*[\])]")
_PUNCT = re.compile(r"[^\w\s]")
# Forms that are a DIFFERENT recording of the same song. Penalised, not banned:
# the caller may genuinely have asked for a live album.
_VARIANT = re.compile(
    r"\b(live|cover|karaoke|instrumental|remix|reaction|sped\s*up|slowed|"
    r"nightcore|8d|tutorial|reversed)\b", re.I)


def _norm(text: str) -> str:
    text = _BRACKETS.sub(" ", text or "")
    text = _NOISE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return " ".join(text.lower().split())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def score_candidate(cand: dict, artist: str, title: str,
                    want_ms: int) -> float:
    """0.0-1.0 — how sure are we that this is the recording that was asked for.

    Deliberately explainable rather than clever: the client renders this number,
    and a confidence nobody can account for is worse than none.
    """
    cand_title = _norm(str(cand.get("title") or ""))
    cand_artist = _norm(str(cand.get("uploader") or cand.get("channel") or ""))
    want_title = _norm(title)
    want_artist = _norm(artist)
    if not cand_title:
        return 0.0

    # The search text as a whole, because a result is usually titled "Artist -
    # Title" and neither field alone matches it.
    combined = _ratio(f"{want_artist} {want_title}".strip(), cand_title)
    # ...and the title on its own, for a channel that titles bare tracks.
    title_only = _ratio(want_title, cand_title)
    score = max(combined, title_only * 0.95)

    # The uploading channel being the artist is strong evidence (an official
    # artist channel, or a topic channel), but its absence is not evidence
    # against — so this only ever adds.
    if want_artist and cand_artist and _ratio(want_artist, cand_artist) > 0.7:
        score = min(1.0, score + 0.15)
    # The artist's name appearing nowhere at all is evidence against.
    elif want_artist and want_artist not in cand_title and not cand_artist:
        score -= 0.1

    raw_title = str(cand.get("title") or "")
    if _VARIANT.search(raw_title) and not _VARIANT.search(title or ""):
        score -= 0.25

    # Duration is the one hard signal here. A three-minute song matched to a
    # fifty-minute upload is a full-album rip, and no title similarity redeems it.
    dur_ms = int(float(cand.get("duration") or 0) * 1000)
    if want_ms and dur_ms:
        delta = abs(dur_ms - want_ms)
        if delta > DURATION_TOLERANCE_MS:
            return 0.0
        score += 0.1 * (1.0 - delta / DURATION_TOLERANCE_MS)
    elif want_ms and not dur_ms:
        score -= 0.05  # a candidate that will not say how long it is

    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# yt-dlp (blocking; every caller goes through asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _ydl(opts: dict) -> Any:
    # Imported lazily and per call so a broken/absent yt-dlp is a 503 on one
    # route rather than a process that will not start.
    from yt_dlp import YoutubeDL
    # The player client depends on whether we are authenticated, and the two
    # cases genuinely want different ones.
    #
    #   no cookies -> `android` first. It is the client that still answers
    #                 unauthenticated on a challenged address; `web` needs a
    #                 challenge this process deliberately does not solve
    #                 ("The page needs to be reloaded", measured 2026-09-23).
    #   cookies    -> `web`/`mweb`. yt-dlp's own guidance is NOT to send account
    #                 cookies with the `android` client: it is the combination
    #                 most associated with accounts being rate-limited or
    #                 terminated, which is also why the account here should be a
    #                 throwaway.
    clients = ["web", "mweb"] if COOKIES else ["android", "web"]
    base = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": SEARCH_TIMEOUT,
        "extractor_args": {"youtube": {"player_client": clients}},
    }
    if COOKIES and os.path.exists(COOKIES):
        # Deliberately not read-only in the container: yt-dlp refreshes the jar
        # as it goes, and letting it write back is what keeps a session alive
        # past its first rotation.
        base["cookiefile"] = COOKIES
    return YoutubeDL({**base, **opts})


def search_blocking(artist: str, title: str, album: str) -> list[dict]:
    """Search, flat. `extract_flat` is the point: it returns id/title/duration/
    uploader for N candidates for the cost of ONE request, where a full
    extraction per candidate would be N extractions to discard N-1 of them."""
    terms = " ".join(t for t in (artist, title) if t).strip()
    if not terms:
        return []
    query = f"ytsearch{SEARCH_RESULTS}:{terms}"
    with _ydl({"extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(query, download=False) or {}
    entries = info.get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


_AUDIO_MIME = {"webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
               "opus": "audio/ogg", "ogg": "audio/ogg", "mp3": "audio/mpeg"}
_VIDEO_MIME = {"mp4": "video/mp4", "webm": "video/webm"}


def _format_mime(fmt: dict) -> str:
    """The media type this format will ACTUALLY be served as.

    Both clients hand the `mime` from /resolve to their Cast `MediaItemConverter`
    and to their player, so a declared type that does not match the bytes is not
    a cosmetic error — it is a track that loads and never plays. It is therefore
    derived from the chosen format rather than assumed, including the
    `video/` case below.
    """
    ext = (fmt.get("ext") or "").lower()
    if fmt.get("vcodec") not in (None, "none"):
        # A MUXED format. Not what we asked for, but see _choose_format: it is
        # sometimes all that is on offer, and a video container whose type is
        # declared honestly plays (the client renders no picture). One whose
        # type is declared as audio does not.
        return _VIDEO_MIME.get(ext, "video/mp4")
    return _AUDIO_MIME.get(ext or (fmt.get("audio_ext") or "").lower(), "audio/webm")


def _choose_format(info: dict) -> Optional[dict]:
    """Best audio-only format, else the smallest muxed one.

    The fallback is not hypothetical. Measured 2026-09-23: with no PO token,
    YouTube's `android` client offers exactly one real format — an 11.9 MB muxed
    360p MP4 — and every adaptive audio-only stream requires a challenge this
    process deliberately does not solve. So "audio-only or nothing" would mean
    the whole feature answers nothing on this network. Smallest-first on the
    fallback because the video track is pure waste to a listener.
    """
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    audio_only = [f for f in formats
                  if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none")]
    if audio_only:
        return max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    muxed = [f for f in formats if f.get("acodec") not in (None, "none")]
    if not muxed:
        return None
    return min(muxed, key=lambda f: f.get("filesize")
               or f.get("filesize_approx") or float("inf"))


def media_url_blocking(video_id: str) -> Optional[tuple[str, str]]:
    """(direct media URL, mime) for one id — a full extraction.

    `format` is left unset and the choice made here instead: asking yt-dlp for
    `bestaudio/best` makes an unavailable audio-only stream a hard
    "Requested format is not available" on some clients, which loses the muxed
    fallback that is currently the only thing that plays at all.
    """
    with _ydl({}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                                download=False) or {}
    fmt = _choose_format(info)
    if fmt is None:
        return None
    mime = _format_mime(fmt)
    if mime.startswith("video/"):
        log(f"{video_id}: no audio-only format on offer, falling back to muxed "
            f"{fmt.get('format_id')} ({mime})")
    return fmt["url"], mime


# --------------------------------------------------------------------------- #
# /resolve
# --------------------------------------------------------------------------- #
async def handle_resolve(params: dict[str, str]) -> tuple[int, bytes, str]:
    artist = (params.get("artist") or "").strip()[:200]
    title = (params.get("title") or "").strip()[:200]
    album = (params.get("album") or "").strip()[:200]
    try:
        want_ms = max(0, int(params.get("durationMs") or 0))
    except (TypeError, ValueError):
        want_ms = 0
    if not title:
        return 400, json.dumps({"error": "title is required"}).encode(), "application/json"

    key = json.dumps([artist.lower(), title.lower(), album.lower(), want_ms])
    cached = RESOLVE_CACHE.get(key)
    if cached is not None:
        return 200, json.dumps(cached).encode(), "application/json"

    assert _extract_sem is not None
    try:
        async with _extract_sem:
            entries = await asyncio.wait_for(
                asyncio.to_thread(search_blocking, artist, title, album),
                timeout=SEARCH_TIMEOUT + 5)
    except asyncio.TimeoutError:
        log(f"resolve timed out: {artist!r} - {title!r}")
        return 504, json.dumps({"error": "resolver timed out"}).encode(), "application/json"
    except Exception as e:  # noqa: BLE001 — an extractor fault is not a crash
        _note_extract_failed(e)
        log(f"resolve failed for {artist!r} - {title!r}: {e}")
        return 502, json.dumps({"error": "resolver failed"}).encode(), "application/json"

    best, best_score = None, 0.0
    for cand in entries:
        s = score_candidate(cand, artist, title, want_ms)
        if s > best_score:
            best, best_score = cand, s

    if best is None or best_score < MIN_CONFIDENCE:
        # `{}` at HTTP 200: "no preview found" is a legitimate answer, never an
        # error. Same rule as lb-bot's strict=False metadata chain, and the
        # clients render it as "no preview" rather than as a failure. Cached, so
        # a track nothing matches does not re-run the search on every page open.
        RESOLVE_CACHE.put(key, {})
        return 200, b"{}", "application/json"

    track_id = f"ext:{PROVIDER}:{best['id']}"

    # Resolve the format NOW rather than declaring a mime and hoping. Both
    # clients feed this value straight to their Cast MediaItemConverter and to
    # their player, so a `mime` that does not match the bytes /stream will serve
    # is a track that loads and then never plays — and which format is on offer
    # is not knowable from the flat search result above. It costs one extraction,
    # warms MEDIA_CACHE for the /stream that is about to follow, and is cached at
    # both ends.
    assert _extract_sem is not None
    media = MEDIA_CACHE.get(best["id"])
    if media is None:
        try:
            async with _extract_sem:
                media = await asyncio.wait_for(
                    asyncio.to_thread(media_url_blocking, best["id"]),
                    timeout=SEARCH_TIMEOUT + 5)
        except Exception as e:  # noqa: BLE001
            _note_extract_failed(e)
            log(f"resolve matched {best['id']} but could not extract it: {e}")
            media = None
        if media is not None:
            _note_extract_ok()
            MEDIA_CACHE.put(best["id"], media)
    if media is None:
        # Matched, but nothing playable. `{}` rather than a track with a dead
        # streamUrl: "no preview" is a state the clients render, and a row that
        # plays silence is not.
        #
        # NOT cached, unlike a genuine no-match: extraction failures are usually
        # transient (a challenge, a throttle), and remembering one for six hours
        # would turn a bad minute into a bad afternoon for that track.
        return 200, b"{}", "application/json"
    mime = media[1]

    dur_ms = int(float(best.get("duration") or 0) * 1000) or want_ms
    answer = {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": album,
        "durationMs": dur_ms,
        # Derived rather than taken from the flat search result, which carries
        # no thumbnail. It is always available and always correct for the id —
        # and it is a FALLBACK: a client showing a preview from an album page
        # already holds that release's Cover Art Archive URL and should prefer
        # it, because this one is a video still, not a sleeve.
        "imageUrl": (best.get("thumbnail")
                     or f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"),
        # Deliberately EMPTY. The hub rewrites this into a signed capability URL
        # on the way out (`PreviewProxy.call`), which is how PREVIEW_SECRET never
        # has to leave these two processes. A client reading a bare sidecar
        # directly gets no playable URL, and that is correct.
        "streamUrl": "",
        "mime": mime,
        "provider": PROVIDER,
        "confidence": round(best_score, 3),
    }
    RESOLVE_CACHE.put(key, answer)
    return 200, json.dumps(answer).encode(), "application/json"


# --------------------------------------------------------------------------- #
# /stream — Range, 206, and no disk
# --------------------------------------------------------------------------- #
def _open_upstream_blocking(url: str, rng: str) -> tuple[int, Any, dict]:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    if rng:
        # Forwarded VERBATIM. iOS will not play a resource that does not answer
        # Range, and seeking is nothing but this header — so the upstream's own
        # Range support is the feature, rather than something reimplemented here
        # over a cache that would then need a size bound.
        req.add_header("Range", rng)
    resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
    headers = {
        "Content-Type": resp.headers.get("Content-Type") or "audio/webm",
        "Accept-Ranges": "bytes",
    }
    for name in ("Content-Length", "Content-Range"):
        value = resp.headers.get(name)
        if value:
            headers[name] = value
    return resp.status, resp, headers


async def handle_stream(params: dict[str, str], request_headers: dict[str, str],
                        writer: asyncio.StreamWriter, method: str) -> bool:
    """Writes the response itself (it is not a buffered body). True = handled."""
    track_id = params.get("id") or ""
    reason = verify(track_id, params.get("exp") or "", params.get("sig") or "")
    if reason is not None:
        # Verified BEFORE a single upstream request is spent: an unsigned or
        # expired URL must cost this process nothing but the HMAC.
        log(f"stream refused ({reason}): {track_id[:48]}")
        await write_response(writer, 403, json.dumps(
            {"error": f"stream capability {reason}"}).encode(), "application/json")
        return True

    provider, video_id = track_id.split(":", 2)[1:]
    if provider != PROVIDER:
        # The signature proves the hub minted this id, not that this build still
        # knows how to fetch it — a capability outlives a deploy.
        await write_response(writer, 404, json.dumps(
            {"error": f"unknown provider {provider!r}"}).encode(), "application/json")
        return True
    cached = MEDIA_CACHE.get(video_id)
    if cached is None:
        assert _extract_sem is not None
        try:
            async with _extract_sem:
                cached = await asyncio.wait_for(
                    asyncio.to_thread(media_url_blocking, video_id),
                    timeout=SEARCH_TIMEOUT + 5)
        except Exception as e:  # noqa: BLE001
            _note_extract_failed(e)
            log(f"stream extraction failed for {video_id}: {e}")
            await write_response(writer, 502, b'{"error":"extraction failed"}',
                                 "application/json")
            return True
        if cached is None:
            await write_response(writer, 404, b'{"error":"no audio stream"}',
                                 "application/json")
            return True
        _note_extract_ok()
        MEDIA_CACHE.put(video_id, cached)

    url, mime = cached
    rng = request_headers.get("range", "")
    assert _stream_sem is not None
    async with _stream_sem:
        try:
            status, resp, headers = await asyncio.to_thread(
                _open_upstream_blocking, url, rng)
        except urllib.error.HTTPError as e:
            # Most often 403: the extractor's direct URLs are themselves signed
            # and expire. Drop the memo so the next request re-extracts rather
            # than serving the same dead URL for the rest of MEDIA_TTL.
            MEDIA_CACHE.drop(video_id)
            log(f"upstream refused {video_id}: {e.code}")
            await write_response(writer, 502, b'{"error":"upstream refused"}',
                                 "application/json")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"upstream open failed for {video_id}: {e}")
            await write_response(writer, 502, b'{"error":"upstream unreachable"}',
                                 "application/json")
            return True

        # Overridden, not defaulted: this is the type /resolve DECLARED, and the
        # two must agree or the client set up its player for a container it is
        # not being sent. The upstream's own header is usually the same, but
        # "usually" is not a contract.
        headers["Content-Type"] = mime
        try:
            write_headers(writer, status, headers)
            if method == "HEAD":
                # A HEAD is how a Chromecast and an iOS player ask "is this real
                # and does it do Range" before they commit to playing it.
                await writer.drain()
                return True
            while True:
                buf = await asyncio.to_thread(resp.read, CHUNK)
                if not buf:
                    break
                writer.write(buf)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            # The listener skipped or closed the app. Entirely ordinary.
            pass
        except Exception as e:  # noqa: BLE001
            log(f"stream relay ended for {video_id}: {e}")
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
    return True


# --------------------------------------------------------------------------- #
# A very small HTTP/1.1 server
#
# `asyncio.start_server`, not a framework, and explicitly not anything that
# buffers a whole response body — which is the single constraint this file
# exists to satisfy.
# --------------------------------------------------------------------------- #
_STATUS_TEXT = {200: "OK", 206: "Partial Content", 400: "Bad Request",
                403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                500: "Internal Server Error", 502: "Bad Gateway",
                503: "Service Unavailable", 504: "Gateway Timeout"}


def write_headers(writer: asyncio.StreamWriter, status: int,
                  headers: dict[str, str]) -> None:
    text = _STATUS_TEXT.get(status, "OK")
    out = [f"HTTP/1.1 {status} {text}"]
    out += [f"{k}: {v}" for k, v in headers.items()]
    out.append("Connection: close")
    writer.write(("\r\n".join(out) + "\r\n\r\n").encode())


async def write_response(writer: asyncio.StreamWriter, status: int, body: bytes,
                         ctype: str) -> None:
    write_headers(writer, status, {"Content-Type": ctype,
                                   "Content-Length": str(len(body))})
    writer.write(body)
    await writer.drain()


async def serve_client(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=15)
        if not request_line:
            return
        try:
            method, raw_path, _ = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await write_response(writer, 400, b'{"error":"bad request line"}',
                                 "application/json")
            return

        headers: dict[str, str] = {}
        read = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            if line in (b"\r\n", b"\n", b""):
                break
            read += len(line)
            if read > REQUEST_HEADER_MAX:
                await write_response(writer, 400, b'{"error":"headers too large"}',
                                     "application/json")
                return
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()

        path, _, query = raw_path.partition("?")
        path = path.rstrip("/") or "/"
        params = {k: v for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)}

        if method not in ("GET", "HEAD"):
            await write_response(writer, 405, b'{"error":"method not allowed"}',
                                 "application/json")
            return

        if path == "/stream":
            await handle_stream(params, headers, writer, method)
            return

        if path == "/resolve":
            status, body, ctype = await handle_resolve(params)
        elif path in ("/status", "/"):
            # The hub's `/preview/status` rides on this. It replaces the body with
            # its own verdict (it is the only side that knows `previewCastable`),
            # so what matters here is answering 200 at all — that IS the
            # reachability fact the hub is probing for.
            status, ctype = 200, "application/json"
            body = json.dumps({
                "ok": True, "provider": PROVIDER, "signed": bool(SECRET),
                # Whether a cookie file is CONFIGURED and present. Never its
                # contents, never its path — this is a health field on an
                # endpoint the hub proxies to clients.
                "cookies": bool(COOKIES and os.path.exists(COOKIES)),
                # The extractor's own state, so a blocked extractor is visible
                # instead of looking like "nothing matched". Cookies expire;
                # this is what will say so.
                "extractorBlocked": _health["blocked"],
                "consecutiveFailures": _health["consecutive_failures"],
                "lastExtractorError": _health["last_error"],
            }).encode()
        else:
            status, body, ctype = 404, b'{"error":"unknown route"}', "application/json"

        if method == "HEAD":
            write_headers(writer, status, {"Content-Type": ctype,
                                           "Content-Length": str(len(body))})
            await writer.drain()
        else:
            await write_response(writer, status, body, ctype)
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:  # noqa: BLE001 — one bad request must not kill the server
        log("request error:", e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    if not SECRET:
        raise SystemExit(
            "PREVIEW_SECRET is not set.\n"
            "/stream is authorised by an HMAC capability in the URL, because a "
            "Chromecast sends no headers. Without a secret every signature "
            "verifies against an empty key, which makes this an open media relay "
            "pointed at a third-party extractor.\n"
            "Set the SAME value here and on the hub. It is NOT HUB_TOKEN."
        )
    global _extract_sem, _stream_sem
    _extract_sem = asyncio.Semaphore(MAX_EXTRACTIONS)
    _stream_sem = asyncio.Semaphore(MAX_STREAMS)

    if COOKIES and not os.path.exists(COOKIES):
        # A configured-but-missing cookie file is the shape a bind mount takes
        # when it is wrong, and the symptom without this is indistinguishable
        # from cookies that simply are not working.
        log(f"WARNING: PREVIEW_COOKIES points at {COOKIES!r}, which does not "
            f"exist. Running WITHOUT cookies -- check the bind mount.")
    elif COOKIES:
        log(f"cookies: {COOKIES} (player_client=web; account cookies are not "
            f"sent with the android client)")
    else:
        log("cookies: none -- fine unless this host's egress address is being "
            "challenged (see PREVIEW_COOKIES)")

    server = await asyncio.start_server(serve_client, HOST, PORT)
    log(f"navi-connect preview sidecar on http://{HOST}:{PORT}  "
        f"(provider={PROVIDER}, extractions<={MAX_EXTRACTIONS}, "
        f"streams<={MAX_STREAMS})")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
