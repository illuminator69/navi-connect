# Session summary — 2026-08-09: Navic native Chromecast, plus reliability fixes

Navic can now cast on its own, with no Play Services and no Cast SDK. It discovers Chromecasts
over mDNS, speaks castv2 directly, and registers each speaker with the hub as a virtual receiver —
the same model Feishin already used, so a speaker looks identical in both clients' pickers whether
or not Feishin is running.

Also in here: two crashes, a permission bug, and a rewrite of the logs screen without which the
cast bug could not have been found.

---

## 1. Network handover (committed as `f4b1327`)

Losing Wi-Fi mid-song stopped playback until the network came back, then re-requested the
transcoded stream and stalled again.

- **The re-resolution collector was rewriting the *currently playing* item's URI.** Swapping a
  remote URI under a playing item restarts buffering from nothing. It now defers a remote→remote
  or local→remote swap to the next track (`currentItemReresolvePending`, applied in
  `onMediaItemTransition`); only a swap to a `file:` URI happens in place, with the position
  preserved.
- **Nothing was banked to ride out a handover.** The limit was `targetBufferBytes`, not
  `maxBufferMs` — raised to 48 MB with a 600 s max buffer, so a song finishes downloading in the
  quality it started in and the switch happens at the track boundary.
- **First `onPlayerError` handling in the app.** Network errors set `retryOnReconnect` and are
  retried off a *connectivity change*, never immediately — an unconditional `prepare()` there
  spins error→prepare→error.

## 2. Saved-queue persistence (committed as `f4b1327`)

- `.debounce(1.seconds)` on a flow that emits every 200 ms means the persist only ever ran when
  the user paused. Now `.sample(1.seconds)`.
- Switching queues left the outgoing one at track 1: the collector only wrote the *incoming*
  `savedQueueId`. It now tracks the previous state and flushes the outgoing snapshot first.
- `upsert` wrote a null cover forever. Falls back to the first queue track, then the current song.

## 3. Native Chromecast support (new, uncommitted)

Five new files in `composeApp/src/androidMain/.../domain/manager/cast/`, no new dependencies:

| File | Role |
|---|---|
| `CastProtocol.kt` | castv2 framing: 4-byte BE length + a hand-encoded `CastMessage` protobuf (7 string fields, ~80 lines — cheaper than a protobuf runtime and its R8 rules). |
| `CastPayloads.kt` | JSON models. `MediaStatus.positionMs` returns null for a literal 0, so "ignore the transient 0 while rebuffering" can't be forgotten at a call site. |
| `CastChannel.kt` | TLS to :8009 with a trust-all manager (Chromecast certs are self-signed), heartbeat, `requestId` correlation, `launchOrJoin()`. |
| `CastDiscovery.kt` | `NsdManager` browse of `_googlecast._tcp`, serialized resolves, re-query cadence. |
| `CastDeviceBridge.kt` | Port of Feishin's class. Its own Ktor WebSocket to the hub as `cast-<id>` / `chromecast` / `["receiver"]`. |
| `CastBridgeManager.kt` | Discovery ↔ hub reconciliation and ownership arbitration. |

**Why not the Cast SDK.** MediaRouter returned zero routes on the target device and the cause was
never found. R8 was investigated and **ruled out** — `mapping.txt` keeps `CastOptionsProvider`,
its `<init>()`, `getCastOptions` and `getAdditionalSessionProviders`; `usage.txt` lists only
`$stable` and `<clinit>` as removed. Rather than keep guessing, the dependency was removed.

**Arbitration.** Both clients see the same speaker and would register the same `cast-<id>`; the hub
kicks the older socket with `4003`, so the naive version flaps forever. Three defences: stand down
if `cast-<id>` is already online, a 0–3 s random pre-claim delay with a re-check, and a 5-minute
lockout after being superseded.

**Safeguards ported** from `SESSION-2026-08-08-v1.15.1-merge.md`: adoption on an *orphaned* session
(not just "still ours" — the hub has already cleared the slot by then, which is why that path never
fired in Feishin), `contentId` proof before claiming, `claimActive()` via `setQueue` (the hub
discards `report` from a non-active device), the `releasing` freeze, `castSessionAlive()` probed
outside the ticker, socket-close re-join guarded by `tearingDown`, host refresh on DHCP move, and
`isPlaying: false` when a load gives up.

**One Feishin bug did not need porting.** Navic is structurally immune to publish-before-`welcome`:
`_connected.value = true` is set *inside* the `welcome` handler after `_myDeviceId`
(`HubManager.kt:858-866`), and `publishQueueIfOurs` returns early on `!_connected`.

