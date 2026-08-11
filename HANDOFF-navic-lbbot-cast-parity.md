# HANDOFF — Navic: lb-bot safeguards + Chromecast scrobbling

**Written 2026-08-09**, after a round of Feishin fixes driven by live use. Everything here is a
gap **Navic has today** that Feishin no longer has. Nothing in it is speculative parity work: each
item is a defect that was observed, diagnosed, and fixed on the desktop side, plus the one backend
change those fixes required.

Read §1 first — it is a **backend contract change that is already deployed in source** and that
Navic must adopt or it keeps the bug. The rest is ordered by how much it hurts.

Navic is *ahead* of Feishin in several places (its `LbError` model, its cast stand-down
arbitration, its correct `present`/`total` parsing). Those are called out where relevant so nobody
"fixes" them into parity with the worse implementation.

---

## 1. The resolved-edition contract (P0 — the download is broken without it)

### What went wrong

`GET /lb/album/sources → 400` and `POST /lb/album/download → 400`, both answering
`{"error": "Could not resolve album"}`, repeatedly, for minutes, with no way forward from the UI.

Both routes call `mbz_resolve_album(rgid)`, which calls `mbz_get`. `mbz_get` parks a transient
MusicBrainz 503/timeout in `_mbz_fail_until` for `MBZ_FAIL_COOLDOWN = 300s` and, inside that
window, **returns `{}` without asking MusicBrainz again** (`listenbrainz_bot.py:2057`). That policy
is right for a background scan and catastrophic for a button: one hiccup takes that album's
download *and* its source search out for five minutes, and every retry is answered instantly with
the same 400, so it doesn't even look like a transient failure.

There is a second, quieter bug in the same place. `mbz_resolve_album` picks "official, earliest" on
its own — so **the edition picker in both clients was decorative**. You chose a Deluxe; lb-bot
downloaded whatever it liked and neither told you.

### The fix (already made, in three repos)

- **lb-bot** (`listenbrainz_bot.py`): `/api/album/download` now prefers a caller-supplied
  `release_mbid` over re-resolving:
  ```python
  resolved = (data if data.get("release_mbid")
              else (mbz_resolve_album(rgid) if rgid else data))
  ```
  `/api/album/sources` takes the same override as query params
  (`release_mbid`, `artist`, `album`, `total`). `rgid` still rides along in both — placement uses
  it to flip the index row (`_index_mark_release_present`), and dropping it would leave filled
  albums showing as missing.
- **hub** (`hub/hub.py`): the two existing whitelist entries gained those fields. **No new route,
  no wildcard** — the security boundary is untouched.
- **Feishin**: sends them.

> ⚠️ **Both the hub container and lb-bot must be restarted.** Until then the hub silently drops the
> new fields (`_filtered_body` / param whitelist) and you get the old behaviour — not an error.
> This is the single most common way a change in this stack appears not to work.

### What Navic must do

`LbBotManager.albumSources` (`LbBotManager.kt:312`) and `LbDownloadRequest`
(`LbBotManager.kt:792`) send only `rgid`. Both need the release the sheet has already resolved.

`MissingAlbumSheet` already tracks `editionMbid` (`MissingAlbumSheet.kt:126`, and the
variant/edition selectors below it), so the value is in hand — it is simply not sent.

```kotlin
// LbBotManager.kt
suspend fun albumSources(rgid: String, edition: LbResolvedEdition? = null): LbResult<LbAlbumSources> {
    if (rgid.isBlank()) return LbResult.Failed(LbError.Rejected(400, ""))
    return getJson("/lb/album/sources", buildList {
        add("rgid" to rgid)
        edition?.let {
            add("release_mbid" to it.releaseMbid)
            add("artist" to it.artist)
            add("album" to it.title)
            add("total" to it.totalTracks.toString())
        }
    })
}

@Serializable
private data class LbDownloadRequest(
    val rgid: String,
    // The pressing the user picked. lb-bot honours it over re-resolving the
    // release-group, which both fixes the five-minute failure cooldown and makes
    // the edition picker mean something.
    @SerialName("release_mbid") val releaseMbid: String? = null,
    val artist: String? = null,
    val title: String? = null,
    @SerialName("total_tracks") val totalTracks: Int? = null,
    val quality: String? = null,
    val sourceUsername: String? = null,
    val sourceFolder: String? = null
)
```

Track count: prefer the loaded tracklist's size for the exact edition, else `LbVariant.trackCount`.
It sizes lb-bot's slskd folder search, so a wrong number costs match quality, not correctness.

**Do not** send `release_mbid` without `rgid`. lb-bot's `resolved = data` branch would work, but
the task then carries no release-group and the index row never flips to `present` — a filled album
double-lists forever.

---

## 2. lb-bot integration — safeguards Navic lacks

### 2.1 A 502 is "busy", not "unreachable" (P1)

