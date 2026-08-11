# Handoff — bring Feishin up to Navic's lb-bot workflow

**Date:** 2026-08-09 · **Status of the other side:** Navic ships all of this and has been used
against the live stack over four rounds of feedback. The hub is done — **no hub change is needed
for anything in this document** (one exception is flagged in §6). lb-bot needs nothing either.

Read alongside `PROTOCOL.md` §15 (the route table and the wire contracts, which is normative) and
`DESIGN-lbbot-client-integration.md` (the original design; six of its claims were wrong and the
doc now carries the corrections inline). `HANDOFF-lbbot-navic.md` is the equivalent document for
the Android side and is now history rather than instruction.

---

## 1. Where Feishin is, and where it needs to get to

| | Feishin today | Navic today |
|---|---|---|
| Missing albums on the artist page | yes, bucketed into the owned release-type sections | yes |
| Download a missing album | **blind** — pick edition + quality, press, hope | **two-step**: find sources → review → pick |
| See the source's actual files before committing | no | yes, each file tagged with the slot it fills |
| Fill gaps in an album you partly own | **absent entirely** | yes, same two-step flow |
| Gap download progress | n/a | per-track count + bar |
| Errors | swallowed to `null`, surface hides itself | typed, shown verbatim, 404 ⇒ "restart the hub" |

Two pieces of work, and they share almost all their machinery: **§3 the source picker** (retrofit
onto the existing download flow) and **§4 gap filling** (new). Do §3 first — §4 reuses its
components and its wire types wholesale, and §3 is the smaller change against code that already
exists.

The user's own framing, which is the acceptance criterion for both:

> "I need to have a peace of mind that I am downloading a correct album because now it's more like
> a blind download."

> "the most important thing tho is to be able to view the actual tracklist in both the new album
> downloading sources and the incomplete album downloads."

---

## 2. Constraints that are not negotiable

- **lb-bot's Flask API has no authentication of any kind and binds `0.0.0.0:8899`.** It exposes
  delete-file, trash and preference-writing routes. It must never be reachable from a client.
- Feishin talks to lb-bot **only** through the hub, at `<hub>/lb/*` with
  `Authorization: Bearer <HUB_TOKEN>`. There is **no direct-LAN fallback** — unlike the AudioMuse
  client next door, which has one because AudioMuse has its own token. This is already how
  `src/main/features/core/lbbot/index.ts` works; keep it that way.
- **The hub's route whitelist is the security boundary.** Do not add routes casually and never add
  a wildcard. Every route this handoff needs is already whitelisted (18 of them).
- **Never call `/api/tasks` or `/api/tasks/<id>`.** Both deep-copy lb-bot's entire multi-MB review
  state under a process-wide lock. They are not whitelisted; keep it that way. Everything a client
  needs about a running search is on the `sourceTask` field of the gap detail.
- **Never call `GET /api/gaps`** (the whole-library list, unbounded). Not whitelisted either. A
  client only ever acts on a group id that arrived on a discography row.
- **Cover art for unowned releases must not carry the user's Navidrome headers.** Feishin's
  `caaCoverUrl` already builds a plain Cover Art Archive URL and the renderer loads it directly —
  that is correct and is the reason it is not proxied. If you ever route it through something that
  attaches custom headers, you leak a reverse-proxy / Cloudflare-Access token to a third party.

---

## 3. Part A — make the album download reviewable

### What is wrong today

`lbbot-download-album` posts `{rgid, quality}` and lb-bot picks the top-ranked Soulseek folder on
its own. In use this fetched the wrong record for a self-titled album and the wrong quality for
another, and there was no way to know until afterwards. `quality` does not fix this: it is a
*ranking* preference upstream (0–600 points inside a ~14000-point folder score), not a filter, so
a well-matching hi-res folder still wins. Word it "prefer", never "only".

### The route that already existed and no client called

`GET /lb/album/sources?rgid=…` → lb-bot's `GET /api/album/sources`. Ranked folders, shaped exactly
like the Fill-gaps source list, with **coverage computed against the canonical MusicBrainz
tracklist rather than a file count** — lb-bot's own comment says the old count-based coverage
"reported a folder holding a completely different album as a complete match". Slow (a live slskd
fan-out, 30–90 s); the hub gives it the 45 s timeout and a short cache so re-opening the modal
doesn't start another search.

`POST /lb/album/download` already accepts `sourceUsername` + `sourceFolder`, and the hub already
forwards them.

### The flow to build

`missing-album-modal.tsx` becomes two steps:

1. **Choose** — variant/edition (existing) + tracklist (existing) + quality (existing). Primary
   button is now **Find sources**, not Download.
