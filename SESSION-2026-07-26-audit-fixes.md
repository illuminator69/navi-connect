# Session — Audit fixes: queue naming, takeover, self-transfer, saved-queue integrity (2026-07-26)

Implements the full audit of the hub-managed saved-queue work (`SESSION-2026-07-26-hub-saved-queues.md`):
the four user-reported bugs plus the ~30 secondary defects found tracing them. Plan:
`~/.claude/plans/check-navi-connect-readme-cuddly-floyd.md`.

## Verification status
| Area | Verified? |
|---|---|
| Hub (`hub.py`) | `python tools/test_edits.py` **PASSES** 4/4, incl. a new `test_audit_fixes` covering these changes |
| Feishin | `tsc --noEmit` **clean** on `tsconfig.web.json` + `tsconfig.node.json`; **not dev-run** |
| Navic | `:androidApp:assembleRelease` **BUILD SUCCESSFUL**; **not device-tested** |
| iOS parity | `compileCommonMainKotlinMetadata` is **not runnable on this Windows host** (Gradle refuses; the pre-existing Room/KSP + `Dispatchers.IO` errors it reports are in files this change doesn't touch) |
| End-to-end over the wire | **NOT tested** — needs a live hub + both clients (see "To verify") |

---

## The four reported bugs → what actually fixed them

**1. Navic didn't save the queue name.** Two independent causes, both fixed:
- Navic read `sourceName` off `PlayerUiState.currentCollection`, which is only ever derived
  *asynchronously from the playing song's album* — at publish time it was null or the previous
  queue's. Source metadata is now an explicit parameter threaded from the caller
  (`RemotePlaybackRouter.setQueue/restoreQueue` → `HubManager.loadSessionQueue`), and
  `playCollectionLocal`/`shufflePlayLocal` stamp `currentCollection` synchronously with the queue
  swap. `RadioManager` passes each mix's real kind + a display name.
- The hub froze name-at-birth *unconditionally*, so a record born with a null name could never be
  filled. Now `(prev ?: {}).sourceName or name` — an established value still wins, a hole gets
  backfilled.

**2. A remotely-playing queue wasn't marked "currently playing" (Navic).** The home Continue
Listening row keyed only on `localUiState.savedQueueId` and never consulted the hub session. It now
uses the hub's `savedQueueId` when connected (same rule as `SavedQueuesScreen`), both screens gate
that id on `connected` so a dropped socket can't leave a stale highlight, and an adopted/transferred-in
queue now carries the hub's record id into local state instead of null.

**3. Force-stop Navic → Feishin resumed from its own old position.** The hub kept Navic's final
position correctly; nothing consumed it.
- Hub: auto-promotion on `act:play` sent a bare `do:play`. It now sends `do:load {tracks, index,
  positionMs, play}` (PROTOCOL §5.1).
- Feishin: `adoptIfNoLiveReceiver` early-returned whenever the queue signature matched, never
  applying `session.positionMs/index`. It now aligns to the hub's cursor unless already in sync — and
  only re-claims at its *own* position when it was itself the active device immediately before
  (tracked via `lastLiveActiveId`), i.e. a genuine socket blip.

**4. A reopened Navic never re-synced and couldn't reclaim active.** `adoptIfNoLiveReceiver` pinned
`lastQueueSig = hubSig`, so `publishQueueIfOurs` (which dedupes on that signature) bailed forever —
no `setQueue`, so the hub never promoted it, so it never reported. Now the signature is cleared on
adopt, and `publishQueueIfOurs` has an explicit unclaimed-escape: when nobody is active and we're
playing, publish regardless of signature (throttled 2 s) — that publish *is* the claim.

**5. Transferring to the already-active device reset playback.** `_transfer` skipped the release but
still sent `do:load` at the (up to a report interval stale) session position. It now early-returns,
at most sending `do:play`/`do:pause`. Navic guards client-side too.

## Everything else

**Hub (`hub/hub.py`)** — late/spurious `released` frames are only honoured from the active device or
one with a handoff in flight; transfer requires the `receiver` cap; `_register` claims the new socket
before closing the old one (closing the race that could clear the active slot); report `index` is
clamped and position-only ticks persist on a 10 s throttle; the INTENT_GRACE stale-position test is
now proximity-to-pre-intent (the old relative compare rejected legitimate post-backward-seek reports);
`favorite`/`rating` relay a purpose-built directive instead of echoing the act (which forwarded the
sender's token); `volume` null-guards a missing device; actions needing a receiver reply
`error{no_active_device}` instead of vanishing; the health endpoint no longer leaks session intent;
the WS path is enforced (`/connect`, `/` deprecated).

Saved-queue integrity: field-level non-destructive merge with a key whitelist and length cap;
persisted, capped **deletion tombstones**; the live session's record is never overwritten by an
incoming copy, never evicted, and is always present in the broadcast list; `clear` flushes the cursor
then detaches (`savedQueueId → null`) leaving the record resumable; an empty `setQueue` mints nothing;
deleting the current record clears the session id so it can't be resurrected; `_disconnect` flushes
the cursor unthrottled.

**Feishin** — `do:queueChanged` preserves status/position when the playing track is unchanged
(`setQueue` hard-codes PLAYING and restores from 0); `hubDrivenUntil` is re-armed after the async
`resolveSongs` fan-out; pending seeks expire (10 s TTL) and fire immediately when their target track
is already current; a short post-adopt watchdog re-pauses an in-flight auto-resume; the stream-URL
cache clears on server change; the local `savePlayQueue` mirror stands down while the hub is
connected (the hub owns it); saved-queue rename/delete now apply locally *and* notify the hub;
`mergeFromHub` preserves local-only fields and falls back to the queue's own cover art;
`PlayerShuffle.ALBUM` survives the round trip via `shuffleMode`; restoring a saved queue while a
remote session is active routes to that device at the stored position, reusing the record id; the
device picker's hide toggle is no longer nested inside a disabled button (offline/hidden devices were
un-unhideable) and the active row shows paused vs playing honestly.

**Navic** — reporter guards reset on `welcome` and on `do:release`; the saved-queue sync-up is
isolated so a Room failure can't skip the adopt; `SavedQueueDao.rename` bumps `updatedAt` (offline
renames were losing the newest-wins merge) and `replaceFromHub` keeps a local name the hub record
lacks; saved-queue rename/delete apply locally *and* through the hub.

