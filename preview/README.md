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
can reach this process on. Without it the hub advertises `previewCastable:
false` and both clients refuse a cast transfer while a preview is queued, rather
than letting it fail as silence.

## Nothing is written to disk

`/stream` opens the upstream media URL with the client's own `Range` header and
relays the bytes, so seeking is the upstream's Range support rather than a cache
that would need a size bound. Only the two resolutions — the search result and
the direct media URL — are cached, in memory, with different TTLs because they
expire for different reasons.
