# Session — Navic: saved-queue parity with Feishin (2026-08-06)

Feishin's saved-queue half went through four rounds of live-fire fixes on 2026-08-01/02
(`SESSION-2026-08-01-feishin-queue-fixes.md`) and Navic got none of them — that doc's verification
table says "Navic | untouched". Since the history is **hub-owned and shared**, the two clients were
rendering the same records by different rules. This pass makes the backend behaviour identical and
the presentation equivalent. Plan: `~/.claude/plans/check-the-readme-md-and-tranquil-lagoon.md`.

## Verification status
| Area | Verified? |
|---|---|
| Hub (`hub.py`) | untouched; `python tools/test_edits.py` **PASSES 4/4** (run to prove the new wire fields don't break merge/upsert) |
| Navic | `:androidApp:assembleRelease` **BUILD SUCCESSFUL** (compile + R8 + package; `lintVitalRelease` skipped — linter, not compilation); **not device-tested** |
| Navic iOS | edited (`MediaPlayer.ios.kt`) but **not compiled** — no Kotlin/Native iOS toolchain on Windows |
| Feishin | untouched |
| End-to-end over the wire | **NOT tested** — see "To verify" |

---

## 1. Identity forked on every replay (and every edit, when adopting)

`newSessionId()` was minted unconditionally at every queue-REPLACE, and nothing ever consulted the
local history — so replaying an album always cut a second card, and the 20-row cache filled with
near-duplicates. `hubSavedQueueIdFor` was worse: an **ordered-prefix** test against the hub's current
session, which a reorder, removal, play-next or shuffle all fail.

- `SavedQueueRepository.findMatching(ids)` — Feishin's 80 % **membership** rule verbatim (best score
  wins, ties to the newer `updatedAt`). Answers synchronously off an in-memory index, because the
  queue-replace paths that need an id are not suspending.