**Deliberately not built:** foreground-service retention while this device owns an active cast
session. Holding a `mediaPlayback` foreground service while nothing plays locally is policy-grey
and untestable here. Consequence: if Android kills Navic while bridging, the speaker keeps playing
and control is lost until Navic restarts, at which point adoption re-joins it.

**Play Services Cast removed entirely:** `AndroidCastManager`, `CastOptionsProvider`, the manifest
`OPTIONS_PROVIDER_CLASS_NAME` meta-data, `CastPlayer`/`SafeMediaItemConverter`/`switchSessionPlayer`
in `PlaybackService`, the `CastManager` interface, and `androidx.media3:media3-cast`.
`CastBridgeStatus.kt` is what the picker reads now.

## 4. The bug that mattered

Casting registered with the hub, transfers reported "playing", and no audio ever came out. Four
rounds of instrumentation to find it, and the first two hypotheses were wrong:

1. **Wrong:** the Play Services Cast framework was raising the repeated "choose a device to
   connect" dialog. Removing it was still right (dead code, unnecessary dependency), but it was
   not the cause.
2. **Actual dialog cause:** `ACCESS_LOCAL_NETWORK` is declared and `targetSdk` is 37, but the
   runtime request only ever fired from the *login button's* onClick. An account signed in before
   the app made direct LAN connections never gets asked, so the platform falls back to prompting
   per connection — a chooser that grants nothing, on every launch. Now requested from `App()` via
   `LaunchedEffect`, using `LocalActivity` rather than casting `view.context` (a
   `ContextThemeWrapper` in Compose — at startup that `!!` would be a launch crash).
3. **Wrong:** the unbounded TLS handshake. Real hardening (an 8 s `soTimeout` across
   `startHandshake()` only, cleared after so the reader keeps blocking reads), but it could not
   have fired — the hang was past the handshake.
4. **Actual cause:**

   ```kotlin
   readerJob = scope.launch(start = CoroutineStart.UNDISPATCHED) { readLoop(s) }
   ```

   `UNDISPATCHED` runs the body inline on the calling thread until a real suspension.
   `readLoop` opens with `withContext(Dispatchers.IO)` — but `connect()` is *already* on
   `Dispatchers.IO`, so that does not suspend, and it fell straight into a blocking
   `input.readFrame()`. `connect()` never returned: `sendVirtualConnect`, the heartbeat and the
   `"connected to $host"` log never ran, and `castMutex` was held forever so every later command
   queued behind it in silence. Fixed by letting the reader start normally.

Also added because this hid for so long: a 25 s ceiling on building a cast session
(`SESSION_SETUP_TIMEOUT`), logging of every inbound `do <cmd>`, and closing three silent exits —
`tracks.getOrNull(index)` returning null, `launchOrJoin()` returning null, and `ensureCast()`
giving up.

## 4b. Casting on launch, uninvited

Reported after the above landed: every app start seized the speaker. A Bluetooth stream playing on
it stopped dead, nothing replaced it, and Navic carried on playing locally — so the connection was
pure cost.

Two causes, both in the adoption path:

1. **The gate was `activeDeviceId == null && queue.isNotEmpty()`.** That is not "a speaker is still
   playing our session", it is the ordinary resting state of the system: the hub keeps the last
   queue indefinitely and clears `activeDeviceId` the moment the active device drops. So adoption
   fired on essentially every launch. It was given a second condition — the hub's session must say
   `isPlaying` — which turned out to be the wrong discriminator and was replaced in §4c. Cause 2 is
   the one that did the damage.
2. **Adoption probed with `launchOrJoin()`.** LAUNCH starts the Default Media Receiver, which
   *takes the speaker's audio output away from whoever holds it* — so the act of asking "is
   anything of ours playing here?" guaranteed the answer was no, and killed the Bluetooth session
   doing it. Adoption is now join-only via a new `CastChannel.joinRunning()`: if the receiver app
   isn't already up, there is by definition nothing of ours to adopt, and the speaker is left
   untouched. `ensureCast(allowLaunch = false)` also skips the re-query retry, since a speculative
   probe has no business chasing a moved address.

A deliberate transfer still launches the receiver, exactly as before — that path goes through
`loadCurrent`, which passes `allowLaunch = true`.

Also tidied: an adoption that finds someone else's session now tears its channel down instead of
holding a virtual connection into a session it has already decided isn't ours.

## 4c. Locking the phone lost the speaker

Reported next, and the same story from four angles: cast something from Navic, lock the phone, and a
few minutes later the session is gone. Reopening the app shows nothing playing while the speaker is
audibly still going; re-connecting shows the true position for about a second and then snaps back to
where the phone last saw it; and Feishin's pause does nothing, while its play starts a different
song from the one on screen.

