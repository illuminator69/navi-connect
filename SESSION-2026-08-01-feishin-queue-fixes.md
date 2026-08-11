# Session — Feishin: auto-advance stall + saved-queue restore/naming/forking (2026-08-01)

Four live-use bugs in the (never dev-run) hub-owned saved-queue work from
`SESSION-2026-07-26-*.md`, plus a "Preview queue" addition. Plan:
`~/.claude/plans/check-the-readme-md-in-idempotent-codd.md`.

## Verification status
| Area | Verified? |
|---|---|
| Hub (`hub.py`) | `python tools/test_edits.py` **PASSES 4/4**, incl. new cover-freeze + reorder-no-fork assertions |
| Feishin | `tsc --noEmit` **clean** on `tsconfig.web.json` + `tsconfig.node.json`; **not dev-run** |
| Navic | untouched (hub records are shared; no client change needed) |
| End-to-end over the wire | **NOT tested** — see "To verify" |

---

## 1. "The next song doesn't start unless I start it from the queue"

`adoptIfNoLiveReceiver` pinned `lastQueueSig = hubSig`. `publishQueue` dedupes on that signature, so
after an adopt Feishin could never publish → the hub never promoted it to active → `report()`
no-op'd → the hub's `session.index/positionMs` stayed frozen at the adopt point. Every later
`session` frame re-entered the adopt path, saw the local index ahead of the hub's stale one, and
`mediaPlayByIndex(staleIndex)` + `armSeek(pause)` + `mediaPause()` + armed the 3 s pause watchdog.
Auto-advance worked; the hub glue undid it a moment later. (This is the Navic bug fixed in
`SESSION-2026-07-26-audit-fixes.md` §4 — never applied to Feishin.)

- `publishQueue` gains an **unclaimed-escape**: `activeId === null && playing` publishes regardless
  of the signature (2 s throttle). That publish *is* the claim.
- The adopt paths no longer pin `lastQueueSig` (all three set `''`).
- The same-queue branch now treats **any** local playback as "we own this session" (re-claim at our
  position) instead of only `lastLiveActiveId === myId` — so it can never rewind a running engine.
  `lastLiveActiveId` is gone with it.
- A track change no longer reports the outgoing track's `positionMs` with the new index (it reset
  to 0 — except while a hub-driven load is positioning us, where the hub's offset is the truth).

## 2. Restoring a saved queue "loaded forever"

`mapHubSavedQueue` built `QueueSong` stubs with **no `_serverId`**, and `useSongUrl` is
`enabled: Boolean(song?._serverId)` → no stream URL → the player sat in PLAYING with no source.
`mergeFromHub`'s wholesale replace meant even locally-captured records got stripped.

- New shared `features/hub/utils/resolve-songs.ts` (`placeholderSong`, `resolveHubTracks`), extracted
  from `use-hub`'s `resolveSongs` so the `do:load` path and the saved-queue path can't drift.
  `mapHubSavedQueue` now builds real placeholder Songs through it.
- `mergeFromHub` keeps the local `songs` array when it holds the same ids as real library songs.
- `useRestoreSavedQueue` re-resolves any song lacking `_serverId` before playing, returns
  `{ isRestoring, restore }`, `await`s the remote branch (was `void`-ed, so failures vanished) and
  toasts on error. Both card surfaces disable while restoring.

## 3. Names and artwork looked random

`savedQueueTitle` fell back to `currentSongName`, which `updateProgress` rewrote on every progress
tick — so cards renamed themselves as playback moved. `sourceName` was almost always absent (only
radio/journey/CLAP sites marked a source).

- Title is now `name || sourceName || "<Kind> · N tracks"`. The current song stays as the third line.
- `resolveQueueSource` always yields a name: single album → album; single artist → `Artist · N
  tracks`; else `Queue · N tracks`.