Every lb-bot route takes its process-wide `_review_lock`, and the hub's proxy timeout is the only
thing separating *slow* from *failed*. When a source search holds that lock, an ordinary poll times
out and the hub answers **`{"error": "lb-bot unreachable"}`** — which `LbErrorLine` shows verbatim
(`LbBotCommon.kt:42`, `LbError.Rejected` → upstream's own words). Taken literally that is wrong,
and it sends you looking at a service that is working fine.

Feishin translates before showing (`describeFailure`, `src/main/features/core/lbbot/index.ts`):

- `404` → "This hub does not know that route — restart the hub to pick up its newer lb-bot
  features." Navic **already has this** as `LbError.RouteUnknown` (`LbBotManager.kt:179`) — good,
  keep it.
- `502` / `504` → "lb-bot is busy or unreachable. It handles one request at a time while searching;
  try again in a moment."
- everything else → lb-bot's own sentence, which it writes for humans.

Add the 502/504 case to `failureFor` (`LbBotManager.kt:170`) as its own `LbError` variant — not as
a string, so the sheets can decide whether it is worth surfacing at all (see next).

### 2.2 Absorb transient poll failures before saying anything (P2)

`refreshGap` already keeps the last good gap on failure (`LbBotManager.kt:501` only writes `_gaps`
on `Ok`) — that part is right. But `GapFillSheet.act`/`load` set `error` on *any* failed result
(`GapFillSheet.kt:100-118`), so one 502 tick during a search paints an error line under a sheet
that is working perfectly.

Feishin absorbs `TRANSIENT_FAILURE_LIMIT = 2` consecutive failures on polled reads before
surfacing anything (`useToleratedResult` in `use-lbbot.ts`). Do the same for the *poll*; keep
showing the first failure immediately for anything the **user pressed** — a silent button is the
bug this whole error model exists to prevent.

### 2.3 The double-fetch window (P1)

`GapFillSheet` gates its actions on `gap.status != "downloading"` (`GapFillSheet.kt:198`, `:203`,
`:219`, `:237`). lb-bot enqueues on its own thread, so between the fetch POST returning and the
group's status flipping there is a window of a tick or two in which the sheet still looks idle and
a second fetch can be fired into the same album.

Feishin holds a `committed` flag from the POST until either `busy` becomes true or 30 s pass —
released on a timer *deliberately*, so a fetch that quietly didn't take gives the button back
instead of dying disabled.

> **Do not** fix this by treating track state `picked` as busy. That was Feishin's bug and it cost a
> round of feedback: `_TRACK_STATE_BY_DECISION` (`listenbrainz_bot.py:~12447`) maps decisions
> `approved` and `source_pending` to `picked`, and `source_pending` is what the **search** sets on
> every missing track *before* it starts looking. `picked` is precisely the state in which the user
> must be able to choose a source. Only `queued` and `downloading` are real transfers.

### 2.4 Progress is invisible for the first minutes of a fill (P2)

`GapStatusLine` renders its counter and bar only when `gap.status == "downloading"`
(`GapFillSheet.kt:322`), and the discography tile's badge is the same shape. lb-bot flips that
status well after the first track is queued, so a fill that is genuinely running shows nothing at
all for the opening minutes — which reads as "my tap did nothing" and invites the double fetch
above.

Gate on "anything is in flight" instead: any track in `queued`/`downloading`, or a live
`sourceTask`, or the local committed flag. The per-track counts (`tracksDone`/`tracksWanted`) are
already correct and already measured against the tracks being filled rather than the album's
length — keep that.

### 2.5 No way to rescan a discography (P1)

`DiscographyShelf` offers indexing only when `!ui.indexed` (`DiscographyShelf.kt:71`) and returns
early otherwise. `LbDiscography.stale` is parsed (`LbBotManager.kt:825`) and **never read**.

The failure this produces: a scan that matched the wrong MusicBrainz artist, or that MusicBrainz
answered thinly, writes a perfectly fresh index holding nothing — and the one page that could fix
it offers no button. The artist just has an empty discography forever. (Feishin had exactly this;
it now always offers the action, labelled *Find missing albums* when the index is empty and
*Rescan discography* otherwise.)

Two parts:

1. Always render the action when an MBID exists. A rescan is idempotent; the only cost of an
   unnecessary one is a minute of MusicBrainz's patience.
2. **Watch `scanned_at`, not `indexed`.** `indexArtist`'s poll stops at `data?.indexed == true`
   (`ArtistDetailViewModel.kt:293`), which for an already-indexed artist is true on the first tick —
   so the spinner clears while the walk is still running, i.e. exactly the case a rescan button
   exists for. lb-bot already returns `scanned_at` in the discography payload
   (`listenbrainz_bot.py:~4388`); add it to `LbDiscography` and finish on
   `scannedAt > scannedAtBefore`. The same rule covers a first scan, where the previous value is 0.

### 2.6 Cosmetic, low priority

The tile badge reads `9/12` (`DiscographyShelf.kt:176`). Feishin now says `3 missing` — the number
the badge exists to answer, rather than the one it can be derived from. Take it or leave it; the
data is identical.

---

## 3. Chromecast

### 3.1 Nothing scrobbles a cast session (P1 — Navic's half of a shared gap)

In this protocol the **receiver** holds the Navidrome credentials and reports its own plays. A
Chromecast is a receiver that holds no credentials and cannot be taught to, and every controller
watching the session is only a mirror. So an evening cast to a speaker records **nothing** — no
play counts, no recently-played, no ListenBrainz. Navic's `ScrobbleManager` is driven by
`ScrobblePlayerSource` (its own local player), so it is silent for the whole session.

**The rule, and it matters: only the bridging client scrobbles.** Scrobbling from "any controller
watching a cast session" double-counts the moment a second client is left open on it. Speaker
ownership is already arbitrated to exactly one client (PROTOCOL §12.2), so "am I the bridge for the
active device" designates one client and no other.

Feishin implements this with a new `cast-bridged-devices` IPC and
`features/player/hooks/use-cast-scrobble.ts`. **Navic already has the state it needs in
commonMain**: `CastBridgeStatus.speakers` exposes `CastSpeaker(id, name, state)` with
`CastBridgeState.BRIDGING` (`CastBridgeStatus.kt`). The gate is:

```
hubManager.activeDeviceId is a speaker whose state == BRIDGING
```

Measurement mirrors the local scrobbler and Feishin's: accumulate **listening time**, not playhead
position, sampling the hub's ~1 Hz progress mirror; a delta larger than ~5 s is a seek or a stall
and buys no credit. Reset on track change (key on queue index *and* track id, or a repeat-one queue
scrobbles once and never again). Submit at the user's existing percentage/duration thresholds, and
treat a zero duration threshold as "no duration rule" rather than "scrobble immediately" — the
accumulator starts at zero and clears a `>= 0` test on the first sample.

Send the now-playing (`submission = false`) on track change too, so the server shows the speaker's
track as live rather than silently accruing a play count at the end.

`ScrobbleManager` is structured around a single `playerSource`, so the cleanest shape is a second,
hub-driven source rather than branching inside it.

### 3.2 Asymmetry worth knowing: Feishin arbitrates only half-way

Navic implements PROTOCOL §12.2's arbitration in full — the online-`cast-<id>` check, the jittered
claim, and the 5-minute `standDownUntil` circuit breaker after a `4003`
(`CastBridgeManager.kt:136`, `:165`, `:195`). **Feishin's bridge still registers unconditionally**
(`src/main/features/core/cast/index.ts`, `connectHub`): it has no pre-registration check, because
the bridge socket *is* the device and so cannot read `devices` before claiming.

It does now honour the circuit breaker (added 2026-08-10, after two Feishin instances flapped
against each other roughly once a second). Steps 1–3 of §12.2 remain unimplemented on the desktop
side, so a collision still happens on start — the breaker just settles it in one round instead of
never. Two Feishin instances therefore no longer fight, and Navic continues to stay out of the way
when a desktop is bridging.

Note the failure mode the breaker fixes, because it is not obvious from the log: `connectHub`'s
`open` handler resets `backoffMs`, so a superseded bridge that reconnects successfully has its
backoff reset before being kicked again. The exponential backoff can never engage, and the war runs
at full speed indefinitely.

### 3.3 Verify: elapsed time while casting

On Feishin the playerbar's elapsed-time label read the **local** player's clock, which is frozen
while a Chromecast plays: the bar advanced and the time sat on the same figure for every song.

Navic pushes an interpolated remote position into the player UI state as a fraction
(`HubManager.pushRemoteProgress`, `HubManager.kt:554`), which the now-playing slider reads —
so this is probably already correct. **Check it anyway**, and check anything that formats a
*time* rather than a fraction: the Feishin bug was invisible precisely because the slider next to
the label was right.

---

## 4. Deliberately not here

- **Feishin's release-type filter** (hiding compilations from an artist page). A desktop
  affordance for artists with 300 releases of which most are compilations. Navic's shelf is
  sectioned already; port it only if the same complaint arrives on mobile.
- **Anything touching lb-bot's match workspace**, `/api/tasks`, or `GET /api/gaps`. Still off the
  wire, still not whitelisted, and `picking`-with-no-sources still correctly hands off to lb-bot's
  own UI.
- **Raising the hub's `PROXY_TIMEOUT`.** Still the last resort it was in the Feishin handoff.

---

## 5. Verification (against the live stack, in this order)

1. **Restart the hub, then lb-bot.** Confirm `GET /lb/status` lists the routes.
2. Open an album sheet, pick a non-default edition, press *Find sources* — lb-bot's log should show
   no `release-group/` lookup for that rgid, and the folders offered should match the pressing you
   chose.
3. Download it; confirm the release lb-bot reports back is the edition you picked.
4. Force the old failure: with MusicBrainz cold (or straight after a 503), the same album should
   still download rather than 400 for five minutes.
5. On an artist whose index is empty but `indexed`, confirm the rescan action exists and its
   spinner survives until `scanned_at` moves.
6. Start a gap fill: progress must appear within a poll or two, not minutes; the fill button must
   not be pressable twice; a single 502 tick must not paint an error.
7. Cast to a speaker Navic is bridging, play a full track, confirm the play count moves — then do
   the same with Feishin also running (Navic should be `BRIDGED_ELSEWHERE`, and must **not**
   scrobble).
