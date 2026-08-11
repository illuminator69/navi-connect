# Navic cast: surviving a locked phone

Follows `SESSION-2026-08-09-navic-native-cast.md`, which built the native Chromecast bridge. Two
rounds of fixes on top of it. The first (uninvited connection on launch, plus the
`HANDOFF-navic-lbbot-cast-parity.md` sweep) is written up in that file as §4b and §6b. This one is
the second: casting stopped surviving the phone being locked.

## The report

Cast from Navic, lock the phone, wait a few minutes. Four symptoms:

1. Navic loses the session. Reopening shows nothing playing while the speaker is audibly still going.
2. Re-connecting shows the true position for about a second, then snaps back to where the phone last
   saw it.
3. Feishin's pause does nothing.
4. Feishin's play starts a different song from the one it is displaying — correct cover, correct
   title, wrong audio.

One trigger, then a chain in which each failure turned a recoverable state into a worse one.

## The trigger: mDNS silence read as absence

`CastDiscovery` retracts a speaker on `onServiceLost`, and `CastBridgeManager.reconcile` destroyed
the bridge for any speaker missing from discovery. But multicast is the first thing a phone stops
listening to when the screen goes off, so "stopped announcing" and "unplugged" are indistinguishable
to `NsdManager` — and only one of them means the speaker is gone.

Destroying the bridge closed its hub socket. The hub, seeing its active receiver disconnect, ran
`_disconnect`: `is_playing = False`, `active_device_id = None`. Nothing had happened to the speaker;
the phone had simply stopped hearing it.

**Fix** (`CastBridgeManager.kt`): silence now has to be corroborated. `retainMissingBridges` gives a
missing-but-bridged speaker a 90 s grace period, then requires a plain TCP connect to port 8009 at
its last known address — `CastDeviceBridge.speakerReachable()`, no TLS, no cast traffic, nothing the
user can perceive. Only a speaker that has gone quiet *and* cannot be reached loses its bridge. A
`lastSeen` map keeps its name and address so it stays in the picker meanwhile.

`reconcile` also runs on a 30 s tick now. Every deadline in that class — this grace, the 5-minute
stand-down after a `4003` — expires with nothing to announce it, and driven purely by the device
flows they would only be noticed the next time something else changed, which in a quiet house is
never. Reconcile can now suspend (the probe), so it is serialised behind its own mutex.

## Then recovery could not fire

§4b had added `isPlaying` to the adoption gate, to stop every launch reaching out to the speaker.
This case proves it was the wrong discriminator: the hub clears the active slot and marks the
session stopped in the same breath, so the flag reads "stopped" precisely when a speaker has been
orphaned mid-song. Adoption was impossible in the one situation it exists for.

**Fix** (`CastDeviceBridge.maybeAdopt`): the gate is back to "orphaned queue". What keeps it from
touching speakers that are none of our business is no longer a guess about hub state but the probe
itself — join-only since §4b, so a speaker playing over Bluetooth answers "nothing of yours here"
and is left alone.

That alone would probe on every launch, which is what §4b was complaining about. `couldStillBePlaying`
decides whether it is worth asking at all: the hub records *when* the session was orphaned
(`updatedAt`) and the queue carries durations, so a queue with twelve minutes left that was orphaned
two hours ago is definitively over and nothing is contacted. `repeat: all` is unbounded and gets a
6 h ceiling instead. Every uncertainty — no timestamp, no durations, a clock skewed against the
hub's — resolves towards probing: being wrong costs one LAN round-trip, being wrong the other way
abandons a speaker that is still playing.

Adoption also gets a fresh attempt whenever another device takes and then loses the session, rather
than spending its one shot per connection on the first frame after connecting.

## Then reconnecting rewound the speaker

With the session orphaned, transferring to it again sent `do:load` at the hub's cursor — frozen
since the bridge died. Joining the running receiver reported the true position first (symptom 2's
correct second), then the LOAD landed and seeked the speaker backwards.

**Fix** (`CastDeviceBridge.loadCurrent`): before loading, ask the joined session what it is doing. A
speaker already *playing* our exact `contentId` is the authority on its own position — nothing else
can be — so we adopt that position, report it, and start the ticker instead of reloading. Gated on
`isPlaying` specifically: a paused leftover session at the same `contentId` is a stale cursor of
exactly the kind the hub's position is meant to correct.

## Feishin's half was downstream

With no active device the hub answers `pause` with `no_active_device` and nothing reaches the
speaker (symptom 3). A subsequent play promotes Feishin itself to active and starts local audio,
while its display shows the queue `adoptIfNoLiveReceiver` had adopted from the hub (symptom 4).

**Nothing changed in Feishin.** No defect was confirmed: `resolveHubTracks` is strictly 1:1, so an
unresolvable track cannot shift the index, and the remote-aware selectors are all gated on
`useHubIsRemoteActive`. Once the bridge holds the active slot again this path stops being exercised.
If the display/audio divergence recurs after that, it is a real store/engine desync and wants a
capture rather than a guess.

## Not addressed

Whether the app process survives the lock at all. If Android is freezing or killing it, the bridge
dies regardless of mDNS — and worse, the queue stops advancing, because the bridge is what loads the
next track on `IDLE/FINISHED`. `RemoteSessionPlayer` reports `STATE_READY` + `playWhenReady` while
casting, so media3 *should* hold a foreground notification and keep the process alive; whether it
does on this device cannot be settled from the source. The tell is cheap: with the phone locked
mid-queue, does the speaker advance to the next track? If it doesn't, that is the next fix, and a
different one.

## Files

- `composeApp/src/androidMain/.../cast/CastBridgeManager.kt` — `retainMissingBridges`, `lastSeen` /
  `missingSince`, `reconcileMutex`, 30 s reconcile tick, `MISSING_GRACE_MS`, `RECONCILE_TICK_MS`.
- `composeApp/src/androidMain/.../cast/CastDeviceBridge.kt` — adoption gate, `couldStillBePlaying`,
  re-adoption after another device drops, the live-session check in `loadCurrent`,
  `speakerReachable()`, `BridgeTrack.durationMs`, `ADOPTION_MAX_AGE_MS` / `ADOPTION_SLACK_MS` /
  `REACHABILITY_TIMEOUT_MS`.
- `SESSION-2026-08-09-navic-native-cast.md` — §4b corrected, §4c added, §8 state updated.

No `README.md` change: the architecture it describes (either client bridges, one owner per speaker)
is unaffected — this is all recovery behaviour behind it.

## Trade-offs

- A speaker genuinely powered off mid-session keeps its bridge for up to 90 s plus one failed TCP
  probe before the hub hears about it. A phantom receiver for a minute and a half is far cheaper
  than dropping a live one, which is what the old behaviour did every time the screen went off.
- Adoption remains strictly join-only (§4b), so a receiver torn down while our session was *paused*
  is still not re-joined on restart. There is no way to tell that from someone else's speaker
  without launching the receiver, and launching is the harmful act.

## State

`:androidApp:assembleRelease` green (3m 5s). APK 12,462,702 bytes; mapping archived by `pg_map_id`.

Untested, and the whole point of the work: lock the phone mid-cast, wait past the old failure
window, reopen. The speaker should never have left the picker, the session should still be live, and
nothing should rewind. Watch whether the queue advances while locked at the same time — that answers
the open process-lifetime question.

Everything since `f4b1327` remains uncommitted, including the entire `domain/manager/cast/` package.