- The index is backed by a new `songIdsCsv` column so it costs one projection query, not twenty blob
  decodes. `primeIndex()` (called from the player's init, after `restoreState`) backfills rows written
  before the column existed.
- `MediaPlayerViewModel.sessionIdFor(songs)` replaces the raw mint at every site:
  `playCollectionLocal` / `shufflePlayLocal` / `playRadio` (Android **and** iOS), `RadioManager.playMix`,
  `HubManager.loadSessionQueue`, `hubSavedQueueIdFor`.
- `hubSavedQueueIdFor` now adopts the hub's record on **set overlap ≥ 50 %** (`HUB_QUEUE_ADOPT_OVERLAP`),
  matching Feishin, then falls through to `findMatching`, then mints.

## 2. Deleting the record you're listening to orphaned the live queue

`upsert`'s cheap path caches "the row for `cachedId` is current". Deleting that row left the belief in
place, so every later tick issued an `UPDATE … WHERE id =` against nothing and the playing queue had
no card until its contents changed.

- `delete`/`deleteOthers`/`clearAll` invalidate the cheap-path cache and the index.
- `MediaPlayerViewModel.restartQueueSession()` (Feishin's `restartQueueSession`): mint a fresh id for
  the queue that's playing and get a record for it immediately. Offline that's a local `upsert`;
  connected it's `RemotePlaybackRouter.republishQueue()` → `HubManager` clears `lastQueueSig` and
  re-publishes, because the publish path dedupes on queue *contents* and what changed is identity.
- Both delete surfaces and "Clear all" call it when the record was the live one.

## 3. Clearing the queue never reached the hub

`player.clearQueue()` only told the hub when a *remote* device was active (`QueueScreen`), and
`publishQueueIfOurs` bails on an empty queue — so a local clear stayed local and the hub went on
serving the session, handing the whole list back to the next client that connected. (Feishin item 12.)

`clearQueue()` now calls `RemotePlaybackRouter.clearSessionQueue()`, which sends `act:clear` gated on
`publishedNonEmptyQueue` — the same `sawNonEmptyQueue` guard Feishin uses, so the momentarily-empty
queue during startup hydration can't wipe a session another device is playing.

## 4. Names: everything generated read "No name"

`RadioManager.playMix` passed the mix's name **only on the remote branch**; the local
`loadRemoteQueue` had no name parameter and set `currentCollection = null`, so every locally started
radio / Mood Flow / journey persisted `sourceName = null`. And `sourceName` was re-read from
`currentCollection` on every full write — which resolves asynchronously from the *playing song* — so
titles drifted or went null mid-session.

- New `PlayerUiState.savedQueueName`, stamped at birth by whoever replaced the queue (collection name,
  or the mix's name), persisted with the blob. `loadRemoteQueue` carries it; `RadioManager`'s local
  branch passes `name ?: defaultMixName(kind)`.
- `upsert` freezes it: `existing?.sourceName ?: state.savedQueueName ?: currentCollection?.name` —
  "established wins, but a null is not established", the same rule as `hub.py:417-421`, so a name that
  resolves a moment late still lands.
- New `savedQueueTitle()` gives Feishin's fallback chain: `name ?: sourceName ?: "<Kind> · N songs"`.
  "No name" is gone from the saved-queue surfaces.

## 5. Artwork crawled through the queue

`coverArtId` was the **current** track's and was rewritten on every progress tick, so a record's art
changed as playback advanced — while Feishin and the hub both freeze it at the queue's origin.

- Dropped from `SavedQueueDao.updateProgress` entirely; the full-write path stamps
  `existing?.coverArtId ?: queue.first().coverArtId`.
- Navic now **sends** `coverImageUrl` (built from `songs[0]`) on all three `setQueue` publishes and in
  `syncSavedQueues` rows, and **reads** the hub's frozen value in `applySavedQueues`
  (`coverArtIdFromUrl` pulls the `id=` out of the Subsonic cover URL; falls back to `songs[0]`, never
  `songs[currentIndex]`). Both clients now show the same art for the same record.

## 6. Sync churn

`applySavedQueues` ran on every `savedQueues` broadcast — which the hub sends on every queue edit
anywhere — and re-encoded and re-inserted all 20 rows, with **one `getSongsByIds` per record**.

- One library lookup for the whole broadcast instead of twenty.
- `replaceFromHub` skips rows whose `updatedAt`, `songCount` and `currentIndex` already match, so a
  broadcast usually writes one row instead of twenty (which also stops `observeAll()` recomposing the
  home and queues screens on every unrelated edit). A transaction around the pass would have bought
  nothing once the write count dropped to ~1, so the DAO stayed an interface.

## 7. Also fixed
- A shuffle/repeat toggle alone was never persisted (not in the change signature, not on the cheap
  path). Both are in the signature now.
- iOS mint sites never set `savedQueueKind`, so every iOS-captured queue — including radio — read
  `"manual"`. They now pass the kind and the name.
- `currentSongName` is stored (new column) so the list can show what a queue left off on without
  decoding twenty blobs.

## 8. UI parity
- **Rows**: cover art instead of a generic glyph; three lines (title / `Now playing · Kind · N songs` /
  current song). The live row is named, not just tinted.
- **Preview queue** (new `SavedQueuePreviewSheet`): track list with the resume row tinted and marked
  "Resumes here", total runtime, Resume / Close — opens scrolled to the resume point. No fetching; the
  record already carries its songs. Wired into the overflow on *both* surfaces.
- **Clear all** (confirm dialog): local clear **plus** a `deleteSavedQueue` act per record, so the hub
  tombstones them and the list doesn't come back on the next connect (Feishin item 14). "Delete other
  queues" kept.
- **Loading state**: `queues` is now `List<SavedQueueEntity>?` (null = not loaded), so the empty state
  no longer flashes on every entry. The empty state gained a second line.
- **Restore errors** are no longer silent: `swapToSavedQueue` reports failure through
  `MediaPlayerViewModel.restoreFailed` and the screen snackbars it.
- **Home "Continue listening"** now includes the playing queue, pinned first and marked, instead of
  hiding it — Feishin's carousel ordering, and the point of a shared history. Cards gained an overflow
  (Resume / Preview / Remove) and long-press.
- Hub-routed rename/delete/delete-others/clear-all live in one `rememberSavedQueueActions()` used by
  both surfaces, so they can't drift.

**Deliberately kept** (Navic ahead of Feishin): the kind filter-chip row, the explicit
"Restore (paused)" vs "Resume playing" split, and confirm dialogs on the bulk deletes.

---

## Files touched

### Navic (commonMain unless noted)
- `data/database/entities/SavedQueueEntity.kt` — `currentSongName`, `songIdsCsv`; `coverArtId` doc now
  says frozen-at-first-track.
- `data/database/dao/SavedQueueDao.kt` — `SavedQueueIndexRow` projection; `getIndexRows` /
  `getRowsMissingIds` / `setSongIds`; `updateProgress` drops `coverArtId`, gains `songName`.
- `data/database/{CacheDatabase.kt → v19, DownloadMigrations.kt}` — `MIGRATION_CACHE_18_19`;
  registered in `di/PlatformModule.{android,ios}.kt`; generated `schemas/…/19.json`.
- `domain/repositories/SavedQueueRepository.kt` — membership index + `findMatching` + `primeIndex`,
  frozen `sourceName`/`coverArtId`, shuffle/repeat in the signature, `invalidateCache`, `clearAll`,
  diffing `replaceFromHub`, `RemoteSavedQueue.currentSongName`.
- `shared/MediaPlayer.kt` — `sessionIdFor` / `newQueueSessionId(songs)` / `restartQueueSession` /
  `restoreFailed` / `resolvePlaceholders`; `loadRemoteQueue` + `swapToSavedQueue` carry the name;
  `RemotePlaybackRouter` gains `republishQueue` / `clearSessionQueue` / `resolveLibrarySongs`.
- `shared/MediaPlayer.android.kt`, `shared/MediaPlayer.ios.kt` — mint sites, `loadRemoteQueue` name
  parameter, `clearQueue` clears the name and (Android) tells the hub.
- `ui/core/PlayerUiState.kt` — `savedQueueName`.
- `domain/manager/HubManager.kt` — overlap adopt, `coverImageUrl` in/out, batched resolution,
  `savedQueueNameFor` / `savedQueueCoverUrl` / `coverArtIdFromUrl`, `publishedNonEmptyQueue`, the three
  new router methods.
- `domain/manager/RadioManager.kt` — the local mix branch names its queue.
- `ui/screens/savedqueues/` — new `SavedQueueFormat.kt`, new `SavedQueueActions.kt`, new
  `components/SavedQueuePreviewSheet.kt`, rewritten `SavedQueuesScreen.kt`, `viewmodels/
  SavedQueuesViewModel.kt` (nullable list + `clearAll`).
- `ui/screens/library/{LibraryScreen.kt, components/Content.kt, components/ContinueListeningCard.kt}`.
- `ui/components/common/ContentUnavailable.kt` — optional `description` line.
- `composeResources/values/strings.xml` — 10 new strings.

### Hub / Feishin / docs
- Untouched. `hub.py` already accepted `coverImageUrl` and whitelists it in `SQ_FIELDS`; `README.md` §4
  already describes the birth-stamped-identity model Navic now conforms to, so no architectural change
  to record.

## Known edges
- **iOS is uncompiled** — no Kotlin/Native toolchain on this machine. The edits are mechanical (mint
  sites + three extra `.copy` fields + two imports) but unverified.
- `positionMs` is still derived from the normalized `progress` float × duration, so resume is
  ~duration/precision accurate rather than exact ms. Unchanged from before.
- `coverArtIdFromUrl` assumes the peer's `coverImageUrl` is a Subsonic-style URL carrying `id=`. Both
  clients build them that way; anything else falls back to the queue's first track, which is where the
  freeze came from anyway.
- `findMatching`'s 80 % rule will merge two genuinely different queues that share most of their tracks
  (e.g. an album and that album minus two songs). That is the intended trade — it's what stops the
  duplicate cards — and it matches Feishin.

## To verify (live walk-through — not yet done)
Hub + Feishin (`pnpm dev`) + the new Navic APK together:
1. Play an album on Navic → **one** card, named after the album, art fixed as tracks advance. Reorder,
   remove, play-next, toggle shuffle → still one card, on **both** clients.
2. Replay that album → the same card refreshes; the count doesn't grow. Relaunch Navic and play it
   again → still no clone.
3. Start a local radio / Mood Flow / Song Journey on Navic → the card reads "Radio" / "Mood Flow" /
   "Journey" with its seed name, **not** "No name".
4. Delete the record that's playing → a fresh card for that same queue reappears within a second, on
   both clients.
5. "Clear all" while something is playing → the list empties on both, stays empty across a reconnect,
   and one card for the still-playing queue comes back.
6. Clear the queue on Navic (queue sheet, no remote device active) → Feishin's session is empty too.
7. `…` → Preview queue on a Feishin-captured record → track list + runtime render, the resume row is
   marked, Resume plays from it.
8. Home: the playing queue is the first "Continue listening" card and is marked; its overflow resumes,
   previews and removes.
9. Toggle shuffle mid-queue, leave, come back → the saved queue restores shuffled.
10. Enter Saved Queues on a cold start → a spinner, not a flash of "No saved queues yet".

---

# Second round — first live run of both clients (2026-08-07)

Four problems visible the moment the two clients were put side by side, plus a cover-art flicker.

## 9. Restoring any hub-derived queue threw "Cannot read properties of undefined (reading 'includes')"

`placeholderSong` (`resolve-songs.ts`) never set `_itemType`, and `mapHubSavedQueue` builds **every**
song of a hub record as a placeholder. `use-scrobble.ts` does `song._itemType.includes('song')`
unguarded, so the throw came the instant a restored stub became the playing song — i.e. every restore.

- `placeholderSong` now sets `_itemType: LibraryItem.SONG`.
- `ensurePlayableSongs` re-resolves on `!song.albumId` as well as `!song._serverId`. Stubs *do* carry
  a `_serverId`, so the old test matched none of them and cross-client restores played with no album,
  no container and no real duration.

## 10. Feishin showed empty placeholders where Navic showed art

The cards passed the record's `coverImageUrl` straight through as an image `src` — and that URL is
whichever client stamped it, carrying **its** server address and **its** Subsonic auth salt. Feishin
was being asked to render Navic's authed URL.

Both clients now derive card art from the record's **resume track id** and build the URL with their
own credentials. `coverImageUrl` is no longer read by either client, and Navic no longer publishes it
(the hub field stays in `SQ_FIELDS`; nothing depends on it).

## 11. Titles were inconsistent, stale, and duplicated the subtitle

Two causes. `resolveQueueSource` **synthesized** names like `"Queue · 12 tracks"` and stored them as
if they were a real origin — so the count froze at birth (a record titled "12 tracks" listing 19) and
the string repeated the subtitle word for word ("Manual · 29 songs" over "Manual · 29 songs"). And
titling by origin was unhelpful anyway: nothing on a card said what pressing it would play.

**A card now represents its resume point.** Title = the user's name if set, else the track that will
play on restore; artwork = that same track; second line = `[Now Playing ·] Kind · N tracks`; third
line = `from <origin>`, only when the origin isn't already the title. `resolveQueueSource` returns a
name only when there is a real one (album / playlist / radio seed), and both clients filter
already-stored synthesized names out at display time (`SYNTHESIZED_NAME` / `realSourceName`).

Consequence: `coverArtId` follows the cursor again (it is back on `updateProgress`), and
`SavedQueueEntity.coverArtId` is documented as the resume track's art, not frozen origin art. The
*name* stays frozen; only the art and title follow the playhead.

## 12. Feishin was hiding records Navic showed

`queues.filter(q => q.serverId === serverId)` in both the route and `useContinueListening`. Navic's
rows carry **no** serverId (single-server setup, no such column over there), so any record that
reached Feishin before its server id could be stamped was invisible for good. An empty serverId now
means "unknown", not "some other server".

## 13. A cover "popped" instead of animating — duplicate shared-element keys

Symptom: on the library home, open an album that appears in **two** rows (e.g. both "Recently played"
and "Frequently played") and come back. One copy slides back correctly; the other — in the row you
never touched — also animates, and one of the two snaps into place instead of easing. Albums that
appear in only one row are fine. Reported live and confirmed frame-by-frame from a screen recording.

`ArtGridItem` builds its shared-element key as `"$tab-$id-cover"` (`ArtGrid.kt`), and all five home
rows passed the same `tab = "library"`. An album that is both recently and frequently played therefore
registered **one key twice, both visible at once** — undefined behaviour for `SharedTransitionScope`,
which then drags the wrong element into the transition and leaves the other unmatched.

Fix: each row gets its own scope — `library-recent` / `library-frequent` / `library-newest` /
`library-playlists` / `library-artists`. That string already flows into `Screen.CollectionDetail`, and
the detail header rebuilds the key from it (`HeadingRow.kt`), so the tapped copy still matches its
destination exactly and the other row's copy is simply not part of the transition. Documented on the
`tab` parameter so it doesn't get collapsed back to one value.

### Rejected: a Coil memory-cache-key change (built, then reverted)

The first attempt blamed Coil: `CoverArt` must set an explicit `memoryCacheKey` (the Subsonic URL
carries a random auth salt, so it never hits), the key was the bare cover id with no size in it, and
`MemoryCacheService.isCacheValueValidForSize` rejects a cached bitmap smaller than the request target.
A `CoverArtSize` tier was added and threaded through every call site. It was wrong, and it is reverted:

- **The size-collision effect is real but self-limiting.** A larger consumer meeting a smaller cached
  bitmap costs *one* extra decode, then the larger bitmap is cached and satisfies both. It does not
  loop and it does not flicker.
- **The animation theory was simply false.** `ConstraintsSizeResolver.size()` is a suspend function
  called once per request execution; it awaits only the first non-zero constraints. Later size changes
  update `latestConstraints` but never call `AsyncImagePainter.restart()` — only an `_input` change
  does. A shared-element bounds animation cannot re-trigger a load.
- The cost was real: fixed tiers meant ~3.8× the bitmap memory for 50dp row covers and 16 MB for the
  now-playing artwork, to save one decode.

Worth remembering: `CoverArt` is used at ~25 sizes with one memory-cache key, so a cover *can* be
decoded twice. It is a minor inefficiency, not a rendering bug, and not worth paying for in memory.

## Wording, and the buttons
"tracks" everywhere (new `count_tracks` plural on Navic, replacing "songs"), and the `manual` kind is
"Queue" on both (Navic said "Manual"). "Clear all" is no longer a second full-width red slab under
"Delete other queues" — it's a neutral button with error-coloured text, and the warning lives in its
confirm dialog.

## Round-two verification
| Area | Verified? |
|---|---|
| Hub | untouched; `test_edits.py` **4/4** |
| Feishin | `tsc --noEmit` **clean** on both `tsconfig.web.json` and `tsconfig.node.json`; **not dev-run** |
| Navic | `:androidApp:assembleRelease` **BUILD SUCCESSFUL**; **not device-tested** |

## Round-two live checks
1. Restore a queue Navic captured, from Feishin → it plays; no "Cannot read properties of undefined".
2. Every card on both clients shows artwork — including records the other client created.
3. Card titles name the track that will resume; no "Manual · 29 songs" doubled onto its own subtitle,
   and no title whose track count contradicts the line under it.
4. Both clients list the **same** records, in the same order.
5. Find an album present in **two** home rows (recently + frequently played), open it from one of
   them and go back: only the copy you opened animates, it eases rather than pops, and the copy in
   the other row doesn't move at all. Repeat from the other row.

## Build/verify commands
- hub: `cd hub && python tools/test_edits.py`
- navic: `set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot & .\gradlew :androidApp:assembleRelease`
  (add `-x lintVitalRelease` to skip the slow linter)