2. **Review** — the ranked list. Each row shows, in this order of importance:
   - **album-match verdict** — `albumMatchOk` as *matches album title* / **different album?**.
     This is the one that catches the self-titled case, where every folder name looks plausible.
   - **coverage** — `coverageDetail.haveTracks` / `.totalTracks`, e.g. "9/12 tracks".
   - **format · bitrate · size** — the quality actually on offer, seen before committing.
   - **flags** (live / compilation / risk) and `peer`, `speedMbps`, `freeSlot`, `queueLength`.
   - **an expandable file list** — `sources[].files`, each with `matchedTo` (the tracklist slot it
     fills) or nothing. *A file matching no slot is the tell that a folder is the wrong album
     however plausible its name.* This route is **not** stripped by the hub, so the files ride
     along with the search — no second call needed here (unlike gaps, §4).

   Pre-select rank 1 (`recommended: true`) so the confident case is one extra tap, not research.

3. Download posts `{rgid, quality?, sourceUsername: peer, sourceFolder: folder}`.

**Say in the UI that the pick is a preference, not a guarantee.** lb-bot floats the chosen peer to
the front of its own ranked list and keeps the rest as failover — if that peer has vanished by
transfer time, the best ranked folder wins instead. Navic has this as a one-line caption under the
source list. Implying certainty we don't have is worse than the sentence.

### Also fix while you are in here

- **Errors are invisible.** `get`/`post` in `src/main/features/core/lbbot/index.ts` return `null`
  on `!res.ok` and the renderer hides the surface. That is right for the *probe* and for the
  discography read; it is wrong for every write. A user pressing a button and getting nothing —
  no message, no log — is exactly the bug that wasted a round on the Navic side, where the real
  cause was that the hub hadn't been restarted and was 404ing. Give the writes a result type
  carrying `{ok, status, error}`, log every non-2xx with route and status, and show lb-bot's own
  `error` sentence verbatim — it writes them for humans.
- **404 does not mean "not found" here.** It means the hub proxies no such route, i.e. the hub is
  older than the app and needs restarting. Translate it to exactly that.
- **`GET /lb/status` now returns a `routes: string[]` capability list.** Use it to say "this hub
  can't do gap filling yet — restart it" instead of failing mutely. Treat an *empty* list as
  "assume supported" (an older hub that doesn't advertise).

---

## 4. Part B — filling gaps in albums the library partly holds

The capability no client had before Navic: the library has 9 of 12 tracks, and lb-bot can fetch
the other 3 into the album that already exists.

### It needs no new plumbing upstream

lb-bot's discography scan doesn't merely *label* a release `incomplete` — it **builds the Fill-gaps
review group as it classifies it** (`_make_review_group`, `source="artist_discography"`, unioned at
`_union_review_groups`). So the `group_id` that arrives on an `incomplete` discography row is a
live handle, with no separate scan and no extra call.

**This means `toRelease()` in the main process is currently dropping the fields you need.** The
discography row carries, for `status: "incomplete"`: `group_id`, `present_tracks`, `total_tracks`,
`missing_tracks`. Add them to `LbBotRelease` (all optional — a `missing` or `present` row has
none) and stop filtering the discography down to `status === 'missing'`.

### Where it lives in the UI

Navic's shape, arrived at after the user rejected the first two:

- The discography is **one cohesive list per release type** — owned, `9/12`-badged, and greyed-out
  unowned, side by side. Feishin already does the owned/missing half of this
  (`groupMissingByReleaseType`); incomplete rows are owned albums, so they are already in the list.
  They just need the badge.
- **Tapping an incomplete album opens the album.** Not the gap modal. The gap is the exception,
  not the headline. (Navic shipped it the other way first and it was wrong.)
- **The gap action lives in the album's context menu**, labelled with the count it already knows:
  "Find 3 missing tracks".

### The modal

Same two-step shape as §3, which is the whole reason to do §3 first:

1. Open on `GET /lb/gap?group_id=…` — tracklist with per-track state, and the caption naming the
   edition (see the trap in §7).
2. **Find sources** → `POST /lb/gap/search {group_id, force?}` → poll `GET /lb/gap` → the ranked
   list, identical rendering to §3's.
3. Pick → `POST /lb/gap/fetch {group_id, sourceId}` where `sourceId` is the source's `id` (its
   index in the list).
4. Progress: **count it off `tracks[].state`**, not a task — a gap fill is per-track and there is
   no single transfer to report. Done = `downloaded | done | skipped`; wanted = every track whose
   state is not `present`; failed = `failed`. Measure the bar against *tracks being filled*, not
   the album's length: progress against 17 when only 12 were queued stalls at 12/17 and reads as a
   hang.
