# navi-connect preview sidecar

Resolves "what does this album I don't own sound like" to a playable track, and
serves its audio.

```
GET /resolve?artist=&title=&album=&durationMs=   -> a queue-track-shaped object, or {}
GET /stream?id=&exp=&sig=                        -> the audio, with HTTP Range / 206
GET /status                                      -> liveness
```

Clients do not talk to it directly for `/resolve` — that goes through the hub, on
`/preview/resolve`, which is where the hub also signs the `streamUrl` it hands
back. `/stream` is fetched from here, by the client or by a Chromecast.

## Why it is a separate process

Two reasons, both structural.

**The hub cannot serve the audio.** Its `HttpProxy` answers from inside the
WebSocket handshake: the body is a single `bytes` with a library-computed
`Content-Length`, the response cap is 4 MB, there are four shared concurrency
slots, and the handshake deadline would truncate a long body mid-stream. That is
a control-plane transport, and media is not control plane.

**An extractor breaks when a site changes.** It must not be able to take the
session relay down with it.

## Configuration

Everything is env; see `.env.example`. The one setting with no default is
`PREVIEW_SECRET`, and the process refuses to start without it: `/stream` is
authorised by an HMAC in the URL, because a Chromecast fetches that URL itself
and sends no headers. It must be the same value as the hub's `PREVIEW_SECRET`,
and it must **not** be `HUB_TOKEN` — that is the hub's whole administrative
surface and must never appear in a URL.

Set `PREVIEW_PUBLIC_URL` **on the hub** to the address a client (or a speaker)
can reach this process on. Treat it as required: with it unset the hub signs
`PREVIEW_URL` instead, and that is normally a Docker-internal hostname that no
client can resolve. It also advertises `previewCastable: false`, so both clients
refuse a cast transfer while a preview is queued rather than letting it fail as
silence.

## Nothing is written to disk

`/stream` opens the upstream media URL with the client's own `Range` header and
relays the bytes, so seeking is the upstream's Range support rather than a cache
that would need a size bound. Only the two resolutions — the search result and
the direct media URL — are cached, in memory, with different TTLs because they
expire for different reasons.

## Cookies

`PREVIEW_COOKIES` points at a Netscape-format `cookies.txt`. It is how yt-dlp
answers *"Sign in to confirm you're not a bot"*, which is what some egress
addresses get — measured on one host: the same code passed over IPv6 and was
challenged over IPv4, from the same machine in the same second.

**Use a throwaway account.** The file is bearer access to whatever account
exported it, it sits unencrypted next to the container, and yt-dlp traffic can
get an account rate-limited or terminated.

Exporting one that lasts:

1. Open a **private/incognito** window and log in to YouTube.
2. Export cookies for `youtube.com` with a `cookies.txt` extension.
3. **Close the private window without logging out.** Logging out invalidates the
   session server-side, which invalidates the file you just exported.

The sidecar switches player client when cookies are configured — `web`/`mweb`
rather than `android` — because yt-dlp's guidance is not to send account cookies
with the `android` client, and that combination is the one most associated with
accounts being limited.

Cookies expire. `/status` carries `extractorBlocked`, `consecutiveFailures` and
`lastExtractorError` so that is visible rather than silent, and the hub passes
`extractorBlocked` through on `/preview/status`. Without it a challenge and a
genuine no-match look identical from a client: an empty resolve against a
process reporting itself healthy.