One trigger, then a chain of failures that each turned a recoverable state into a worse one.

**The trigger: mDNS is not evidence of absence.** `CastDiscovery` removes a speaker on
`onServiceLost`, and `CastBridgeManager.reconcile` destroyed the bridge for any speaker missing from
discovery. But multicast is the first thing a phone stops listening to when the screen goes off, so
"stopped announcing" and "unplugged" are the same event to NsdManager — and only one of them means
the speaker is gone. Tearing the bridge down closed its hub socket, and the hub, seeing its active
receiver disconnect, cleared the active slot and marked the session stopped. Nothing was wrong with
the speaker; the phone simply stopped hearing it.

The bridge now survives a silence: `retainMissingBridges` gives it a 90 s grace period and then
requires corroboration — a plain TCP connect to port 8009 at the last known address, which answers
the only question that matters. Only a speaker that has gone quiet *and* cannot be reached loses its
bridge. Reconcile also runs on a 30 s tick now, because every deadline in that class — this grace,
the 5-minute stand-down after a 4003 — expires with nothing to announce it, and driven purely by the
device flows they'd be noticed the next time something else changed, which in a quiet house is
never.

**Then recovery couldn't fire.** §4b's `isPlaying` gate was the wrong discriminator, and this is the
case that proves it: `hub.py:_disconnect` sets `is_playing = False` and clears `active_device_id` in
the same breath, so the flag reads "stopped" precisely when a speaker has been orphaned mid-song.
Adoption was therefore impossible in the one situation it was written for. The gate is back to
"orphaned queue", and what keeps it from touching speakers that are none of our business is no
longer a guess about hub state but the probe itself — join-only, per §4b, so a speaker on Bluetooth
answers "nothing of yours here" and is left alone.

That alone would probe on every launch, which is what §4b was complaining about, so
`couldStillBePlaying` decides whether it is even worth asking: the hub records *when* the session
was orphaned (`updatedAt`) and the queue carries durations, and a queue with twelve minutes left
that was orphaned two hours ago is definitively over. `repeat: all` is unbounded and gets a 6 h
ceiling instead. Every uncertainty — no timestamp, no durations, a clock skewed against the hub's —
resolves towards probing: being wrong costs one LAN round-trip, being wrong the other way abandons a
speaker that is still playing. Adoption also gets a fresh attempt whenever another device takes and
then loses the session, rather than spending its one shot on the first frame after connecting.

**Then reconnecting rewound the speaker.** With the session orphaned, transferring to it again sent
`do:load` at the hub's cursor — which stopped advancing the moment the bridge died. Joining the
running receiver reported the true position first (the correct timestamp, for about a second), and
then the LOAD landed and seeked the speaker back. `loadCurrent` now checks before loading: a speaker
already *playing* our exact `contentId` is the authority on its own position, so we report it and
start the ticker instead of reloading. Gated on `isPlaying` specifically — a paused leftover session
at the same contentId is a stale cursor of exactly the kind the hub's position is meant to correct.

**Feishin's half was downstream.** With no active device the hub answers `pause` with
`no_active_device` and nothing reaches the speaker, and a subsequent play promotes Feishin itself to
active and starts local audio. Its display was right because `adoptIfNoLiveReceiver` had adopted the
hub's queue; the audio was not. No Feishin-side defect was confirmed — `resolveHubTracks` is
strictly 1:1 and the remote-aware selectors are properly gated — so nothing was changed there. If it
recurs once the speaker holds the active slot again, that's a real store/engine divergence worth
chasing with a capture.

**Not addressed: whether the app process survives the lock at all.** If Android is freezing or
killing it, the bridge dies regardless of mDNS — and worse, the queue stops advancing, since the
bridge is what loads the next track on `IDLE/FINISHED`. `RemoteSessionPlayer` reports
`STATE_READY` + `playWhenReady` while casting, so media3 *should* hold a foreground notification and
keep the process alive; whether it does on this device is the one thing that can't be settled from
the source. The tell: with the phone locked mid-queue, does the speaker advance to the next track?

## 5. Two crashes

- **`NoSuchElementException: Key <albumId> is missing in the map`.** `AlbumDao.getAlbumsByQuery`
  was the only relation-returning query without `@Transaction`. `AlbumWithSongs` makes Room walk
  the cursor twice — collect ids, fetch songs, then re-read and `map.getValue(albumId)` — so a
  concurrent sync inserting an album between the passes produces a row the first pass never saw.
  Generated code confirms it: every other such query is `performSuspending(__db, true, true)`;
  this one was `(true, false)`.
