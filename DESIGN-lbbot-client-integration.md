# DESIGN — lb-bot integration in Feishin & Navic

*Drafted 2026-08-08. **Revised 2026-08-08 after review against the real code**, and phases 1–2
implemented the same day. Assumes lb-bot's `DOWNLOAD_RELIABILITY_PLAN.md` is implemented:
auto-fill matches and downloads succeed in the common case, and a failed fill carries a named
reason.*

> **lb-bot lives at `C:\Users\icher\Lb-bot-missing`.** The copy that used to sit in this repo at
> `lb-bot/` was a June snapshot missing every endpoint below; it has been deleted.

**Status.** Phase 1.5 (lb-bot), phase 1 (hub `/lb/*`), phase 2 (Feishin) and phase 4
(`libraryEvent`, hub + Feishin halves) are implemented, unit- and build-verified, **not yet run
against the live stack**. Phase 3 (Navic) and phase 5 (discovery rows) are not started.

Corrections from first use, on top of the six below:

7. **Missing releases do not get a shelf.** They are grouped by the same release-type key the owned
   albums use and rendered inside those sections, so an unowned album sits in *Albums* next to the
   ones the library holds — faded and dashed rather than filed elsewhere. A separate shelf read as
   an unsorted pile of everything absent, which is the opposite of a discography.
8. **A placed album stayed `missing` in the index**, so it showed in both lists at once.
   `_finalize_group` now flips the `release_groups` row to `present` (a full rescan is one
   MusicBrainz request per second and cannot run per download), and the clients invalidate on the
   fill reaching `placed`/`verified`. The cross-device half is phase 4's `library` broadcast, fed
   by a new inbound `POST /lb/notify` — see `PROTOCOL.md` §15.1.

Four more from the second run:

9. **Missing tiles must follow the album sort direction.** They were year-ascending regardless, so
    a section read newest-first through the owned albums and then oldest-first through the missing
    ones. They now take the page's `sortOrder`; year-then-title is still the only ordering that
    applies to a release with no date added, play count or rating.
10. **The index flip is not enough on its own.** It only fires for fills *this* pipeline completed;
    anything else that puts an album in the library (a manual copy, a fill against a different
    release of the same group, a title the matcher missed) leaves the row saying `missing` while
    Navidrome plainly has it. The artist page therefore reconciles missing rows against the owned
    albums by normalised title (`withoutOwned`) — Navidrome is the authority on what the library
    holds and is already loaded on that page. Bracketed edition suffixes are stripped; dash
    suffixes deliberately are **not**, because collapsing "X" and "X - Live" would hide an album
    the user then cannot download.
11. **Quality has to be per download, not global.** lb-bot's Source preference is one setting for
    the whole library, and "not 24/96 for this single" is an album-level decision. `POST
    /api/album/download` takes an optional `quality`; it is scoped with a ContextVar exactly like
    the MP3 opt-in and carried on the album group for the failover path. The client remembers the
    last choice.
12. **A fill has to survive the client restarting.** The watch store was in-memory "because
    lb-bot's ledger is too", which meant quitting Feishin mid-download silently stopped tracking
    the album. It is persisted now, pruned on load to the 20-minute watch window; a rehydrated fill
    that finds nothing upstream resolves through the existing bounded-`unknown` path. The matching
    lb-bot bug: a group whose transfers all vanished was only finalized by the 6 h TTL, and only
    when a Telegram app was registered for its token — so a client-started fill could sit on the
    Downloads tab indefinitely. The sweep now also finalizes a group with nothing left in
    `pending_downloads`, and `_tg_send` no-ops without a bot.

Six things in the first draft were wrong; they are corrected inline below and summarised here so a
reader of the old version knows what changed:

1. **No cover proxy.** lb-bot's `/api/cover/<id>` is *Navidrome* art keyed by a *Navidrome album
   id* — meaningless for a release the library lacks. Cover Art Archive URLs are public and built
   client-side from the release-group id.
