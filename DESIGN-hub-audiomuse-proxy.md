# navi-connect — routing AudioMuse Tier 2 through the hub (design)

Status: **implemented** 2026-08-07 (hub + both clients; not yet run against a live AudioMuse).
Companion to `DESIGN-adaptive-audiomuse.md`, which specifies the *features*; this specifies only how
their Tier-2 HTTP calls reach the AudioMuse core. The wire contract is `PROTOCOL.md` §14; §9 below
records where the build departed from this plan.

Goal: stop every client from holding the AudioMuse base URL, the AudioMuse API token, and the
Navidrome password, and stop AudioMuse's core API from needing its own internet exposure. The hub is
already the one authenticated, remotely reachable, server-side component — Tier 2 should ride it.

---

## 1. Why this is a small change

Tier 2 is already built in both clients, and the surface in use is **five endpoints**, all
synchronous in-memory lookups:

| Endpoint | Method | Used by |
|---|---|---|
| `/api/sonic_fingerprint/generate` | GET | both |
| `/api/alchemy` | POST | both |
| `/api/clap/search` | POST | both |
| `/api/clap/stats` | GET | both (capability probe) |
| `/get_score` | GET | Feishin only (visualizer mood/energy) |

`chatPlaylist` — the one streaming, LLM-backed endpoint — is **deliberately unused** (README §5), so
nothing on the client path needs a streamed response.

Each client already funnels all of them through a single choke point:

- **Feishin** — `src/main/features/core/audiomuse/index.ts`, five `ipcMain.handle` handlers. The
  renderer never calls AudioMuse directly (the core sets no CORS headers), and passes config per
  call, persisting nothing in main.
- **Navic** — `domain/manager/AudioMuseManager.kt`, four Ktor calls on one shared `HttpClient`.

So this is a base-URL-and-auth-header change at two files, not a rewrite.

## 2. What's wrong with the direct route

1. **Both clients send the Navidrome username and password as URL query parameters.**
   `AudioMuseManager.kt` (`parameter("navidrome_user", …)`, `parameter("navidrome_password", …)`) and
   `audiomuse/index.ts` (`url.searchParams.set('navidrome_password', …)`), on the
   `/api/sonic_fingerprint/generate` call. Over plain HTTP to `http://host:8000`, in a query string,
   so they land in access logs and any intermediary. The hub already holds `HUB_ND_USER` /
   `HUB_ND_PASS` server-side for the savePlayQueue mirror — it can supply them itself and the clients
   can stop carrying them at all.
2. **The AudioMuse bearer token lives on every device** (`PreferenceManager.audioMuseUrl` /
   `audioMuseToken` in Navic; `settings.store.ts` `audioMuse` in Feishin). A phone that leaves the
   LAN carries a credential for a service that should never have been reachable from outside it.
3. **Tier 2 only works where AudioMuse is reachable**, so using it away from home means exposing
   port 8000 — a service with heavy ML and admin endpoints — to the internet.
4. Config is duplicated per client, so the two can disagree about whether Tier 2 is available, and
   the result cache the feature design asks for (`DESIGN-adaptive-audiomuse.md` §2, responsiveness
   rule 3) has to be built twice.

## 3. Transport: HTTP on the hub's existing WebSocket port

**Decision: serve the proxy as HTTP on port 4790 via `websockets.serve(process_request=…)`.**

The `websockets` legacy asyncio server calls `process_request(path, request_headers)` before the
WebSocket handshake; returning a `(status, headers, body)` triple short-circuits the connection into
a plain HTTP response. So one listener serves both the existing WS protocol and the proxy, with no
new port and no new exposure.

Rejected alternatives:

- **New WebSocket act frames** (`act:"sonic"` + a correlated result frame). The hub's protocol is
  fire-and-forget today — `act` / `do` / `report` / broadcast, with **no request/response
  correlation anywhere**. This would mean building correlation ids, per-request timeouts, and
  cancellation from scratch, then implementing that RPC layer a second and third time in each
  client, to end up expressing calls that are already REST. Rejected as the larger change for no
  gain.
- **Extending the health listener on 4791.** `_serve_health` already speaks raw HTTP over
  `asyncio.start_server`, so full control (including chunked streaming) is available there. But it
  answers *any* request with status JSON and is unauthenticated by design, so it would need an auth
  split, and it means exposing a second port. The only thing it buys is streaming, which
  `chatPlaylist` would need and nothing else does. Keep 4791 as the unauthenticated health probe;
  revisit only if `chatPlaylist` is ever adopted.