5. Secondary actions: **Pick the best source for me** (`POST /lb/gap/auto` — search, rank and
   commit in one shot; keep it, demoted to a text button), **Cancel** (`/lb/gap/cancel`, while
   downloading), **Re-check album** (`/lb/gap/rescan` — re-reads Navidrome *and* walks the folder
   for files Navidrome hasn't indexed; also the manual reconcile after a fill), and **Allow MP3**
   when `mp3WouldHelp` is set (widens formats for this album only; global policy stays flac/opus).

### The file listing is a separate call here

`GET /lb/gap` is **stripped by the hub**: `sources[].files` and `sources[].filesTruncated` are
removed, because lb-bot embeds every ranked source's entire peer file listing, ten sources to a
page — hundreds of KB on a path a client polls every 5 s. So fetch the listing on demand when a
source is expanded:

`GET /lb/gap/source-files?group_id=…&source_index=…` → `{ok, expanded, files[], filesTruncated,
fileCount, coverage, coverageDetail}`.

**`expanded: false` means the peer was unreachable and the rows are the original search hits, not
the real folder.** Say so; do not imply otherwise. Cache per source for the life of the modal.

---

## 5. The 502 — flag this, it is still live

The user still sees `Request failed (502)` intermittently. Two causes are known and one is fixed:

**Fixed (hub `9788512`):** `POST /lb/gap/search` was on the default 20 s timeout. The POST itself
returns immediately — the search is a background task — but it takes lb-bot's process-wide
`_review_lock` on the way out, and a search already in flight holds that lock in bursts. A second
press therefore blocked *in the handler* past 20 s and the hub answered 502 for what was really
"busy". That route now has the 45 s timeout, and Navic disables the button while a search runs.

**Not fixed, and the likely source of the remaining sightings:** *every* lb-bot route takes
`_review_lock`, and the hub's default `PROXY_TIMEOUT = 20` is the only thing separating "slow"
from "502". While a placement or a large search holds the lock, an ordinary
`GET /lb/album/status` poll can exceed it. Flask is `threaded=True`, so this is lock contention,
not request serialization. Also relevant: `PROXY_MAX_INFLIGHT = 4` caps concurrent upstream calls
per proxy, so several polling clients plus one slow write can queue behind each other.

Three things worth doing, in order of value:

1. **Make a transient 502/504 non-fatal in the client.** A poll is going to be retried in five
   seconds anyway; showing a hard error for one failed tick is the actual defect the user is
   reporting. Keep the last good data, and only surface an error after N consecutive failures.
   React Query's `retry` plus keeping `placeholderData` covers most of it.
2. **Distinguish it in the copy.** 502 from this proxy means "lb-bot is busy or unreachable", not
   "your request was refused" — the hub's own body is `{"error": "lb-bot unreachable"}`, which
   read literally is misleading during lock contention.
3. **Only then consider raising `PROXY_TIMEOUT`** for the routes that block on the lock, or adding
   a single hub-side retry. This is a hub change and the one exception to "no hub change needed";
   don't reach for it before (1), which fixes the symptom the user actually sees.

Do not "fix" it by polling faster or by adding a second poll path. lb-bot is one Python process
with its own 2 s-polling web UI already on it.

---

## 6. Wire contracts

All 18 whitelisted routes are in `PROTOCOL.md` §15 with their cache TTLs. The ones this work adds
to Feishin's surface:

| route | upstream | params / body | notes |
|---|---|---|---|
| `GET /lb/album/sources` | `GET /api/album/sources` | `rgid` | 45 s timeout, short cache. **Not** stripped — the files are the evidence. |
| `GET /lb/gap` | `GET /api/gaps/{group_id}` | `group_id`, `sourcePage` | never cached; **stripped** |
| `GET /lb/gap/source-files` | `GET /api/groups/{id}/sources/{idx}/files` | `group_id`, `source_index` | 60 s cache, 45 s timeout |
| `POST /lb/gap/search` | `POST /api/groups/{id}/sources` | `group_id`, `force` | 45 s timeout |
| `POST /lb/gap/auto` | `POST /api/gaps/{id}/auto` | `group_id` | |
| `POST /lb/gap/fetch` | `POST /api/gaps/{id}/fetch` | `group_id`, `sourceId` | |
| `POST /lb/gap/cancel` | `POST /api/gaps/{id}/cancel` | `group_id` | |
| `POST /lb/gap/rescan` | `POST /api/gaps/{id}/rescan` | `group_id` | 45 s timeout |
| `POST /lb/album/download` | unchanged | `+ sourceUsername`, `+ sourceFolder` | already forwarded |

lb-bot speaks **snake_case for index rows** and **camelCase for the screen-shaped views**. Both are
mirrored verbatim on the Navic side rather than normalized, so any field can be checked against its
Python source by name. Feishin normalizes in the main process; keep doing that, but keep the
upstream name in a comment.

Ported from Navic's `LbBotManager.kt` — these are the shapes, already verified against the live
stack:

```ts
type LbBotGapSource = {
    albumMatch: number;          // how much the folder's own name reads as this album
    albumMatchOk: boolean;       // the "is this even the right record" verdict
    bitrate: string;
    coverage: string;            // "9/12 tracks" | "full" | "partial" | "unknown"
    coverageDetail: { haveTracks: number; totalTracks: number; unmatched: string[] };
    coverageFull: boolean;
    fileCount: number;
    files: LbBotSourceFile[];    // album/sources only — always [] on a gap poll
    filesTruncated: boolean;
    flags: string[];             // live / compilation / risk
    folder: string;              // → sourceFolder on download
    format: string;
    freeSlot: boolean;
    id: number;                  // → sourceId on gap/fetch
    peer: string;                // → sourceUsername on download
    queueLength: number;
    rank: number;
    recommendation: string;
    recommended: boolean;        // rank === 1
    score: number;
    size: string;
    speedMbps: number;
};

type LbBotSourceFile = {
    accepted: boolean;           // false when the format is outside lb-bot's list
    bitrate: number;
    durationSec: number;
    ext: string;
    filename: string;
    matchedTo: null | { basis: string; position: string; title: string };
    sizeMb: number;
};

type LbBotGap = {
    album: string; albumId: string; artist: string; id: string;
    allowMp3: boolean;
    canonicalMbid: string;       // the release the gap is MEASURED AGAINST — see §7
    extra: number;               // files the tracklist can't account for
    failDetail: string; failReason: string;
    missingCount: number;
    mp3WouldHelp: boolean;
    noSourceReason: string;
    present: number; total: number;
    sources: LbBotGapSource[];
    sourcesFoundAt: number; sourcesPage: number; sourcesPages: number; sourcesTotal: number;
    sourceTask: null | { current: string; error: string; id: string; label: string;
                         status: string; summary: string };
    status: 'complete' | 'downloading' | 'failed' | 'picking' | 'ready';
    tracks: { downloadError: string; position: number; state: string; title: string }[];
};
// track state: present | missing | picked | queued | downloading | downloaded
//            | failed | skipped | done
```

---

## 7. Traps — the expensive ones, all paid for already

**`search` flips the group to `picking` before it has found anything.** The POST approves the
group's pending missing tracks and *then* starts the background search, so the very next poll reads
`picking` with an empty `sources` list. `picking` normally means "lb-bot wants a human decision in
its own match workspace", which is a terminal, stop-polling state — and treating it as terminal
here settled the watch one tick after the press, so results arriving thirty seconds later were
never read. The modal sat on "asking slskd" until the user pressed again. **Gate every terminal
judgement on `sourceTask.status`: while it is `running` (or `queued`), nothing the group says about
itself is final.** Cost: a full round of user feedback.

**lb-bot ends tasks `complete` or `error` — never `finished`.** Navic's "no sources found" branch
tested for `"finished"` and could never fire, so an empty search looked identical to one still
running.

**`picking` *with* sources is not a hand-off.** Now that the client has a picker, that state means
"your move" and must be worded that way. Only `picking` with no sources is the real "finish it in
lb-bot's web UI" case.

**Never filter discography rows on `status === 'missing'`.** A completed fill flips the row to
`present` while Navidrome still has no album, so the tile falls out of *both* lists — the album
disappears from the page **because the download succeeded**. Keep every unmatched row and render a
non-`missing` unmatched one as in-library-pending. Feishin's `missingReleases()` helper does
exactly this filter today and is fine only while gaps don't exist; §4 changes that.

**The index row is necessary but not sufficient.** lb-bot's index only learns a release-group is
present when something writes that row. A manual copy, or a fill against a different release of the
same group, leaves it saying `missing` while Navidrome plainly has the album. Reconcile against
Navidrome at render time — Feishin already does, in `withoutOwned`. Keep it.

**`unknown` is not a terminal fill state.** The download POST returns before the worker writes its
first ledger row, so `unknown` is both "nothing is filling this" and "it's about to start".
Feishin's `UNKNOWN_POLL_LIMIT = 18` is the right answer; don't simplify it away.

**A backwards fill transition is normal, not an error.** lb-bot reports `downloading` for as long as
a transfer group is pending, which can follow `placing`.

**The gap is measured against your album's own MusicBrainz tag.** `canonicalMbid` is the release
lb-bot compares to, taken from the Navidrome tag — so a library tagged as a 17-track deluxe reports
17 missing slots even when every pressing on offer has 12. That reads as a miscount and the short
download reads as a bug unless the UI names the edition. Navic shows a caption; do at least that.
**A real edition picker for gaps is not currently possible:** `refresh_group_missing` overwrites
`canonical_mbid` from the Navidrome tag on every refresh, so pinning a different edition needs a new
override field in lb-bot. Not done, deliberately, and out of scope here — but it is the honest
answer if the user asks again.

**Two places have to agree when a fill lands** or the album double-lists: lb-bot's index row (it
writes that itself, via `_index_mark_release_present`) and the client's query invalidation.
Feishin's `useLbBotLibraryRefresh` already invalidates both the discography and the albums queries.