2. **Only the discography GET is instant.** `album/releases`, `album/tracklist` and `album/similar`
   sit on rate-limited MusicBrainz / ListenBrainz calls.
3. **`album/download` was not idempotent** — made so in lb-bot.
4. **The download task finishes before the download does**, so it cannot drive the progress chip.
   A new lb-bot endpoint `GET /api/album/status` does.
5. **`/api/tasks*` must never be polled** — it deep-copies lb-bot's whole review state.
6. **Failure reasons and Allow-MP3 lived only on review groups**, so the artist-page download path
   had neither. lb-bot now records both for that path too.

Consequently "no lb-bot changes required for phases 1–3" was false; three small ones were needed.

## Goal

Surface lb-bot's library-gap intelligence inside both streaming clients:

1. **Artist pages show the full discography** — owned releases *and* releases not in the
   library (lb-bot's per-artist MusicBrainz index), with covers.
2. **One-tap download** of a missing album from either client; the album then appears in the
   normal library views once lb-bot places + Navidrome verifies it.
3. **Discovery rows** — fresh releases with ownership badges, similar-artists/albums shelves —
   reusing lb-bot's ListenBrainz/Last.fm lookups.

Non-goal: reimplementing lb-bot's Fill-gaps workspace (source picking, manual pairing, repair)
in the clients. Complex failures deep-link to the lb-bot web UI.

## Why this is cheap

lb-bot's `GET /api/artist/discography?nd_id=<navidrome-artist-id>` is an **instant SQLite read
keyed by the same Navidrome artist id the clients already hold** on every artist page. It
returns `{indexed:true, ...releases}` or `{indexed:false}` — never a surprise MusicBrainz wait
(refresh is an explicit POST that runs as a background task). So the integration is a
fail-soft enrichment layer, architecturally identical to the AudioMuse Tier-2 pattern both
clients already implement.

**That claim holds for the artist page only.** The album sheet's routes —`album/releases`,
`album/tracklist`, `album/similar` — call MusicBrainz and ListenBrainz *synchronously*. They are
cheap in the sense that matters (the answers are immutable, so the hub caches them for hours) but
a cold call takes seconds and the sheet must render a skeleton, not assume an instant answer.

The discography rows carry `rgid`, `title`, `year`, `primary_type`/`secondary_types`/
`effective_type`, `status` (complete / incomplete / missing / untagged), the match method and, when
owned, `navidrome_album_ids`. **No cover field** — see the covers note below.

## Architecture: hub `/lb/*` proxy

Mirror `SonicProxy` (`hub/hub.py`, `PROTOCOL.md` §14): plain HTTP on port 4790, hub-token
gated, whitelisted routes, credentials/addresses held server-side, responses cached.

Why proxy instead of direct client → `:8899`:

- **Remote access.** lb-bot's Flask is LAN-only and unauthenticated; the hub is the
  publicly-reachable rendezvous. Without it, discography on Navic works only at home — and
  exposing `:8899` to the internet is not acceptable.
- **Caching / back-pressure.** lb-bot is a single Python process with process-wide locks and a
  hot 2s poll path from its own SPA. The hub cache (60s TTL on reads) keeps two clients from
  adding load. Clients must never poll `/lb/*` on an interval tighter than the hub TTL.
- **Fail-soft for free.** Hub down or lb-bot down → the section hides, same as Tier 2.

`SonicProxy`'s cache, semaphore and param/body filtering are now a shared `HttpProxy` base
(`hub/hub.py`); `SonicProxy` and `LbProxy` differ only in prefix, upstream, token and route table.
`process_request` walks `PROXIES` in order — each returns `None` for a path outside its own prefix,
so the WebSocket handshake still falls through untouched.

Whitelist (`LB_ROUTES`, alongside `SONIC_ROUTES`):

| Route | Method | Cache | Purpose |
|---|---|---|---|
| `lb/status` | GET | 60s | liveness probe; always answers, even when disabled |
| `lb/artist/discography` | GET | 60s | instant index read (`nd_id`/`mbid`) |
| `lb/artist/discography` | POST | — | explicit "Index this artist" (background task) |
| `lb/fresh-releases` | GET | 60s | home row, carries `artistOwned`/`releaseOwned` |
| `lb/album/releases`, `lb/album/tracklist`, `lb/album/similar` | GET | **6h** | MusicBrainz-backed; immutable answers |
| `lb/album/download` | POST | — | one-tap acquisition; idempotent per release |
| `lb/album/status` | GET | **never** | the download-progress poll |
| `lb/album/allow-mp3` | POST | — | per-album format opt-in |

Two details worth keeping:

- **No `cover/` route.** lb-bot's `/api/cover/<album_id>` fetches *Navidrome* `getCoverArt` keyed
  by a *Navidrome album id* — by definition absent for a release the library doesn't have. Art for
  unowned releases is `https://coverartarchive.org/release-group/<rgid>/front-250`, a public URL
  the clients build themselves. Proxying it would also put multi-megabyte bodies into a cache
  bounded by entry count, not bytes.
- **`allow-mp3` puts the group id in the body**, not the path, so the route table stays a table of
  exact paths. The hub interpolates it into `/api/gaps/{group_id}/allow-mp3` after validating it
  against `^[A-Za-z0-9_.:-]{1,64}$` — an id that isn't a plain token is a 400, not an escaped
  attempt to walk out of the whitelist.

Hub env: `LBBOT_URL` (unset = feature disabled, clients hide the sections). **No welcome capability
flag** — `GET /lb/status` mirrors `/sonic/clap/stats`: routed unconditionally, always 200, answering
`{configured, upstreamReachable}`. That is the already-proven pattern and costs no protocol change.

## Client UX

### Artist page (both clients)

- Renders exactly as today, immediately. In parallel one GET to `/lb/artist/discography`.
- `indexed:true` → a "Not in your library" shelf below the owned albums (release-group tiles,
  CAA covers, type badges: album/EP/single/compilation). Stale index → small "may be out of
  date · Rescan" affordance (fires the POST, shows task progress).
- `indexed:false` → single "Index this artist" affordance. Never auto-scan on page open
  (MusicBrainz is rate-limited 1 req/s; a big artist takes 10–60s).
- Unreachable → render nothing. No error states on the artist page.

### Missing-album detail → download

Tap a missing release → lightweight sheet: tracklist (`album/tracklist`), variant/edition pick
(default digital), **Download** button. POST fires lb-bot's normal pipeline (slskd search
40–75s → transfer → tag → place → Navidrome verify). Fire-and-forget.