- **Crash in `ThreadedRenderer.updateViewTreeDisplayList` when opening Logs.** `LogManager`
  appended to a `mutableStateListOf` from `Dispatchers.IO` — Compose state mutated off the main
  thread, racing the draw phase. The reader now feeds a bounded channel and a main-thread
  coroutine drains it every 100 ms.

## 6. Logs screen

Unusable before: every new line yanked the view back to the bottom, copying worked one line at a
time, and lines lived on a horizontal scroller.

- Follows the tail only while actually at the tail; a top-bar chevron returns to live.
- `SelectionContainer` for multi-line selection, with the severity chip in `DisableSelection` so a
  selection yields pasteable text; plus a copy-visible-lines button.
- A filter field — the thing that made the cast bug findable.
- Lines wrap.
- `DbRepository`'s per-album progress callback logged a raw float and a `StringResource.toString()`
  tens of times a second, burying every other tag. Removed; the `- X Synced` lines remain.

## 6b. HANDOFF-navic-lbbot-cast-parity.md, worked through

Every item in that handoff except the two it explicitly defers.

**§1 — the resolved-edition contract (P0).** `albumSources` and `download` now send the pressing
the sheet already resolved (`release_mbid` / `artist` / `album`|`title` / `total`), via a new
`LbResolvedEdition`. This is what makes the edition picker mean anything — lb-bot was choosing
"official, earliest" for itself and telling nobody — and it takes the download out of
`mbz_resolve_album`'s five-minute failure cooldown, where one MusicBrainz 503 answered every retry
with an instant 400. `rgid` still rides along in both, or placement can't flip the index row and a
filled album double-lists forever. Track count prefers the loaded tracklist's size, falls back to
the variant's, and is omitted rather than sent as a zero.

> Needs the hub container **and** lb-bot restarted, or the hub silently drops the new fields and
> you get the old behaviour with no error.

**§2.1 — a 502 is "busy".** New `LbError.Busy` for 502/504, its own variant rather than a string so
callers can decide whether it is worth surfacing. It no longer shows lb-bot's "unreachable" through
to the user, which was actively misleading: the service is fine, it just handles one request at a
time while searching.

**§2.2 — absorbing transient poll failures.** Navic's shape differs from Feishin's here, and the
fix follows the shape rather than the letter: the gap *poll* lives in `LbBotManager` and already
only writes on success, so the sheet's exposure was its opening read. That now tolerates
`TRANSIENT_FAILURE_LIMIT = 2` consecutive `Busy` results before saying anything. Anything the user
pressed still reports its first failure immediately — a silent button is the bug the error model
exists to prevent.

**§2.3 — the double-fetch window.** A `committed` flag held from the fetch POST until lb-bot's
status catches up, released on a 30 s timer as well, so a fetch that quietly didn't take gives the
button back rather than dying disabled. Not keyed on track state `picked`, per the handoff's
warning — that is precisely the state in which the user must be able to choose a source.

**§2.4 — invisible progress.** Gated on "anything is actually in flight" (any track `queued` or
`downloading`, or the local commit) rather than `status == "downloading"`, which lb-bot sets well
after the first track is queued.

**§2.5 — no way to rescan a discography.** The action is now always offered when an MBID exists,
labelled *Find missing albums* when the index is empty and *Rescan discography* otherwise. The poll
watches `scanned_at` rather than `indexed` — for an already-indexed artist `indexed` is true on the
first tick, so the spinner used to clear while the walk was still running, in exactly the case the
button exists for. `scannedAt` is typed `Double`: lb-bot writes a `time.time()` float and a `Long`
would throw on the decimal point, taking the whole discography payload down with it.

**§2.6 — the badge** now reads `3 missing` rather than `9/12`.

**§3.1 — nothing scrobbled a cast session.** New `CastScrobbler` (commonMain, eager singleton). It
samples the hub's ~1 Hz progress mirror and accumulates *listening time*, never playhead position:
a step that is backwards, zero, or larger than 5 s buys no credit, so seeking to the end scrobbles
nothing. Keyed on queue index *and* track id, or repeat-one would scrobble once and never again.
Sends now-playing on track change so the server shows the speaker's track as live. The gate is
`activeDeviceId is a speaker whose state == BRIDGING` — one client and no other, which is what
stops a second open controller double-counting.

**§3.3 — elapsed time while casting: verified, no change.** `NowPlayingDurationsRow` derives the
label from `duration * playerState.progress`, and `progress` is the remote-aware fraction
`HubManager.pushRemoteProgress` writes. Navic never had Feishin's frozen-clock bug.