**Placement can refuse and it is now retried, but know the shape.** A MusicBrainz 503/timeout parks
that request in a five-minute cooldown during which every call returns empty *without trying*;
placement then refuses with "No MusicBrainz tracklist for release … — refusing to place untagged
files" (correct — the alternative is writing Unknown Artist). lb-bot `85ea302` retries twice with a
short backoff, and the refusal payload now carries `retryable: true` and says the files are safe in
the downloads folder. If you surface fill errors, honour that flag rather than presenting it as
final.

**Don't poll faster than 5 s** and don't add a second poll path — one Python process, one
process-wide lock, its own 2 s-polling SPA already on it.

---

## 8. Files to touch

```
src/shared/types/lbbot-types.ts        + gap/source/file types; LbBotRelease gains the
                                         incomplete fields (group_id, present/total/missing)
src/main/features/core/lbbot/index.ts  + album-sources, gap detail, gap search/auto/fetch/
                                         cancel/rescan, source-files handlers; result type
                                         carrying {ok,status,error}; non-2xx logging;
                                         toRelease() keeps the incomplete fields
src/preload/lbbot.ts                   + the matching bridge methods
src/renderer/features/lbbot/hooks/use-lbbot.ts
                                       + useLbBotAlbumSources, useLbBotGap (with the
                                         sourceTask-gated poll), useGapSourceFiles;
                                         missingReleases() stops being a `missing` filter
src/renderer/features/lbbot/components/missing-album-modal.tsx
                                       → two-step: choose → find sources → review → pick
src/renderer/features/lbbot/components/source-list.tsx        (new, shared by both modals)
src/renderer/features/lbbot/components/gap-fill-modal.tsx     (new)
src/renderer/features/artists/hooks/use-artist-albums-grouped.ts
                                       incomplete rows keep their gap fields through grouping
src/renderer/features/albums/…context menu                    + "Find N missing tracks"
```