**The task id is not the progress signal.** `_album_download_task` calls `_task_finish` the moment
slskd accepts the enqueue — "Queued 9/12 file(s)" — with the transfer, the placement and the
Navidrome scan all still ahead. A client polling `/api/tasks/<id>` would flip to success about a
minute before anything reached the library. Worse, `/api/tasks*` goes through `_review_snapshot()`,
a deep copy of lb-bot's entire multi-MB review state under its process-wide lock, on what would be
a polled path. **Never whitelist it.**

Instead lb-bot keeps an in-memory fill ledger keyed by release MBID (`_album_fill_status`), written
at the points that actually know — the download task, `_finalize_group`, and a verifier — and
served by `GET /api/album/status?release_mbid=`:

```
unknown → searching → queued → downloading → placing → placed → verified
                                       ↘ needs_match   ↘ failed
```

Live transfer counts are read off `pending_album_groups` when the endpoint is called, not written
by the download poller, so the hot path pays nothing. `placed` means the files are in the album
folder; **`verified` means Navidrome has indexed them**, which is the only state that answers "is it
in my library". A review-group fill already has `_verify_placement_worker`; an artist-page download
has no group, so `_start_album_fill_verification` polls the placement's own `per_file` rows with one
cheap `search3` each (a full library page-through on a 30s loop was the alternative, and isn't one).

The ledger is in memory by design: an in-flight download doesn't survive a restart either, and a
client that finds no entry reads `unknown`, which renders as the plain "not in your library" state.

- v1 (implemented): poll `album/status` through the proxy only while the sheet is open, at no less
  than the hub's TTL, stopping on a terminal state.
- v2 (implemented, hub + Feishin): lb-bot webhooks the hub on placement —
  `POST /lb/notify {event, release_mbid, rgid, artist, album}`, the one inbound route under the
  prefix — and the hub rebuilds it into a `library` broadcast to every connected device
  (`PROTOCOL.md` §15.1). Feishin drops its Navidrome album queries and lb-bot's discography read;
  Navic ignores the frame until phase 3 gives it something to refresh. Chosen over hub-side polling
  because the hub would otherwise have to poll lb-bot for albums nobody is watching.
  Configured on lb-bot (`LB_BOT_HUB_URL`/`LB_BOT_HUB_TOKEN`) and entirely optional: the index flip
  below already makes the *next* read on any client correct, so the broadcast only closes the window
  where a page is already open somewhere else.
  Still to do: the Android notification "X is now in your library", and a Feishin toast.
- Failure surfaces lb-bot's *reason* verbatim. These lived only on **review groups**
  (`no_source_reason` / `mp3_would_help`, set in `_apply_group_sources`), and the artist-page
  download creates no group — so `_album_download_task` now fills the same `stats` dict and runs it
  through `_no_source_reason` too. "103 peers offered 2,047 files, but none in FLAC, OPUS" is the
  useful answer; paraphrasing it loses the evidence. "Allow MP3 for this album" is offered only when
  the status carries a `groupId` (the endpoint needs one); otherwise the fallback is "Open in
  lb-bot" (deep link `#/gaps/<id>` / `#/artist/<id>/<rgid>`, LAN only).

### Discovery rows

- Home: "Fresh releases" row from `fresh-releases` — two scopes using its `artistOwned` /
  `releaseOwned` flags ("from your artists", "new artists"). Tap-through: owned artist → artist
  page; unowned → the missing-album sheet.
- Album page: "Similar albums" shelf from `album/similar` (one owned album per similar artist,
  already attributed). A "similar artists you don't have" surface can follow later — the
  lookups exist in lb-bot; may need one small new endpoint.

## Performance rules (the smoothness contract)

- Discography/discovery fetches are **never on the critical render path**: separately fetched,
  skeleton-loaded, fail-soft hidden. Same discipline as Tier-2 AudioMuse.
- **Navic caches discography rows in Room** (new table keyed by artist nd_id, schema version
  **20** — 19 is current): shelf renders instantly from cache, revalidates in background. Feishin
  relies on the hub cache plus react-query's (60s for the discography, `Infinity` for the
  immutable album lookups).