## 4. Hub design

### Config
Two env vars, mirroring the existing `NAVIDROME_URL` pattern:

```
AUDIOMUSE_URL=http://audiomuse:8000     # unset => proxy disabled, probe reports unavailable
AUDIOMUSE_TOKEN=...                     # server-side only; never sent to clients
```

Add both to `.env.example`. No Dockerfile change — same port, same volume.

### Route table (whitelist, not a forwarder)
A literal map from a hub path to an upstream path, method, and allowed parameters:

```
GET   /sonic/fingerprint   -> GET  /api/sonic_fingerprint/generate   params: n
POST  /sonic/alchemy       -> POST /api/alchemy                      body: items[], n, temperature, subtract_distance
POST  /sonic/clap/search   -> POST /api/clap/search                  body: query, limit
GET   /sonic/clap/stats    -> GET  /api/clap/stats                   params: -
GET   /sonic/score         -> GET  /get_score                        params: id
```

**Never forward an arbitrary path.** `HUB_TOKEN` is held by every client; if the proxy passed
through whatever it was given, that token would grant access to AudioMuse's analysis, clustering,
and embedding-management endpoints. Unknown path or method → 404, no upstream call. Unknown
parameters are dropped rather than relayed.

The hub injects `Authorization: Bearer $AUDIOMUSE_TOKEN` and, for the fingerprint route, the
`navidrome_user` / `navidrome_password` params from `HUB_ND_USER` / `HUB_ND_PASS`. Clients never
send either.

### Auth
`Authorization: Bearer <HUB_TOKEN>`, compared with `hmac.compare_digest` (the module is already
imported for the WS handshake). Missing or wrong → 401 before any upstream call. When `HUB_TOKEN` is
empty the hub already warns that it accepts anything; the proxy should additionally refuse to run in
that state rather than become an open relay.

### Execution
`urllib.request` inside `asyncio.to_thread`, exactly as `_nd_save_play_queue_blocking` does at
`hub.py:355`. Two limits matter:

- **Cap in-flight calls** with an `asyncio.Semaphore` (4 is plenty for two clients). Without it a
  slow or hung AudioMuse can saturate the default thread pool and stall the 1 Hz progress fan-out —
  the hub's one genuinely latency-sensitive job.
- **Short timeouts** (connect 5s, read 20s), matching the tight timeouts both clients already use,
  so a cold core fails fast into the Tier-1 fallback.

### Cache
An in-memory `{(route, canonical-params): (value, expiry)}` map, ~60s TTL, capped in size. This is
the shared version of the per-client cache the feature design asks for, and it means the two clients
asking the same question get one upstream call. Never persisted — `state.json` stays session state
only.

### Errors
Pass the upstream status through (`503` cold index, `404` unanalyzed track, `400` feature disabled)
rather than flattening to 500, so the clients' existing fail-soft branches keep working unchanged.
The hub logs failures like the mirror does and never raises into the WS loop.

## 5. Client changes

**Feishin** (`src/main/features/core/audiomuse/index.ts`): the five handlers keep their argument
shapes, response parsing, and fail-soft `return []` behaviour. Only the request construction
changes — hub base URL instead of `args.baseUrl`, `Bearer <hub token>` instead of the AudioMuse
token, and drop `navidrome_user`/`navidrome_password` from the fingerprint call. The renderer is
untouched.

**Navic** (`AudioMuseManager.kt`): same, on the four Ktor calls. The shared `HttpClient`, timeouts,
and response models are unchanged.

**Settings**: prefer the hub route whenever the hub is connected and reports the proxy configured;
keep the direct fields as an explicit fallback for a LAN-only setup with no hub. `isConfigured`
becomes "hub proxy available **or** direct URL+token set".

**Capability probe**: `/sonic/clap/stats` already doubles as the liveness probe. Add the hub's own
availability to it — the hub returns a small `{configured, upstreamReachable}` so both clients agree
on Tier-2 state from one source instead of probing independently.

## 6. Caveats accepted