**Found while doing §3.1, fixed in both scrobblers:** `minDurationToScrobble` is stored and
displayed in *seconds* (the settings screen renders `"30s"`) but was compared against a duration in
*milliseconds*. The minimum-duration rule has therefore never excluded anything — it only rejected
tracks shorter than 30 ms. Now `* 1000` in `ScrobbleManager` and `CastScrobbler` alike.

Deliberately not done, per the handoff's own §4: Feishin's release-type filter, anything touching
lb-bot's match workspace, and raising the hub's `PROXY_TIMEOUT`.

## 7. Docs

`PROTOCOL.md` §12.2 rewritten from "Phase 2, Navic uses the Cast SDK" to what exists, including the
four-step ownership rule and the `4003` circuit breaker. `README.md` §Chromecast rewritten (either
client bridges, neither uses a Cast SDK, one-owner rule). No further README change is needed — the
architecture it now describes is accurate.

## 8. State

Cast confirmed working live by the user. `:androidApp:assembleRelease` green throughout.

**Everything since `f4b1327` is uncommitted**, including the whole `domain/manager/cast/` package,
the Play Services removal, both crash fixes, and the logs screen. Also still uncommitted from
earlier work: `hub/hub.py`, `hub/tools/test_saved_queues.py`, `feishin/.../audiomuse/index.ts`,
`AUDIT-NAVIC-FEATURES-2026-08-07.md`. The hub's `deleteSavedQueues` act is the counterpart to
already-committed Navic code and should land with it.

Untested: iOS (`NoopCastBridgeStatus` only), arbitration against a live Feishin, adoption after
force-stop, paused idle-out, and DHCP address change.

Untested from §4c, and the whole point of that work: lock the phone mid-cast, wait past the old
failure window, and reopen — the speaker should never have left the picker, the session should still
be live, and nothing should rewind. Worth watching the queue advance while locked at the same time,
since that answers the open process-lifetime question.

Untested from the handoff work, and needing the **hub and lb-bot restarted first** (§1 does nothing
until then, silently): the edition override end to end, the rescan spinner surviving until
`scanned_at` moves, and a cast scrobble actually moving a play count — including the negative case,
where Feishin is also running, Navic is `BRIDGED_ELSEWHERE`, and Navic must **not** scrobble.

Adoption is now strictly join-only, which narrows it: if the receiver app has been torn down while
our session was paused, a restart of Navic no longer re-joins anything. That is the intended
trade — there is no way to tell "our paused session" from "someone else's speaker" without
launching the receiver, and launching it is the thing that was causing harm.

The mDNS grace in §4c has its own trade: a speaker genuinely powered off mid-session keeps its
bridge for up to 90 s plus one failed TCP probe before the hub hears about it. A phantom receiver
for a minute and a half is a far cheaper mistake than dropping a live one, which is what the
previous behaviour did every time the screen went off.

**Retracing note:** R8 overwrites `mapping.txt` on every build, so a crash stack is only readable
against the exact build it came from. One stack was lost to this. Mappings are now archived by
`pg_map_id` when built.

---

# Second summary — open items

Two things observed this session, neither investigated or fixed.

## A. The home-screen widget does not update in real time

The mini-player / turntable widgets can show the *previous* song while a different one is already
playing.

Cause identified (not fixed): the `${applicationId}.NOW_PLAYING_UPDATED` broadcast that
`MiniPlayerReceiver` / `TurnTableReceiver` listen for is sent from exactly one place —
`onIsPlayingChanged` in `MediaPlayer.android.kt:405`. A track ending and the next one starting does
not change `isPlaying`, so no broadcast is sent and the widget keeps rendering the previous song's
title, artist and art until playback is next paused or resumed.

Fix is to also broadcast from `onMediaItemTransition` (and anywhere else metadata changes without a
play-state change, e.g. `onMediaMetadataChanged`), reading the new item rather than
`_uiState.value.currentSong`, which may not have been updated yet at that point.

## B. The volume rocker should control the remote device

While a cast device — or any remote receiver — is active, the phone's hardware volume keys still
change the *phone's* stream volume, which affects nothing that is audible. They should drive the
active device's volume instead.

The pieces already exist: `hubManager.actSetVolume(level)` is what the picker's slider calls, and
the bridge maps it onto castv2 `SET_VOLUME`. What's missing is routing the keys there — a media
session with a remote playback volume provider (`VolumeProviderCompat` / Media3's equivalent on
`RemoteSessionPlayer`) while `hubManager.isRemoteActive` is true, falling back to local volume when
playback returns to the phone.