- Covers for unowned releases load lazily **straight from the Cover Art Archive**, `loading="lazy"`,
  hiding themselves on error — plenty of release-groups have no front cover, and an empty slot
  beats a broken-image icon.
- Clients poll download status only while a relevant view is open (until `libraryEvent` lands),
  never tighter than the hub TTL, and stop on a terminal state.

## Known limitations

- Unindexed artists show nothing until explicitly indexed; index staleness is real
  (`INDEX_SCAN_VERSION`, 30-day TTL). Mitigation: in-UI Rescan; optionally a hub-side nightly
  bulk `library-index/build` trigger.
- Artist matching depends on MBIDs/nd ids lining up; mismatched tags fall to `indexed:false`
  gracefully. **The index POST needs `mbid` *and* `name`**, not `nd_id` alone, so an artist with no
  MusicBrainz tag can't be indexed at all — the affordance hides rather than offering something that
  would fail. Feishin gets the MBID from the Navidrome native normalizer (`mbz: item.mbzArtistId`);
  the plain Subsonic normalizer leaves artist `mbz` null.
- `album/tracklist` reports `presenceKnown: false` when no Navidrome album ids are supplied, which
  is the normal case for a fully missing album. Every track is missing; that is not an error and
  must not render as "0/12".
- Soulseek is best-effort by nature: even with reliable matching, a release can have no usable
  source (format policy, locked shares, dead peers). Failure reasons + the lb-bot deep link are
  part of the design, not an afterthought.