- `coverImageUrl` is stamped once at birth (queue's first track) and dropped from
  `SavedQueueProgress`; `upsert` also preserves `sourceKind`/`sourceName`/`coverImageUrl` on refresh.
  Cards key their `ItemImage` on `songs[0]` rather than the current song.
- Playlist plays announce the playlist name (`playlist-detail-song-list-header.tsx`).
- Hub: `coverImageUrl` accepted on `act:setQueue`, frozen in `_upsert_saved_queue` with the same
  "established value wins, a null is not established" rule, and added to `SQ_FIELDS`.

## 4. The playing queue forked into a new history entry on every edit

Both identity checks were ordered-prefix tests, so a reorder, a removal, a play-next or a shuffle
minted a fresh UUID → the hub upserted a new record → near-duplicates evicting real history (cap 20).

**Identity is now an explicit session, not a track list.** `saved-queue-source.ts` gains
`beginQueueSession(kind, name?, id?)` / `isNewQueueSessionPending()` / `consumeQueueSession()`;
`player-context.tsx` announces one centrally whenever the queue is *replaced* (`Play.NOW`/`SHUFFLE`,
both `addToQueueByData` and `addToQueueByFetch` — `addToQueueByListQuery` funnels through them),
keeping whatever name a call site already announced. `resolveSavedQueueId` returns the existing id
unchanged unless a new session is pending; its hub-adopt branch now matches on **set overlap ≥ 50 %**
rather than an ordered prefix, and no longer leaves the previous queue's name on an adopted record.
The offline capture path follows the same rule. `disconnected` no longer clears `savedQueueId` (a
socket blip was forking the queue you never stopped playing).

## 5. Preview queue

New shared `features/saved-queues/components/saved-queue-preview-modal.tsx` — track list with the
resume row marked, total runtime, Resume/Close — wired into the `...` menu on both the Continue
Listening carousel and the Saved Queues route. No fetching: `entry.songs` already carries the
metadata.

---

## Second round — OS integration + cast reload loop (same day, after live testing)

### 6. Cast device reloaded the current track every 1–2 s
The bridge log showed `loading "Pur" @ 0ms` → `load ok → PLAYING` on a ~1.5 s cadence forever.
`routeLocalPlayToRemote` fires on **any** local PLAYING event while a remote device is active and
unconditionally sends `act:setQueue { positionMs: 0 }` — so every stray local play event (a watchdog
pause losing the race with the audio engine, an auto-resume, a media-key round trip) restarted the
cast device from zero. It now **no-ops when the hub already holds this exact queue** and just
silences the local player; only a genuinely different queue is published.

This is also the most likely source of the reported Auto-DJ history duplicate: a `setQueue` storm
while the queue is mid-top-up can land with `savedQueueId` unresolved and mint a second record.

### 7. Media hotkey *unpaused* remote playback when the app was unfocused
The media session reported the **local** engine, which is paused by design during remote playback.
Windows therefore believed we were paused and sent `play` on the play/pause key — unpausing the
remote session. In-app controls were correct because they read the remote-aware state.
`use-media-session.ts` now publishes `getRemoteAwareSnapshot()` (new non-hook accessor in
`use-remote-aware.ts`) and subscribes to the hub store too, with change-guards so the ~1 Hz progress
mirror can't re-assign `MediaMetadata` every second.

### 8. Stale now-playing thumbnail + "paused" in the title bar during remote playback
Same root cause, same fix: `window-bar.tsx` (title, status, queue position) and
`use-native-menu-sync.tsx` (play/pause menu item) now follow the session rather than the local
player. The OS thumbnail follows from the media-session metadata fix.

*Caveat to check live:* with no local audio element playing, Windows may still drop the SMTC session
entirely. If the hotkey does nothing (rather than unpausing), that's the next thing to chase.

### 9. Offline capture could still fork a record
If the socket dropped mid-listen, the offline capture hook minted a fresh UUID for music that never
stopped. It now continues the hub's current `savedQueueId` unless a new play was actually announced.

---

## Third round — the OS transports were never remote-aware at all (2026-08-02)

Round 2 fixed `use-media-session.ts`, but that file is **inert on this setup**: `mediaSession` and
`globalMediaHotkeys` are mutually exclusive in settings (`window-hotkey-settings.tsx`), the defaults
are `mediaSession: false` / `globalMediaHotkeys: true`, and a media key that works while the app is
unfocused means the *globalShortcut* path is the live one.

### 10. Media keys / tray / thumbar drove the local engine
`use-main-player-listener.tsx` took its transports from `usePlayerActions()` — the raw store —
while the playerbar uses `usePlayer()`, the context that routes to the active remote device. So
every OS-level control (media keys via `main/features/core/player/media-keys.ts`, the tray and
Windows thumbar items, the macOS dock menu) toggled the local player, which is paused by design
during remote playback. Switched to the context; the mpv error handler keeps the store's local
`mediaPause` on purpose.

### 11. OS now-playing metadata was gated on a setting that governs something else
`useMediaSession` skipped every metadata/`playbackState` write when the media-session setting was
off — so with global hotkeys enabled the OS overlay kept whatever Chromium derived from the local
`<audio>` element, i.e. the pre-transfer track. That setting exists to stop us registering **action
handlers** (which is what fights globalShortcut); metadata and playbackState are display-only. The
gate now sits only on the handler registration and the callers that must respect it.

### 12. Clearing the queue never reached the hub
`publishQueue` bailed on `if (!items.length) return;` *after* updating its dedupe signature, so a
cleared queue was purely local — open Navic and the whole list is back, served from a session
Feishin thought it had thrown away. It now publishes an empty `act:setQueue` (which the hub already
handles: `s.saved_queue_id = None`), gated on `sawNonEmptyQueue` so the momentarily-empty queue
during startup hydration can't wipe a session another device is playing.

### 13. Every relaunch minted another identical history card
Session identity lives in a ref, so it dies with the renderer. Launching Feishin republished the
restored queue under a fresh UUID → a new record for music already in the history. Four launches,
four identical rows. New `findMatchingSavedQueueId(ids)` (80 % membership overlap, newest wins) is
consulted before minting, in both the hub publish path and the offline capture path. It also means
replaying something already in the history refreshes that card instead of cloning it.

### 14. "Clear all" history was local-only
`saved-queues-route.tsx` called `clearAll()` and stopped there. The hub kept every record and
rebroadcast the lot on the next connect — so the whole list came back. It now sends
`deleteSavedQueue` per id (the hub tombstones them, and `_merge_saved_queues` refuses to re-add a
tombstoned row from any client's offline copy).

### 15. Deleting the record you're listening to made the live queue disappear
Once the hub tombstones a record, the client's session id points at nothing, and the publish
dedupe means nothing re-mints until the queue membership changes — so what you were playing had no
card until you started a different queue. `restartQueueSession()` (new, in `saved-queue-source.ts`)
drops the id, re-infers the name from the queue itself and republishes immediately; both delete
surfaces and "Clear all" call it.

### 16. The expanded player was still local-only
The playerbar art was session-aware but the full-screen player wasn't, so expanding froze the cover
on the pre-transfer track. `full-screen-player.tsx` (background + container tint) and
`full-screen-player-image.tsx` now use `useRemoteAwarePlayerSong` / the new
`useRemoteAwareNextSong`. The visualizer is deliberately left local — it reads this machine's audio
engine, which has nothing to show during remote playback.

## Fourth round — first dev run (2026-08-02)

### 17. Launching Feishin started playing on its own for a few seconds
Adopting the hub's session at startup calls the store's `setQueue`, which unconditionally sets
`status = PLAYING`; `use-hub` pauses immediately afterwards, so the store settles on PAUSED. But
`setQueue` also emits `QUEUE_RESTORED`, and the mpv engine's handler (`replaceMpvQueue`) **awaits**
the stream URLs and then told mpv `pause: false` — unconditionally. That resolved *after* our pause
and started audio under a playerbar that read "paused". `replaceMpvQueue` now takes
`honourPausedState`, passed only from the `onQueueRestored` handler, and reads the store status
after the awaits — right before handing the queue over. Play/next/prev are untouched: they genuinely
intend to start audio.

### 18. The expanded sidebar cover was still local-only
Round 3 fixed the playerbar and round 3's item 16 the full-screen view; `SidebarImage` — the large
art at the bottom of the expanded sidebar — still read `usePlayerSong`, so it stayed on the last
locally-played track. Now `useRemoteAwarePlayerSong`.

*Still unverified:* whether the media key now reaches the remote device. If it does nothing at all,
the next suspect is that `remoteAct` isn't firing — not the media-session/SMTC theory, which
round 3 makes moot on this configuration.

---

## To verify (live walk-through — not yet done)
1. Hub connected, no other device active: let a track finish → the next starts by itself. Repeat
   with Navic connected-but-idle, and after force-stopping Navic mid-song (the hub log should show
   `ACT setQueue` from Feishin, then `report` frames).
2. Restart Feishin → click a Continue Listening card (including one Navic created) → it plays from
   the stored position. With Navic active, the click restores *there*.
3. Play an album, a playlist and a radio/Mood Flow mix → three cards named after their origin, art
   fixed, neither changing as tracks advance.
4. With a queue playing: reorder, remove, play-next, toggle shuffle → **one** history entry
   throughout, on both clients. Then play a new album → exactly one new entry.
5. `...` → Preview queue on cards of each origin; Resume from the modal plays, Close does nothing.
6. Restart the hub container → the same record refreshes on reconnect; no duplicate.
7. Cast to a Chromecast → the bridge log shows **one** `loading …` per track change, not a loop.
8. While casting: the title bar names the track playing on the cast device and doesn't say "paused";
   the Windows now-playing overlay shows that track's art; the media hotkey **pauses** it.
9. Cast + Auto DJ top-up mid-album → still one Continue Listening entry.
10. While remote (cast or Navic): the media key pauses/resumes the **remote** device, and the
    Windows now-playing overlay shows the remote track's title and art.
11. Clear the queue in Feishin → open Navic: the session is empty there too.
12. Close and reopen Feishin (queue restores) and start casting → **no** new Continue Listening
    card appears for that queue. Repeat twice; the count must not grow.
13. Saved Queues → "Clear all" while something is playing: the list empties, stays empty across a
    reconnect and on Navic, and **one** card for the still-playing queue reappears within a second.
14. Expand the player while casting → cover and background follow the remote track.
15. Launch Feishin with a session on the hub → **no** audio starts; the bar stays paused. Then press
    play → it plays normally (the pause-on-restore must not stick).
16. Expand the sidebar while casting → its large cover follows the remote track.

## Build/verify commands
- hub: `cd hub && python tools/test_edits.py`
- feishin: `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false` (and `tsconfig.node.json`)