- **Tier 2 becomes hub-dependent** when routed. Today it works with the hub down. This is a real
  behaviour change; the fail-soft rule absorbs it (no hub → grey out Tier 2, fall back to Tier 1),
  and the direct-config fallback in §5 covers the LAN-only case.
- **`process_request` is the legacy `websockets` API.** `requirements.txt` pins
  `websockets>=12.0,<14`, so it works today, but the 14+ asyncio server changes the signature. This
  couples a future `websockets` bump to a proxy rewrite — small, but worth knowing before the bump.
- **No streaming through `process_request`** — the body is returned whole. Fine while
  `chatPlaylist` stays unused; adopting it means the 4791 route from §3 instead.
- **One extra hop.** Client → hub → AudioMuse. Both are normally on the same host or LAN, so this
  adds single-digit milliseconds against an index lookup that already dominates. Audio is unaffected
  — these calls only build queues; playback still streams from Navidrome.

## 7. Build order

1. Hub: config, route table, auth, semaphore, cache, `/sonic/*` handler. ~150 lines in `hub.py`.
2. `.env.example` + a `PROTOCOL.md` section (the proxy is HTTP, not part of the WS message catalog,
   so it wants its own short section rather than an entry in §5).
3. Navic `AudioMuseManager` + settings toggle.
4. Feishin main-process handlers + settings toggle.
5. Retire the per-client AudioMuse token once both are on the hub route.

## 8. Verification

- `hub/tools/test_edits.py` stays 4/4 (the proxy must not perturb session/saved-queue merge logic).
- A direct `curl` against `/sonic/clap/stats` with and without the bearer token: 200 / 401.
- An unlisted path (`/sonic/../api/analysis`, `/sonic/anything`) returns 404 with no upstream call.
- Both clients: Sonic Fingerprint autoplay and CLAP search still return tracks with the AudioMuse
  token removed from client settings; the features grey out when the hub is stopped.
- Confirm the Navidrome password no longer appears in AudioMuse's access log.

---

## 9. What the build changed (2026-08-07)

Three departures from §3–§5, all forced by what the code actually allows:

1. **A `WebSocketServerProtocol` subclass, not a bare `process_request=` callable.** The legacy
   server's `read_request()` hard-rejects any method but `GET` — a `POST /sonic/alchemy` would never
   have reached `process_request` at all — and a plain callable is handed only `(path, headers)`,
   with no route to the `StreamReader` holding the request body. `hub.py:_build_proxy_protocol()`
   subclasses the protocol, re-implements `read_http_request()` without the GET assertion (recording
   the method), and reads the body off `self.reader` in the handler. If those internals ever move,
   the import fails, the hub logs it and keeps serving WebSocket traffic with the proxy off — the
   `websockets` bump caveat in §6 stays true, but it degrades instead of failing to start.
2. **`/sonic/alchemy` is not cached.** §4 asks for one 60 s TTL across the board, but alchemy is
   stochastic (`temperature`) and Mood Flow re-asks with the same add/subtract sets as a session
   runs, so a cached mix would top the queue up with the tracks it just added. Everything else
   caches; only 200s are stored, so a cold-index 503 can't stick for a minute.
3. **The probe answers 200 even when it can't reach AudioMuse.** §5 asked for `{configured,
   upstreamReachable}`; those are merged into the upstream `/api/clap/stats` body rather than
   replacing it, and "unreachable" is reported as data, not an error status, so a client can tell
   *"the hub has no AudioMuse"* (→ fall back to its direct config) from *"AudioMuse is down"*
   (→ Tier 1). Both clients demote themselves off the hub route for 10 minutes on `configured:false`
   rather than permanently, so a hub that gains an `AUDIOMUSE_URL` is picked up without a restart.

Also worth knowing: `urllib` has a single timeout knob covering connect and read, so §4's 5 s/20 s
split is one 20 s timeout; and an unread POST body is drained before answering 401/404/405, because
closing the socket mid-upload surfaces on the client as a connection reset instead of the status.

Verification run (`§8`): hub proxy smoke test 22/22 (auth 200/401, unlisted path and traversal 404,
parameter/body whitelisting, injected Navidrome creds, client-supplied password dropped, cache hit,
upstream status passthrough, WebSocket handshake unaffected); `hub/tools/test_edits.py` 4/4;
Feishin renderer + main typecheck clean. Both clients are **unverified against a live AudioMuse**.
