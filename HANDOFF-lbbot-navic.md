# HANDOFF — lb-bot in Navic (phase 3)

*Written 2026-08-09, after three implementation rounds on the Feishin side. Everything below is
either shipped and running or a decision that round forced. Read `DESIGN-lbbot-client-integration.md`
for the original design and its twelve corrections; read `PROTOCOL.md` §15 for the wire contract.
This document is the part that matters when writing the Kotlin.*

**lb-bot lives at `the lb-bot repo`** and is *not* in this repo.

---

## 1. What Navic is being asked to grow

An artist page that shows the artist's **whole discography**, not just the part the library holds,
and lets you fetch a missing release with one tap. On Feishin that reads as: the same Albums /
Singles / EPs / Compilations sections as before, with the releases you don't own sitting in them,
faded and dashed, tappable.

Three things ride along with it: a per-album quality choice, a fill you can watch after leaving the
screen, and a cross-device refresh so a download on the desktop updates the phone.

---

## 2. The hard constraint, first

**lb-bot's Flask API has no authentication of any kind and binds `0.0.0.0:8899`.** It exposes
delete-file, trash and preference-writing routes. It must never be reachable from a client.

Consequences, all non-negotiable:

- Navic talks to lb-bot **only** through the hub, at `<hub>/lb/*` with `Authorization: Bearer
  <HUB_TOKEN>`. There is **no direct-LAN fallback** — this is the one place Navic must *not* copy
  `AudioMuseManager`, which does have one (AudioMuse has its own token; lb-bot has nothing).
- The hub's route whitelist is the security boundary. It is what stops a `HUB_TOKEN` holder reaching
  `/api/prefs` or the delete routes. Do not add routes to it casually, and never add a wildcard.
- The hub refuses to run its proxies at all when `HUB_TOKEN` is empty — they would be open relays.

So `LbBotManager` is `AudioMuseManager` minus `directEndpoint()`, minus the `hubDemotedAtMs`
fallback dance (there is nothing to fall back *to* — demote just means "off"), plus the routes below.

---

## 3. Wire contract

Full table in `PROTOCOL.md` §15. What Navic actually needs:

| Route | Cost | Notes |
|---|---|---|
| `GET /lb/status` | free | Always answers, even with `LBBOT_URL` unset. `{configured, upstreamReachable}` — anything but both true means hide the whole surface. |
| `GET /lb/artist/discography?nd_id=&mbid=` | **instant** | SQLite read upstream. Safe on every page open. |
| `POST /lb/artist/discography` | minutes | `mbid` **and** `name` both required. Walks MusicBrainz at 1 req/s. Explicit user action only. Returns a task id you should ignore (see §6). |
| `GET /lb/album/releases?rgid=` | seconds | Editions. Sits on MusicBrainz. Hub caches 6 h. Show a skeleton. |
| `GET /lb/album/tracklist?release_mbid=` | seconds | Same. `presenceKnown:false` means "the library holds none of it" — render as *none owned*, not as an error or `0/12`. |
| `POST /lb/album/download` | fast | Body: `rgid`, optional `quality`, optional `sourceUsername`/`sourceFolder`. Idempotent per release. |
| `GET /lb/album/status?release_mbid=` | fast | The progress poll. Never cached by the hub. |
| `POST /lb/album/allow-mp3` | fast | Body `group_id` + `allow`. Only offer when a status carries a `groupId`. |

**Never call `/api/tasks` or `/api/tasks/<id>`.** Both deep-copy lb-bot's entire multi-MB review
state under a process-wide lock. They are not whitelisted; keep it that way.

**Covers for unowned releases come straight from the Cover Art Archive**, built client-side:
`https://coverartarchive.org/release-group/<rgid>/front-250`. lb-bot's `/api/cover` is *Navidrome*
art keyed by a *Navidrome album id*, which by definition does not exist for a release you don't
have. A 404 from the Archive is the normal case for an obscure release — render an empty slot, not
a broken-image icon.

### The discography row