- Concurrent triggers (client + lb-bot SPA) on the same album: clients are thin triggers, lb-bot
  group state stays the single owner. `POST /api/album/download` now checks `pending_album_groups`
  and the running `album-download` tasks for the same release first and returns
  `{ok: true, existing: true}` with the in-flight ids, so a second tap is a no-op rather than a
  second search, a second set of transfers and a second placement pass over one folder.

## Phasing

0. ✅ **Delete the stale in-repo lb-bot copy.** Its state JSONs and an orphaned `staging/` holding
   ~330 MB of downloaded-but-never-placed FLACs were moved to
   `C:\Users\icher\lb-bot-old-state-2026-06-21\` first.
1.5 ✅ **lb-bot**: idempotent `album/download`; new `GET /api/album/status`; failure reason +
   `mp3_would_help` on the album path.
1. ✅ **Hub**: `/lb/*` proxy + whitelist + per-route TTL + `/lb/status` probe (shared `HttpProxy`
   base extracted from `SonicProxy`).
2. ✅ **Feishin**: artist discography shelf, index/rescan, missing-album sheet + download + polled
   status. (Desktop first — faster iteration than APK builds.)
3. **Navic**: parity + Room discography cache (schema v20).
4. ✅ **Hub → clients `library`** broadcast, fed by lb-bot's `POST /lb/notify`. Android
   notification on placed/verified still to do (needs 3).
5. **Discovery rows**: fresh releases + similar shelves.

## Integration points

- Hub: `hub/hub.py` — `HttpProxy` base, `LB_ROUTES` + `LbProxy`, the `PROXIES` dispatch in
  `_build_proxy_protocol`, plus `_handle_lb_notify` ahead of it and `HUB_INSTANCE` for the
  broadcast; `LBBOT_URL` in `.env.example`. `PROTOCOL.md` §14/§15 document both proxies, §15.1 the
  inbound notify, §5.4 the `library` frame.
- Feishin: `src/main/features/core/lbbot/index.ts` (hub-only — lb-bot is unauthenticated, so
  unlike AudioMuse there is deliberately no direct-LAN fallback), `src/preload/lbbot.ts`,
  `src/shared/types/lbbot-types.ts` (one wire-shape definition for all three layers),
  `src/renderer/features/lbbot/{hooks/use-lbbot.ts,stores/active-fills.store.ts,components/*}`.
  The tiles are mounted *inside* each release-type section by
  `features/artists/hooks/use-artist-albums-grouped.ts`, which now buckets lb-bot's rows with the
  same key it gives owned albums; the index/rescan button sits in the album toolbar. The `library`
  frame is handled in `features/hub/hooks/use-hub.tsx`.
- Navic: new `domain/manager/LbBotManager.kt` next to `AudioMuseManager.kt` (Ktor, same
  fail-soft pattern); Room cache; artist screen + home rows. Reuse `LB_ROUTES` unchanged.
- lb-bot (`C:\Users\icher\Lb-bot-missing\listenbrainz_bot.py`): the fill-status ledger
  (`_album_fill_set` / `_album_fill_view` / `_start_album_fill_verification`), the idempotency
  check in `api_album_download`, `api_album_status`, `_index_mark_release_present` and
  `_notify_hub_library_change` (both called from `_finalize_group`'s success path). All of it stays
  off `_review_lock`'s hot path and out of `_review_snapshot`.
