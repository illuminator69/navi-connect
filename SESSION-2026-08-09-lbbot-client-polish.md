# SESSION 2026-08-09 — lb-bot client integration, rounds 2 and 3

Two rounds of fixes on top of the 2026-08-08 build (phases 0 / 1.5 / 1 / 2), both driven by actually
running it. Round 2's write-up was folded into the design doc's corrections 7–8; this file covers
both so the day reads in one place. **Nothing here has met the live slskd / Navidrome / lb-bot
stack** beyond the user's own use of Feishin.

Repos touched: `Lb-bot-missing` (committed + pushed, `cd5ac0d`), `navi-connect/hub`,
`navi-connect/feishin`.

---

## Round 2 — the shelf was wrong, and a filled album listed twice

**Missing releases are no longer a shelf.** They were rendered in a separate "Not in your library"
block, which read as an unsorted pile of everything absent. They are now bucketed by the *same*
release-type key the owned albums use and rendered inside those sections — a missing album in
*Albums*, a missing single in *Singles*. The key derivation was extracted out of Feishin's own
grouping code (`releaseTypeKeyFor` in `use-artist-albums-grouped.ts`) so there is one rule rather
than two that can drift, and it respects the primary/all grouping toggle. A type the library holds
none of still gets a section. Tiles are faded and dashed with a "Not in library" badge.

**A completed fill left the album in both lists.** Two independent causes, both needing a fix:

- lb-bot's `release_groups.status` stayed `'missing'` until a full MusicBrainz rescan — one request
  per second, unusable per download. `_finalize_group` now calls `_index_mark_release_present()`,
  matched by rgid for an artist-page fill and by review group id for a Fill-gaps one. The rgid is
  threaded from `api_album_download` through `_album_download_task` into the fill ledger purely so
  that call has it.
- The clients cache both answers, so even a correct index doesn't reach an open page. Fixed with
  query invalidation on `placed`/`verified`, and for other devices an lb-bot → hub webhook:
  `POST <hub>/lb/notify` (`LB_BOT_HUB_URL` / `LB_BOT_HUB_TOKEN`, optional) fanned out as a `library`
  broadcast. Handled *ahead* of proxy dispatch, because `LbProxy` would 404 an inbound path against
  its outbound route table.

**Bug found while building it:** `unknown` was in the terminal-fill-state set, but the download POST
returns before lb-bot's worker writes its first ledger row — so the poll could stop a second after
the tap and never restart. Replaced with bounded patience (~90 s of `unknown`).

---

## Round 3 — sort order, quality, the stuck download, the duplicate

### Missing tiles follow the album sort direction

They were year-ascending regardless, so a section read newest-first through the owned albums and
then flipped to oldest-first. `AlbumSection` now takes `descending` from the page's `sortOrder`.
Year-then-title remains the ordering itself — a release the library doesn't hold has no date added,
play count or rating.

### Quality is per download

`POST /api/album/download` takes an optional `quality` (`flac-any` | `flac-16-44` |
`highest-bitrate` | `prefer-opus`); unrecognised values are a `400` rather than a silent ignore.
Upstream it is scoped with a ContextVar (`_quality_preference`) exactly like the MP3 opt-in — the
preference is read six levels down, in `_file_score` and folder ranking, both now going through
`_effective_quality()` — and stored on the album group, because the failover path runs on another
thread long after that scope has exited. The hub's body whitelist gained the key; the enum lives
client-side (lb-bot's only reader of the list is its prefs endpoint, a write surface the hub does
not whitelist). Feishin's selector remembers the last choice across albums and restarts.

**Confirmed limitation:** this is a ranking preference, not a filter. `_quality_preference_score`
contributes 0–600 to a folder score running to ~14000, and in `_file_score` quality is a tie-break
after format priority. A well-matching 24/96 folder still beats a poorly-matching 16/44 one, and
with a single source available the preference is moot. A hard guarantee would need a `qualityStrict`
flag upstream that makes `_file_score` reject non-conforming files outright — not done, because
"no hi-res" would then mean "no download".

### The download that stopped being tracked

Two separate bugs.

*Client:* the watch store was in-memory on the reasoning that lb-bot's ledger is too. Quitting
Feishin mid-download therefore stopped tracking the album. It is persisted now
(`store_lbbot_fills`), pruned on load to the 20-minute watch window; a rehydrated fill that finds
nothing upstream resolves through the existing bounded-`unknown` path.

*lb-bot, and the one that left the album on the Downloads tab:* a group whose transfers had all
disappeared from slskd was only finalized by `ALBUM_GROUP_TTL` (six hours), **and** only when a
Telegram app was registered for the group's token — neither of which holds for a fill started from a
client. The poller sweep now also finalizes a group with nothing left in `pending_downloads` after
`ALBUM_GROUP_ORPHAN_GRACE` (180 s), skipping groups mid-failover, and `_tg_send` no-ops on a falsy
bot so placement never depends on there being a chat to narrate it to. Left alone, such a group
stranded its files in `/downloads` and never resolved the fill a client was watching.

### The duplicate that survived the index flip

The flip only fires for fills this pipeline completed. Anything else that puts an album in the
library — a manual copy, a fill against a different release of the same group, a title the matcher
missed — leaves the row saying `missing` while Navidrome plainly has it. Correcting the index from
the client would mean a MusicBrainz walk, so the artist page reconciles missing rows against the
albums Navidrome reports, by normalised title (`withoutOwned`): NFD, strip combining marks,
lowercase, drop bracketed segments, drop non-alphanumerics. Reconciled against the *unfiltered*
album list, so a search term can never make an owned album start showing as missing.

Bracketed suffixes are stripped; **dash suffixes deliberately are not** — collapsing "X" and
"X - Live" would hide an album the user then cannot download, and that mistake costs more than a
leftover duplicate.

---

## Verification

lb-bot and hub both `ast.parse` clean. 23/23 hub proxy assertions, including new ones for
`/lb/notify` (auth, method, works with `LBBOT_URL` unset, never forwarded upstream) and for
`quality` surviving the body whitelist. A live WebSocket test confirms a notify POST arrives as a
`library` frame on a connected client. Feishin typechecks and lints clean and `pnpm run build`
succeeds.

## Deferred

- **Phase 3 — Navic.** See `HANDOFF-lbbot-navic.md`, written this session. Navic receives the
  `library` frame and ignores it, which is correct until it has an lb-bot surface at all.
- The Android notification and a Feishin toast on `placed` — the remaining half of phase 4.
- Phase 5 — discovery rows (fresh releases, similar albums).
- A live-stack run of any of it.

## Note on the lb-bot repo

`Lb-bot-missing` has **no `main` branch**; `origin/HEAD` is
`claude/unraid-containers-handoff-egnevy`, which is where `cd5ac0d` was pushed. That commit also
carries the previously-uncommitted `DOWNLOAD_RELIABILITY_PLAN` round that was sitting in the working
tree. Several files remain untracked there (`.codex/`, `AGENTS.md`, `AUDIT_FIXES.md`,
`DOWNLOAD_RELIABILITY_PLAN.md`, `handoff.md`, `design_handoff_lb_bot_frontend/`).