Navic's equivalents, worth reading side by side rather than re-deriving:
`navic/composeApp/src/commonMain/kotlin/paige/navic/domain/manager/LbBotManager.kt` (the client and
every wire type, heavily commented), `ui/components/sheets/MissingAlbumSheet.kt` (§3),
`GapFillSheet.kt` (§4), `LbBotCommon.kt` (the shared source/file rendering).

---

## 9. Verification

Restart the hub first — `hub.py` is a long-running process and editing it changes nothing until it
restarts. That alone accounted for a whole round of "the button does nothing" on the Navic side.
Then confirm `GET /lb/status` lists the 18 routes.

Live, in order:

1. A missing album lists real sources; a deliberately wrong-looking one is visibly flagged
   *different album?*; expanding a source shows real filenames with their matched slots, and a
   folder from another record shows files matching no slot.
2. The album that downloads is the one picked, at the quality shown.
3. After it lands, the tile **stays on the shelf** and becomes a real album within a couple of
   minutes.
4. An incomplete album shows `9/12`; tapping it opens the album; its context menu offers
   "Find 3 missing tracks".
5. **Press Find sources exactly once** and leave it: the list appears on its own within ~90 s. This
   is the §7 trap and the single most likely thing to get wrong.
6. Pick a source: per-track progress advances, the bar is against tracks queued, and the album
   fills in.
7. Kill the app mid-fill and reopen — the watch resumes (Feishin's fill store is already
   persisted; do the same for gaps).
8. An intermittent 502 during any of the above does **not** blank the modal or show a hard error.