**Docs** — `PROTOCOL.md`: session shape gains `savedQueueId`/`sourceKind`/`sourceName`, `welcome`
gains `savedQueues`, §7 documents the self-transfer no-op / late-`released` / takeover-load / receiver
cap, §8.3 documents tombstones, the non-destructive merge and the naming rule. `README.md`: §4
corrected (active id is **cleared** on disconnect — it previously contradicted both the code and
PROTOCOL §7) plus the takeover-load, self-transfer and tombstone notes.

## Notes / deviations from the plan
- **F16 (read the live position on `do:release`) was dropped as a non-issue**: `positionMs` is fed by
  every `onPlayerProgress` tick — only the 1 Hz *report* is throttled — so there is no staleness to
  fix. A comment now records that.
- **F3's watchdog is scoped to a 3 s post-adopt window** rather than "whenever activeId is null and
  the hub says paused". The broad form would fight the user: pressing play locally with no active
  device sets `playing` before the hub's promotion round-trips, so the watchdog would have paused
  their own playback.
- **N3's "Now Playing" highlight** wasn't added to the home row: that row *excludes* the playing
  queue by design, so the fix there is the exclusion keying on the hub session (done). The highlight
  already exists on `SavedQueuesScreen`.
- **N4's mirror clear on disconnect** was left out deliberately: `REMOTE_HOLD_MS` intentionally holds
  the remote view across a blip. Gating the UI on `connected` (done) fixes the stale highlight
  without fighting that.

## To verify (live walk-through — not yet done)
1. Play an album/playlist/mix on Navic → the record appears on **both** clients with the right
   name and kind.
2. Play remotely (Feishin active) → Navic's home row drops it and Saved Queues marks it "Now Playing".
3. Force-stop Navic mid-song → press play on Feishin → it resumes at **Navic's** position.
4. Reopen Navic, press play → it claims active, reports flow, Feishin mirrors it.
5. Device picker → transferring to the active device does nothing (no rewind); an offline device can
   be unhidden.
6. Rename offline on Navic → reconnect → the rename survives on both. Delete on one client →
   reconnect the other → it stays deleted.

## Build/verify commands
- hub: `cd hub && python tools/test_edits.py`
- feishin: `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`)
- navic: `set JAVA_HOME=C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot & .\gradlew :androidApp:assembleRelease`