```json
{ "rgid": "...", "title": "...", "year": "1997",
  "primary_type": "Album", "secondary_types": ["Live"],
  "effective_type": "live", "status": "missing",
  "navidrome_album_ids": ["..."], "group_id": "...", "present": 3, "total": 12 }
```

`status` is one of `complete` | `incomplete` | `missing` | `untagged`. Lowercase
`primary_type`/`secondary_types` on ingest — lb-bot sends them title-cased.

---

## 4. Where the tiles go — the one real design decision

Feishin already grouped an artist's albums into release-type sections, so merging lb-bot's rows into
those sections was the obvious move (and the *only* acceptable one — a separate "not in your
library" shelf shipped first and the immediate feedback was that it read as "an unsorted mess").

**Navic has no such sections.** `ArtistDetailScreen.kt` renders albums as a single `ArtCarousel`
sorted by play count, and `DomainAlbum` carries no release type at all. So the Feishin solution does
not port.

**Recommended: render the discography section entirely from lb-bot's index.**

That index is the complete release list — owned and missing alike — and each row already carries
`effective_type` *and* `navidrome_album_ids`. So a "Discography" section below the existing carousel
can group by `effective_type`, and for each row either deep-link to the local album (when
`navidrome_album_ids` is non-empty and resolves in `AlbumDao`) or show a missing tile. One source,
one grouping rule, no need to invent release types for Navidrome albums that don't have them.

Keep the existing play-count carousel as-is. It answers "what do I listen to", which is a different
question, and replacing it would be a regression for anyone not using lb-bot.

The whole section disappears when lb-bot is unconfigured, unreachable, or the artist is unindexed.

### Sorting

**Newest first, within every section, for owned and missing alike.** This was a bug worth two
rounds: missing tiles were sorted year-ascending while owned albums followed the page's sort, so a
section read newest-first and then flipped to oldest-first halfway down. Year then title is the only
ordering that applies to a release with no date added, play count or rating — but the *direction*
must match whatever the rest of the section is doing.

---

## 5. Two safeguards that are not optional

### 5.1 A completed fill must leave the missing list — in two places

This is the single most confusing bug the feature can have: the album appears twice, once real and
once greyed out.

**Cause one, upstream.** `release_groups.status` defaults to `'missing'` and was only ever rewritten
by a full MusicBrainz rescan (1 req/s — unusable per download). lb-bot's `_finalize_group` now calls
`_index_mark_release_present()` on placement, keyed by rgid for an artist-page fill and by review
group id for a Fill-gaps one. **Already done; Navic inherits it.**

**Cause two, client-side, and Navic must implement this itself.** The index flip only covers fills
*this pipeline* completed. Anything else that puts an album in the library — a manual copy, a fill
against a different release of the same group, a title lb-bot's matcher didn't recognise — leaves
the row saying `missing` while Navidrome plainly has the album. Fixing the index from a client means
a MusicBrainz walk, so instead: **reconcile at render time against the albums Navidrome reports.**

Feishin's `withoutOwned` normalises both sides — NFD, strip combining marks, lowercase, drop
`[](){}`-bracketed segments, drop everything non-alphanumeric — and filters missing rows whose key
matches an owned album's. Port that verbatim.

**Do not extend it to dash suffixes.** It is tempting ("X - Remastered"), but it would collapse
"Hail to the Thief" and "Hail to the Thief - Live", and the two mistakes are not symmetric: a
leftover duplicate is untidy, whereas merging two real releases hides an album the user then cannot
download at all.

### 5.2 A fill must survive the app being killed

The Feishin store was in-memory on the reasoning that "lb-bot's ledger is in memory too". Wrong
conclusion: quitting mid-download silently stopped tracking the album, and the user noticed. Fills
now persist, pruned on load to a 20-minute window.

For Navic that means a small persisted map — `Settings`/DataStore is enough, this is not Room-worthy
— of `rgid → {releaseMbid, quality, startedAt, settled}`, pruned on read. A rehydrated fill that
finds nothing upstream resolves through the bounded-`unknown` path below.

---

## 6. The fill state machine, and the two traps in it

```
unknown → searching → queued → downloading → placing → placed → verified
                                       ↘ needs_match        ↘ failed
```

- **`placed`** = files are in the library folder. **`verified`** = Navidrome has indexed them. Only
  `verified` answers "is it in my library", but refresh the page on `placed` too — that is what makes
  the album appear the moment Navidrome's scan finishes rather than a minute later.
- Terminal for polling purposes: `failed`, `needs_match`, `verified`. **`placed` is not terminal** —
  the interesting transition is still ahead.

**Trap 1: `unknown` is not terminal either.** It means both "no fill for this release" *and* "the
download POST returned but lb-bot's worker thread hasn't written its first ledger row yet", which is
the normal first second or two after a tap. Treating it as terminal stops the poll before the fill
starts. Give it bounded patience instead — Feishin allows ~18 polls at 5 s (~90 s) and then gives up.

**Trap 2: `placed` can be forever.** lb-bot's verifier gives up after ten minutes and leaves the
state at `placed`. Without a wall clock of your own that is an endless poll, so carry `startedAt` and
stop after ~20 minutes regardless of state.

**Poll interval: 5 s, and no faster.** lb-bot is one Python process with a process-wide lock, and its
own SPA is already polling it every 2 s. The hub deliberately does not cache `/lb/album/status`, so
every poll is a real upstream call.

**Don't poll the task id from the download response.** `_album_download_task` finishes at "slskd
accepted the enqueue", roughly a minute before anything is in the library. Same for the index-artist
POST: rather than polling its task, just re-read the (instant) discography every 5 s for a couple of
minutes and let the section appear when it appears. A miss just means "not yet".

---

## 7. Per-album quality — including what it does *not* guarantee

`POST /lb/album/download` takes an optional `quality`: `flac-any` | `flac-16-44` |
`highest-bitrate` | `prefer-opus`, or omitted for lb-bot's global Source preference. An unrecognised
value is a `400`, not a silent ignore.

The enum is hardcoded client-side rather than fetched: lb-bot's only reader of the list is its prefs
endpoint, which is a *write* surface the hub deliberately does not whitelist. Drift fails loudly at
the 400.

Feishin makes the choice sticky across albums and restarts, which is the right behaviour — someone
who wants CD-standard rips wants them for the next album too, and the global-only setting was
useless precisely because it lived three screens from the decision.

**Known limitation, confirmed in use: this is a ranking preference, not a filter.** Upstream,
`_quality_preference_score` contributes 0–600 to a folder score that runs to ~14000 (name match,
artist match, coverage and year dominate), and in `_file_score` the quality rank is a tie-break
*after* format priority and the live/compilation flags. So a 24-bit/96 kHz folder that matches the
album well will still beat a 16/44 folder that matches it poorly — and when only one source exists,
the preference is moot. Users will occasionally get a format they didn't ask for. Say so in the UI
wording ("prefer", not "only"); don't build Navic on the assumption that the request is honoured.

*If a hard guarantee is ever wanted, it belongs upstream:* a `qualityStrict` flag that makes
`_file_score` return `None` for a non-conforming file, the same way an unaccepted extension does
today. That turns "no hi-res" into "no download" when nothing conforming exists, which is a real
trade-off and the reason it wasn't done by default.

---

## 8. The cross-device refresh

Already built end to end; Navic currently receives it and ignores it.

lb-bot POSTs `<hub>/lb/notify` on placement (enabled by setting `LB_BOT_HUB_URL` and
`LB_BOT_HUB_TOKEN` on lb-bot — same value as `HUB_TOKEN` — and off by default), and the hub fans it
out to every connected client as:

```json
{ "t": "library", "event": "albumPlaced", "releaseMbid": "…", "rgid": "…", "artist": "…", "album": "…" }
```

`HubManager.handleFrame`'s `when (msg["t"])` has no `else` branch, so the frame is harmless today.
Adding the handler is one case: invalidate the artist discography cache and re-read. It carries no
authority — it only says "re-read data you can already read" — so no extra validation is needed
beyond what the socket already provides.

Note the ordering guarantee you get for free: because lb-bot flips the index row *before* it pings,
a client that re-reads on the frame sees the corrected `present` status. And because the index flip
is durable, a client that misses the frame entirely still gets the right answer on its next read.
The ping is a nicety, never a dependency — the hub being down cannot fail a placement.

---

## 9. Navic-specific mechanics

From `navic-build-constraints` — the things that cost time if rediscovered:

- **Cache DB is at v19.** A discography cache entity bumps `CacheDatabase` to **v20**. It uses
  `fallbackToDestructiveMigration(true)` and doesn't export schemas, so a version bump is enough —
  but forgetting it is Room's identity-hash crash on launch. `DownloadEntity` belongs to *both*
  databases; if you don't touch it, `DownloadDatabase` stays at v4.
- **Caching the discography is optional.** The upstream read is an instant SQLite lookup and the hub
  caches it 60 s. Room is worth it only for offline display of the missing list, which is arguably
  pointless — you can't download anything offline anyway. **Recommendation: skip the Room entity
  entirely in the first pass**, keep it in memory in the ViewModel, and revisit. That drops the whole
  v20 question.
- **DI:** `singleOf(::LbBotManager)` — constructor DI auto-wires anything singleton-resolvable
  (`Settings`, `PreferenceManager`), so no module edit beyond the registration.
- **Kotlin block comments nest.** Writing `<hub>/lb/*` inside a KDoc opens a second comment and
  swallows the rest of the file; the compiler reports only "Unclosed comment" at EOF plus a flood of
  unrelated "Unresolved reference" errors elsewhere. Write the route without the asterisk, or as
  `` `/lb/…` ``. (Feishin's TS comments don't nest, which is why the same phrasing is safe there.)
- **Strings** go in `commonMain/composeResources/values/strings.xml`; `Res.string.*` accessors are
  generated at build time, so a typo only surfaces at compile.
- **iOS parity:** adding to an `open fun` is safe if iOS doesn't override it; `abstract`/`expect`
  forces edits in `.ios.kt`. A new standalone manager is safe. iOS targets are **not** built here, so
  a pure-Kotlin error in commonMain surfaces via the Android build but an iOS-specific actual won't.
- **Typecheck:** `./gradlew :composeApp:compileAndroidMain --offline` (~30 s warm). Do **not** use
  `:compileCommonMainKotlinMetadata` — it fails on HEAD for unrelated reasons. Full build:
  `./gradlew :androidApp:assembleRelease --console=plain` (~4 min). Don't pipe through `tee`; it
  masks gradle's exit code.

---

## 10. Suggested order

1. `LbBotManager` + the `/lb/status` probe, wired to nothing. Verify it returns "unavailable" cleanly
   with no hub configured — that is the state most users are in.
2. Read-only discography section on the artist page, with the Navidrome reconciliation (§5.1) and
   newest-first sorting (§4) in from the start. No download yet.
3. The missing-album sheet: editions → tracklist → Download, with the quality selector.
4. The fill watch: persisted store, 5 s poll, both traps in §6, refresh on `placed`/`verified`.
5. The `library` frame handler in `HubManager` (§8) — one `when` case.
6. *Optional, the other half of phase 4:* an Android notification when a fill reaches `verified`.
   Feishin has no toast for this yet either.

## 11. Testing notes

Nothing in any of the three rounds has been run against the live slskd / Navidrome / lb-bot stack —
it is all unit- and build-verified plus one round of real use on Feishin. lb-bot's own
`DOWNLOAD_RELIABILITY_PLAN.md` round carries the same caveat. **A failed download during testing is
more likely an lb-bot-side matching bug than an integration bug** — check lb-bot's web UI before
blaming the client.

Two specific things to watch, both of which were real bugs:

- A filled album must *leave* the missing list, not appear in both.
- An album whose transfers vanish from slskd must still finalize (lb-bot's poller sweep now does this
  once nothing is left in `pending_downloads`, instead of waiting out a 6-hour TTL and requiring a
  Telegram app to be registered). If a fill sits on lb-bot's Downloads tab for more than a few
  minutes with no progress, that is the bug to check first.
