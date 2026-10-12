# navi-connect — Protocol Specification (v0.1 draft)

A Spotify-Connect-style control layer for a personal Navidrome setup. A small
**headless relay** ("the hub") runs on Unraid; each music client (Feishin on
Windows, a Navic fork on Android) connects out to it and is simultaneously a
**controller** and a **receiver**. The hub never touches audio — each receiver
streams directly from Navidrome by track id. The hub only shuttles a small JSON
session (`queue + index + positionMs + isPlaying + activeDevice`) between devices
and fans out state to controllers.

> **Scope of this doc:** the wire protocol only. It defines the transport, the
> message catalog, the session model, and the transfer-with-resume flow, plus how
> each side maps onto the *existing* Feishin and Navic code. No client code is
> changed yet — this is the design to review first.

---

## 1. Design principles

1. **The active receiver is the single source of truth for live playback.** It
   owns local progression (auto-advance, gapless) and reports `positionMs /
   index / isPlaying`. The hub mirrors that truth; controllers mirror the hub.
   This matches how Spotify Connect works and avoids the hub guessing position.
2. **The hub owns the *intent* (the session): queue, order, repeat/shuffle,
   which device is active.** Controllers mutate intent; the hub translates intent
   into directives for the active receiver.
3. **Audio never flows through the hub.** Receivers resolve `/rest/stream` from
   Navidrome themselves (both clients already do). The hub moves track *ids* and
   metadata only.
4. **Single user.** One shared secret. No per-user device ownership, no ACLs.
5. **Survives restarts.** The hub persists session + device registry to disk
   (like the existing bot's `lb_bot_state.json`). Optional mirror to Navidrome
   `savePlayQueue` for interop with other Subsonic clients (§9).

---

## 2. Roles

| Role | Who | Responsibility |
|------|-----|----------------|
| **Hub** | headless container on Unraid | device registry, session state, command routing, transfer orchestration, persistence |
| **Receiver** | the *active* device | plays the queue from Navidrome, owns progression, reports progress |
| **Controller** | any device's UI (or a future bot/web UI) | mutates session intent: setQueue, play/pause, next, seek, transfer, … |

A single device is a receiver **only while it is the active device**; otherwise
it is purely a controller. Every device is always a potential controller.

---

## 3. Transport

- **WebSocket**, JSON **text** frames, one JSON object per frame.
- Every frame has a string field **`t`** (the message type). All other fields are
  type-specific (flat, no `{event,data}` nesting).
- Hub listens on a single port (proposed **`4790`**). Path `/connect`. The same
  port also answers plain HTTP on `/sonic/*` — the AudioMuse Tier-2 proxy (§14) —
  and `/lb/*`, the lb-bot proxy (§15).
- **Heartbeat:** WS ping/pong every 10 s (mirrors Feishin's existing
  `PING_TIMEOUT_MS`). A receiver missing 2 pongs is marked `online:false` and, if
  it was active, the session is auto-paused (queue/index/position retained).
- **Reconnect:** clients reconnect with exponential backoff and re-send `hello`
  with their persisted `deviceId` to reclaim their registry slot.

**Close codes.**

| Code | Meaning | Client response |
|---|---|---|
| `4001` | bad or missing token | stop; do not retry until settings change |
| `4002` | protocol violation (bad first frame, malformed JSON) | reconnect with backoff |
| `4003` | superseded — another socket registered this same `device.id` | **stand down**; for a cast bridge that means 5 minutes (§12.2) |
| `4004` | bad path (not `/connect`) | fix the URL; do not retry |

### 3.1 Connection handshake

```
Client → Hub : { "t":"hello", "token":"<shared-secret>",
                 "device": { "id":"<persisted-uuid|null>", "name":"Feishin-PC",
                             "platform":"desktop|android", "caps":["receiver","controller"],
                             "bridgedBy":"<owning-device-id|absent>" } }
Hub → Client : { "t":"welcome", "deviceId":"<assigned-uuid>",
                 "session": <Session>, "devices": [<DeviceInfo>...],
                 "savedQueues": [<SavedQueue>...],    // §8.3
                 "lb": { "available":true, "routes":["GET /lb/…"], "webUrl":"http://…:8899" },   // §15.3
                 "lbIndex": { "seq":42, "epoch":"1-…" } }              // §15.3, once heard
```

- Bad/missing `token` → hub sends `{ "t":"error", "code":"auth", ... }` and closes.
- If `device.id` is null the hub mints a UUID; the client persists it and reuses
  it on every future `hello` (so reconnects don't create duplicate devices).
- `caps` is also how a client opts into optional behaviour. `"loadAck"` means this
  receiver answers a transfer's `do:load` with a `loaded` frame (§7.1); a receiver
  that does not advertise it is never held to the timeout. `"transferAckV2"` means it
  also echoes the directive's `transferId` on `loaded` and `released`, so the hub can
  match a reply to the attempt that asked for it (§7.2).
- `bridgedBy` is set only by a **virtual** device — a receiver whose socket is held
  on behalf of hardware the hub cannot reach itself (today: a Chromecast, §12.2).
  It names the client process holding the bridge.

### 3.2 DeviceInfo

```json
{ "id":"cast-abc", "name":"📺 Living Room", "platform":"chromecast",
  "caps":["receiver","loadAck"], "online":true, "isActive":false,
  "lastSeen":1755500000000, "volume":40,
  "reachable":true, "appRunning":true, "bridgedBy":"feishin-pc" }
```

**`online` and `reachable` answer different questions, and conflating them is a
bug.** `online` means *a WebSocket claiming this id is attached right now*. For a
bridged device that is the **bridge's** socket — it says nothing about whether the
speaker is powered on, on this network, or answering. `reachable` is the bridge's
verdict on the hardware:

| `reachable` | Meaning | Picker |
|---|---|---|
| `true` | the bridge reached the device recently | available |
| `null` | unknown / not applicable — every ordinary client, and a bridge that hasn't reported yet | available |
| `false` | the bridge is connected but the device did not answer | shown, greyed, **not** selectable |

A verdict expires: if the asserting bridge stops renewing it (90 s), the hub
demotes it back to `null` rather than keep asserting a speaker is up — or down —
on the strength of a report from a bridge that may itself be wedged.

---

## 4. Data model

### 4.1 Track (minimal — receivers re-resolve audio from Navidrome by `id`)

```jsonc
{
  "id": "tr-123",            // Navidrome/Subsonic song id (authoritative)
  "serverId": "nd-main",     // which configured server (multi-server safe)
  "title": "Song",
  "artist": "Artist",
  "album": "Album",
  "durationMs": 213000,      // needed by Navic to convert ms↔normalized seek
  "coverArtId": "al-99"      // for controller UI; resolved via getCoverArt
}
```

### 4.2 Session (hub-owned intent; mirrored to all)

```jsonc
{
  "rev": 42,                 // monotonically increasing; bump on every change
  "activeDeviceId": "uuid|null",
  "queue": [ <Track>... ],
  "index": 6,                // current item in queue (pre-shuffle order)
  "order": [ ... ],          // optional: shuffled index order; null = sequential
  "positionMs": 32000,       // last reported by active receiver
  "isPlaying": true,
  "repeat": "none|all|one",
  "shuffle": false,
  "updatedAt": 1730000000000,
  "savedQueueId": "q_...|null",  // history record this queue IS (§8.3); null = untracked
  "sourceKind": "album|playlist|radio|moodFlow|journey|manual",
  "sourceName": "Album One|null" // display name of what was played
}
```

> **`volume` is intentionally NOT in the session** — like Spotify, volume is a
> per-device property of the receiver, controlled via the `volume` action but
> not part of transferable intent.

### 4.3 DeviceInfo (registry; mirrored to all)

```jsonc
{ "id":"uuid", "name":"Feishin-PC", "platform":"desktop",
  "caps":["receiver","controller","loadAck"], "online":true,
  "isActive":false, "lastSeen": 1730000000000, "volume": 100,
  // presence (`online`) and reachability are different questions — see §3.2
  "reachable": null, "appRunning": null, "bridgedBy": null }
```

Full field semantics — in particular why `online:true` on a Chromecast row does
**not** mean the speaker is on — are in **§3.2**.

---

## 5. Message catalog

Three logical channels, all over the one socket, disambiguated by `t`.

### 5.1 Controller → Hub: **actions** (`t:"act"`, field `action`)

These express user intent. The hub validates, updates the session, bumps `rev`,
issues directives to the active receiver (§5.2), then broadcasts the new session.

| `action` | Fields | Meaning |
|----------|--------|---------|
| `play` / `pause` / `playpause` | — | transport on active device |
| `next` / `previous` | — | advance/retreat queue |
| `seek` | `positionMs` | seek active device |
| `jump` | `index` | jump to a queue item |
| `setQueue` | `tracks[]`, `index?`(0), `positionMs?`(0), `play?`(true) | replace queue (e.g. user picked an album) |
| `enqueue` | `tracks[]`, `at?`("end"\|"next") | add without replacing |
| `remove` | `index` | remove a queue item |
| `clear` | — | empty the queue and stop the active device |
| `move` | `from`, `to` | reorder queue |
| `repeat` | `mode` | none/all/one |
| `shuffle` | `on` | toggle shuffle (hub computes `order`) |
| `volume` | `level`(0–100) | set active device volume |
| `transfer` | `target`, `play?`(true) | hand playback to another device (§7) |
| `favorite` | `id`, `favorite` | star/unstar (passthrough to receiver→Navidrome) |
| `rating` | `id`, `value` | 0–5 |
| `renameSavedQueue` | `id`, `name` | rename a saved-queue history record (§8.3) |
| `deleteSavedQueue` | `id` | delete a saved-queue history record (§8.3) |
| `deleteSavedQueues` | `ids[]` | delete several records in one act — one broadcast (§8.3) |
| `syncSavedQueues` | `queues[]`, `deleted[]?` | push a client's local/offline history **and its offline deletions** up (§8.3) |
| `saveMix` | `id?`, `name`, `kind`, `seedId?`, `seedName?`, `moodCharacter?`, `count?`, `coverArtId?` | create (no `id`) or update one "Mixed for You" recipe (§17) |
| `renameMix` | `id`, `name` | rename a recipe (§17) |
| `deleteMix` | `id` | delete a recipe (§17) |
| `touchMix` | `id` | record that a recipe was just played — bumps `lastPlayedAt` only (§17) |

`setQueue` additionally accepts optional `savedQueueId`, `sourceKind`
(`album|playlist|radio|moodFlow|journey|manual`), `sourceName`, `coverImageUrl`, `serverId`
— see §8.3.

If there is **no active device** when a controller sends a playback action, the
hub applies it to the session and, for `play`/`setQueue`, auto-promotes the
**sending device** to active (it then receives a `do:load`).

### 5.2 Hub → active receiver: **directives** (`t:"do"`, field `cmd`)

| `cmd` | Fields | Receiver action |
|-------|--------|-----------------|
| `load` | `tracks[]`, `index`, `positionMs`, `play` | build queue, resolve streams from Navidrome, seek, (play/pause). **The transfer/setQueue primitive.** |
| `play` / `pause` | — | transport |
| `seek` | `positionMs` | seek |
| `jump` | `index` | play queue item `index` from 0 |
| `queueChanged` | `tracks[]`, `index` | reconcile queue edits **without restarting the current track** if its id is unchanged |
| `clear` | — | empty the queue and stop. Distinct from `queueChanged` because that command carries a track list, and receivers can't tell an empty one from "nothing to apply" |
| `setVolume` | `level` | set local volume |
| `setRepeat` / `setShuffle` | `mode` / `on`, `order?` | apply mode |
| `release` | — | send one final `report` then stop audio and drop active role (used during transfer / shutdown) |

### 5.3 Receiver → Hub: **reports** (`t:"report"`)

```jsonc
{ "t":"report", "positionMs":33000, "index":6, "isPlaying":true,
  "ended":false }   // ended:true when the whole queue finished
```

- Sent **once per second while playing**, and **immediately** on any state change
  (play/pause, track change via local auto-advance, seek completion).
- The hub updates `session.positionMs/index/isPlaying` from this and rebroadcasts
  (throttled; position-only updates need not bump `rev`).

**`t:"loaded"` — transfer acknowledgement** (receivers advertising `loadAck` only):

```jsonc
{ "t":"loaded", "transferId":"t7", "ok":false, "error":"no route to host" }
```

Answers a `do:load` that arrived as part of a transfer (§7). `ok:true` means audio
is actually running (or paused at the requested spot, if `play:false`) — the receiver
must have *observed* that, not merely called its player's API; `ok:false` means it
could not start. Silence for the directive's `timeoutMs` (10 s) counts as failure.
`transferId` echoes `do:load` and is required from receivers advertising
`transferAckV2`. See §7.1-7.2.

**`t:"deviceState"` — a bridge's verdict on its hardware** (bridged devices only):

```jsonc
{ "t":"deviceState", "reachable":true, "appRunning":true }
```

Sent on change and at least every 30 s. Only meaningful from a device with
`bridgedBy` set: an ordinary client *is* its own hardware, so its socket already
proves reachability. `reachable:false` while that device holds the session makes
the hub pause and relinquish the active slot — nothing else can notice, because
the bridge's own socket stays perfectly healthy. See §3.2 and §12.2.

### 5.4 Hub → all: **broadcasts**

| `t` | Payload | When |
|-----|---------|------|
| `session` | `<Session>` | any intent change (new queue, index, repeat…) |
| `progress` | `{ positionMs, index, isPlaying }` | throttled live position from active receiver |
| `devices` | `[<DeviceInfo>...]` | device joins/leaves/goes active |
| `savedQueues` | `{ queues:[<SavedQueue>...] }` | saved-queue history changed (§8.3); also in `welcome` |
| `mixes` | `{ mixes:[<Mix>...] }` | "Mixed for You" recipes changed (§17); also in `welcome` |
| `library` | `{ event, releaseMbid?, rgid?, artist?, album?, ndArtistId?, ndAlbumIds?, row? }` | lb-bot placed an album (`albumPlaced`), Navidrome indexed it (`albumIndexed`), or an artist's discography scan ended (`artistScanned`) (§15.1). Empty fields are omitted, so an `artistScanned` frame has no `releaseMbid` |
| `fill` | `{ kind, key, ... }` — for `kind: "album"` the `album/status` view minus `files[]`, keyed by `rgid`; for `kind: "gap"` a gap summary keyed by review-group id; for `kind: "wishlist"` a landing keyed by `rgid` | lb-bot's ledger moved (state or throttled progress) (§15.1). Touches nothing in the library, so clients update one ledger row and refetch nothing |
| `index` | `{ seq, epoch }` | lb-bot's library-index head moved (§15.3). Carries no rows: clients pull `/lb/index/changes` from their own cursor. Not a library event either — nothing is refetched |
| `lb` | `{ available, routes, webUrl }` — the same object as `welcome.lb` | the hub's verdict on lb-bot's availability **changed** (§15.3). Clients apply it exactly like `welcome.lb` |
| `error` | `{ code, message }` | auth, bad target, etc. Codes: `bad_action`, `target_offline`, `target_unreachable` (§3.2), `not_a_receiver`, `load_failed` (§7.1), `no_active_device`, `unknown_saved_queue` (§8.3), `unknown_mix` / `bad_mix` (§17) |

Controllers render `session` + `progress`; receivers ignore `progress` for their
own id.

---

## 6. State ownership summary

```
 user taps "next" on phone
        │  t:act action:next
        ▼
      [HUB]  index←index+1 (respecting repeat/shuffle), rev++
        │  t:do cmd:jump index:7        ──────────────►  [ACTIVE RECEIVER = Feishin-PC]
        │                                                 loads/seeks, starts track 7
        │  t:session (rev 43) ──► all controllers          │ t:report index:7 positionMs:0
        ◄───────────────────────────────────────────────── ┘
      [HUB] mirrors report → t:progress → all controllers
```

The receiver is authoritative for *what is actually happening*; the hub is
authoritative for *what should happen*. They converge via `do`/`report`.

---

## 7. Transfer-with-resume flow (the Spotify handoff)

Goal: move playback from device A to device B and continue from the exact spot.

```
Controller → Hub : { t:act, action:transfer, target:"<B>", play:true }

1. Hub → A : { t:do, cmd:release }
   A pauses, emits a FINAL { t:report, positionMs:P, index:I, isPlaying:false },
   stops audio, drops active role, then { t:"released" }.
   (Hub uses the final report's P/I; if A is offline/unresponsive it falls back
    to the last known session.positionMs/index — so transfer works even from a
    dead device.)

2. Hub : activeDeviceId ← B ; session.positionMs ← P ; session.index ← I ; rev++

3. Hub → B : { t:do, cmd:load, tracks:<session.queue>, index:I, positionMs:P, play:true }
   B resolves stream URLs from Navidrome by id, builds its queue, SEEKS to P,
   and plays.

4. Hub → all : { t:session } (activeDeviceId=B) ; { t:devices } (isActive flags)
   B begins { t:report } at 1 Hz.
```

Because the queue is just track ids and B is already a full Subsonic client,
B reconstructs identical audio and resumes seamlessly — no media proxied through
the hub.

**Edge cases**
- *Transfer from an offline device:* step 1 times out (≈1.5 s) → hub uses last
  known position. Still resumes correctly within ~1 s of granularity.
- *Target offline:* hub replies `error{code:"target_offline"}`, no state change.
- *Target unreachable:* its bridge is connected but the hardware is not answering
  (§3.2) → `error{code:"target_unreachable"}`, no state change. This is the check
  that stops "cast to the TV in the other house" from silently committing.
- *Target is already the active device:* **no-op**. The hub does NOT re-issue
  `do:load` — `session.positionMs` trails the live position by up to a report
  interval, so reloading would visibly rewind playback. If `play` differs from the
  current state the hub sends only `do:play`/`do:pause`.
- *Target lacks the `receiver` cap:* rejected with `error{code:"not_a_receiver"}`.
- *Late `released`:* only honoured from the current active device or one with a
  handoff in flight; a straggler from a device that handed off earlier is dropped
  (it would otherwise rewind the session to that device's stale position).
- *Taking over an orphaned session:* when no device is active, an `act:play` promotes
  the sender **and hands it `do:load {tracks, index, positionMs, play:true}`** — not a
  bare `do:play`. The new device knows nothing of the session, so a bare play would
  resume its own stale local queue instead of where the dead device left off.
- *Active receiver disappears mid-playback:* hub marks it offline, sets
  `isPlaying:false`, **clears `activeDeviceId` to null**, and retains
  queue/index/position. With no live receiver, any still-open client auto-adopts the
  last-known queue locally (paused) and can resume by pressing play (which claims the
  active slot); an explicit transfer still works too. A device that was genuinely still
  playing re-claims active via its normal reporter on reconnect.

### 7.1 Confirming the handoff (`loaded`)

Step 2 commits `activeDeviceId` **before** step 3's `do:load` on purpose: the target
must know it is active before its load side effects fire, or it misroutes them. The
cost is that a load which then fails is invisible — every client shows a playing bar
over a silent device until a human presses something. A Chromecast makes that
failure both likely (the speaker is off, asleep, or on another network) and slow
(connect + LAUNCH + first-byte timeouts stack to ~20 s).

So a receiver advertising `caps:["loadAck"]` must answer every transfer `do:load`:

```
3.  Hub → B : { t:do, cmd:load, ... }
3a. B   → Hub: { t:loaded, ok:true }            // audio is actually running
    …or  { t:loaded, ok:false, error:"…" }      // could not start
```

If the answer is `ok:false`, or none arrives within **10 s**, the hub **rolls the
active slot back** to the previous device — or to `null` if that device is gone,
which is the orphaned-session signal every client already knows how to read — sets
`isPlaying:false`, and broadcasts `error{code:"load_failed"}`.

Two deliberate limits:
- The rollback is skipped if a newer transfer or takeover already moved the active
  slot. That later decision wins; this ack is stale by then.
- A receiver that does **not** advertise `loadAck` is never held to the timeout, so
  an older client's transfers are unaffected.

The wait runs off the issuing socket's read loop, as does the `release` handshake in
step 1 — the common case is the active receiver transferring away from *itself*, and
awaiting inside its own read loop means the hub cannot read the very `released`
frame it is waiting for.

### 7.2 Correlating the handoff (`transferId`, `timeoutMs`)

§7.1 correlates a reply to a **device**. That is not enough, because a transfer is a
two-phase transaction and a device outlives any one attempt at it. Three failures
follow from device-only correlation, all ending with the hub holding a state no
speaker agrees with:

- A `loaded` delayed past its own 10 s deadline satisfies the **next** transfer to
  the same device. The hub calls that load good on the strength of an answer to a
  question it stopped asking.
- **ABA.** Transfer A → X times out; B moves to Y; C moves back to X. A's expired
  rollback sees "X is active" — true again, for an unrelated reason — and undoes C.
- A duplicate `released` from an earlier handoff completes the release future armed
  for the current one, ending phase 1 early *and* rewinding the session to where that
  device stood two transfers ago.

So every handoff gets an opaque hub-minted `transferId`, carried on all four frames:

```
1.  Hub → A : { t:do, cmd:release, transferId:"t7" }
1a. A   → Hub: { t:released, transferId:"t7", index:I, positionMs:P }
3.  Hub → B : { t:do, cmd:load, transferId:"t7", timeoutMs:10000, tracks:[…], … }
3a. B   → Hub: { t:loaded, transferId:"t7", ok:true }
```

A receiver advertising `caps:["transferAckV2"]` **must** echo the id it was given;
an ack whose id does not match the phase currently pending is discarded, and the
phase runs its own course (timeout, rollback) as if nothing had arrived. A receiver
without the cap is correlated by device exactly as in §7.1 — it was never told to
echo anything, and holding it to a rule it cannot know about would fail every
transfer it wins.

Two further hub-side rules make the id sufficient rather than merely present:

- **The active slot carries a generation.** Every assignment to `activeDeviceId`
  bumps it, so a rollback can require *the exact assignment it made* to still stand,
  not just the same id. This is what actually kills ABA — and the reconnect case
  underneath it, where a dropped device takes its own orphaned session back (§7,
  "Active receiver disappears") and an id-only check would let an abandoned load's
  deadline tear that down.
- **A superseded wait is cancelled, not left running.** Arming a new load future for
  a device cancels the previous one, so two waiters never race to roll the same slot
  back.

**`timeoutMs`** is the receiver's share of the same deadline, measured from dispatch.
The hub owns `LOAD_TIMEOUT`; without being told it, a receiver picks its own — Navic's
Cast bridge allowed 25 s per connect attempt, and made two — and a load that lands
after the rollback is audio on a speaker no client believes is active. A receiver must
answer inside the budget, reserving enough of it to send the ack, and must guarantee
that an attempt abandoned on expiry cannot still start playback afterwards.

Delivery of `do:release` and `do:load` is checked rather than assumed. A send that
fails immediately ends its phase immediately, instead of the hub sitting the full
timeout waiting for an answer to a frame that never left the process.

---

## 8. Mapping onto existing client code

### 8.1 Feishin (Electron, `feishin-development`)

Feishin **already** runs a WS *server* for its web-remote
(`src/main/features/core/remote/index.ts`, port 4333) with auth + heartbeat and a
transport event set. We do **not** reuse that server (it's a per-instance remote,
single-song, no queue). Instead add a small **hub *client*** in the main process
that speaks this protocol and bridges to the renderer over the existing IPC.

| Protocol directive | Existing Feishin hook |
|--------------------|------------------------|
| `do:load` (queue+index+pos) | `player.store.ts` → **`setQueue(songs, index, position)`** — already takes all three |
| `do:play`/`pause`/`jump`/`seek` | existing IPC `renderer-player-play` / `-pause` / `-next`/`-previous`, `request-position` |
| `do:setVolume` | `request-volume` IPC / `setVolume` |
| `do:setRepeat`/`setShuffle` | `setRepeat` / `setShuffle`, `renderer-player-toggle-*` |
| `report` (progress) | renderer already pushes song/state via `update-remote-song.tsx` (`window.api.remote.updateSong`) + `update-position`; extend to also push **queue+index** |
| `favorite`/`rating` | existing `request-favorite` / `request-rating` IPC |

New work: (a) a `hub-client.ts` in `src/main/features/core/` (WS out to hub,
backoff, hello/welcome); (b) extend the renderer→main bridge to publish the whole
queue + index (today it publishes only the current song); (c) handle `do:load`
by calling `setQueue`. ~Most of the protocol's transport half already exists.

### 8.2 Navic (Kotlin Multiplatform, `composeApp`)

No remote layer yet, but the seams are clean and Ktor (WebSocket client) +
media3 are already dependencies.

| Protocol directive | Existing Navic API (`MediaPlayerViewModel`) |
|--------------------|----------------------------------------------|
| `do:load` (queue+index+pos) | **`syncPlayerWithState(PlayerUiState)`** — same path `restoreState()` uses: build a `PlayerUiState{queue, index, positionMs, isPaused}` and apply |
| `do:play`/`pause` | `resume()` / `pause()` |
| `do:jump` | `playAt(index)` |
| `do:seek` | `seek(normalized)` — **convert `positionMs / track.durationMs`** |
| `do:next`/`previous` | `next()` / `previous()` |
| `do:setRepeat`/`setShuffle` | `toggleRepeat()` / `toggleShuffle()` |
| queue edits | `addToQueue`, `removeFromQueue`, `moveQueueItem`, `clearQueue` |
| `report` (progress) | collect `uiState: StateFlow<PlayerUiState>` and emit on change + 1 Hz tick |

New work: a `HubClient` in `commonMain` (Ktor `WebSocketSession`, hello/welcome,
backoff), wired to `MediaPlayerViewModel`; persist `deviceId` next to the existing
`PlayerStateRepository` DataStore. Android needs the playback **foreground
service** to stay alive as a receiver (media3 already implies one).

> **`PlayerUiState` is `@Serializable`** — so the on-wire Track/queue shape can be
> derived from it, minimizing translation on the Navic side.

### 8.3 Saved-queue history (Continue Listening) — hub-owned, shared

The hub owns a **rolling saved-queue history** (Symfonium-style "Continue Listening"),
so every client shows the *same* list and the *same* current-vs-previous distinction.

- **Record shape** (`SavedQueue`): `{ id, serverId?, songs:[<Track>], songCount,
  currentIndex, positionMs, sourceKind, sourceName?, coverImageUrl?, name?, shuffle,
  repeat, createdAt, updatedAt }`. Capped at **20**, evict oldest by `updatedAt`,
  persisted in the hub state.
- **Identity on the session:** the live `Session` carries `savedQueueId` + `sourceKind` +
  `sourceName`. The record whose `id == session.savedQueueId` is the **current** queue
  (clients highlight it "Now Playing", first). A `setQueue` adopts the client's
  `savedQueueId` (or the hub mints one) and **upserts** a record from the live queue; a
  re-publish of the same id just refreshes it (no fork). `enqueue`/`remove`/`move` grow /
  edit that same record — this is what makes "the current queue updates dynamically".
- **Broadcast:** on any change the hub emits `savedQueues { queues:[...] }` (whole capped
  list); it's also embedded in `welcome`.
- **Offline reconciliation:** a client keeps its own local store as an offline cache. On
  (re)connect it sends `act:syncSavedQueues { queues, deleted? }` with its local rows and
  the ids it deleted while offline. **`deleted` is applied before `queues`** — a client that
  deleted a row offline still holds it in `queues` (it had no way to re-fetch), so merging
  first would re-add what it just removed. The hub then **union-merges by id (newest
  `updatedAt` wins)**, caps, and rebroadcasts, so queues captured while a client was offline
  are preserved and everyone converges, newest-first.
  The merge is **field-level and non-destructive**: a whitelisted set of fields is taken,
  and a client copy that lacks `name`/`sourceName`/`coverImageUrl` never blanks the hub's.
  The record backing the live session is reconciled **metadata-only** — its songs and cursor
  are never overwritten by an incoming copy, though a genuinely newer `name` is accepted (an
  offline rename of the queue you are listening to is otherwise unable to sync back). If the
  hub has *lost* the live record but still names it, the record is rebuilt from the live
  session, never from the client copy.
- **Limits.** Client-supplied records are coerced on the way in — they are persisted and
  rebroadcast, so a bad value would outlive the frame that carried it. `syncSavedQueues`
  accepts at most 20 records per frame (accepting more let one reconnecting device evict a
  history another device was looking at); each record is capped at 1000 tracks; each track is
  whitelisted to the §4.1 fields plus `imageUrl`, `streamUrl` and `mime` (the hub's
  `SQ_TRACK_FIELDS`), with `streamUrl` then dropped as below; and
  `currentIndex`/`positionMs`/`createdAt`/`updatedAt` are coerced to integers with
  `currentIndex` clamped into the track list.
- **History carries no credentialed `streamUrl` or cover-art URL.** An ordinary
  track's `streamUrl` and `imageUrl`, and a record's `coverImageUrl`, are Subsonic URLs with
  the publishing client's salted-token (or plain-password) credentials in their query, and
  history is persisted and broadcast to every device. So every track that enters a saved
  queue, from `setQueue`/`enqueue`/edits and from `syncSavedQueues` alike, **loses its
  `streamUrl` and `imageUrl`**, and a record's `coverImageUrl` is dropped the same way (by
  detecting Subsonic-style credential params — `u`/`t`/`s`/`p`/`apiKey` — in the query string,
  not by host, so a self-hosted Navidrome under any domain is caught and a public cover
  survives). The one exception is an `ext:` preview track (§16.2): both its `streamUrl` (a
  short-lived signed capability, not a credential, and the only way to play it) and its
  `imageUrl` (a public Cover Art Archive URL, not a Subsonic address, and the only source for
  its art) survive untouched. **Clients must rebuild an ordinary track's stream URL and cover
  art from its `id`** on restore (`getSongDetail`/their own stream-URL and cover-art
  builders), never expect either in a `SavedQueue.songs` entry or a record's `coverImageUrl`
  — both clients already do this unconditionally for every saved-queue card, own or peer's,
  since a peer's authed URL points at *its* server with *its* credentials and cannot be
  rendered here regardless. The live session queue is untouched: `do:load` is built from
  `s.queue`, which still carries each track's `streamUrl`/`imageUrl` for a cast bridge
  (§12.2). A state file written by an older hub is scrubbed in memory when it loads; the copy
  on disk keeps the old URLs until the hub's next state save rewrites it.
- **Deletions are tombstoned:** `deleteSavedQueue`/`deleteSavedQueues` remember the id
  (capped, persisted) **whether or not the hub currently holds the record** — a client can
  delete a row it captured offline before the hub ever merged it. A tombstoned id is then
  inert everywhere: a client that still holds the row can't resurrect it via
  `syncSavedQueues`, and a `setQueue` naming a tombstoned `savedQueueId` **mints a new id**
  rather than re-creating the record (the device that kept playing knows nothing of a delete
  made elsewhere). Deleting the *current* record also clears `session.savedQueueId`.
  Clients keep their own local tombstones for deletions made while disconnected and replay
  them in `syncSavedQueues.deleted` on the next connect.
- **Naming/art:** `sourceKind`, `sourceName` and `coverImageUrl` are set when the queue is
  born and are not clobbered by later re-publishes — but a **null is not an established
  value**: a later publish carrying a real value backfills it (clients often publish the
  queue before the collection metadata resolves). Nothing about a record's identity tracks
  playback, so a card's origin name never changes as the queue advances. **Its art does:
  a card shows the resume track** — `songs[currentIndex]`, rendered **by song id** with the
  client's own credentials (a peer's `coverImageUrl` carries that peer's server and auth),
  and its title is that track's unless the user named the queue. Every client derives both
  from the record's own cursor, so one shared record still looks the same everywhere;
  `coverImageUrl` is only a fallback. (Until 2026-09-30 the art was the **birth** track,
  which read as a thumbnail always a track behind the song playing.)
  Records are broadcast only when a queue is edited, and the hub touches the live record's
  cursor at most every `SQ_PROGRESS_THROTTLE` (5 s), so a client keeps **the live session's
  record** on the session's own `index` from `session`/`progress` frames, checked against
  the session queue by track id, and re-applies it after each `savedQueues` merge.
- **Identity is a session, not a track list.** A client mints a new `savedQueueId` only
  when the user starts playing something new (an album/playlist/radio/journey/Mood Flow
  play, a `Play.NOW`, or a restore — which reuses the restored record's id). Every edit to
  the queue being listened to — reorder, remove, play-next, autoplay top-up, shuffle —
  re-publishes the **same** id, so the record is refreshed rather than duplicated. Matching
  on queue membership instead forks a near-identical record on every edit.
- **Management:** `act:renameSavedQueue`/`deleteSavedQueue` mutate the shared history;
  `deleteSavedQueues { ids }` is the batched form used by "clear all" / "delete others" (one
  broadcast and one state write instead of one per row). `renameSavedQueue` for an id the hub
  doesn't hold answers `error { code: "unknown_saved_queue" }` — the client has already
  applied the rename locally, so silence would leave the two permanently disagreeing.
  "Save as Navidrome playlist" stays a **client-local** action (no hub involvement).
- **Lifecycle:** `act:clear` flushes the resume cursor, then detaches the session
  (`savedQueueId → null`) — the record stays in the history and remains resumable. A
  `setQueue` with an empty track list mints no record at all.

Client mapping: Feishin `store/saved-queues.store.ts` + `features/hub/hooks/use-hub.tsx`;
Navic `SavedQueueRepository`/`SavedQueueDao` + `HubManager`.

---

## 9. Optional: Navidrome interop (durable resume for *other* clients)

Independently of the hub, the hub MAY mirror session changes to Navidrome's
native `savePlayQueue` (queue ids + `current` + `position`). This is **not**
required for navi-connect's own transfer (the hub already persists), but it lets
unmodified Subsonic clients (Symfonium, DSub) pick up "where you left off" via
`getPlayQueue`. Pull-only, not real-time — purely a courtesy bridge. Toggle:
`HUB_MIRROR_PLAYQUEUE=true`.

---

## 10. Security

- Single **shared secret** (`HUB_TOKEN`) in `hello`. Wrong/absent → close.
- Intended to run inside the Unraid `media` Docker network alongside Navidrome
  and the existing bot. If exposed beyond LAN, front with TLS (reverse proxy) —
  the token is a bearer credential and must not cross plaintext WAN.
- No media or Navidrome credentials ever transit the hub; receivers hold their
  own Navidrome auth (as they do today).

---

## 11. Resolved decisions

1. **Hub port `4790`** — confirmed.
2. **Shuffle authority = hub** — confirmed. Hub computes `order` and sends it so
   every controller's "up next" agrees.
3. **Mirror to Navidrome `savePlayQueue` = ON** by default (`HUB_MIRROR_PLAYQUEUE=true`, §9).
4. **Hub language = Python** (`websockets`/asyncio) — matches the existing bot's
   runtime and Unraid `docker run` pattern; one stack to maintain. The hub is
   **independent of upstream Feishin/Navic releases** — it never tracks their
   versions, so it has no rebasing concern. Fork-maintenance only affects the
   *client* changes, which are kept as small, isolated modules (see §13) to make
   rebasing onto upstream `feishin-development` / `Navic-master` easy.
5. **iOS = out of scope for v1.** `HubClient` still lives in Navic `commonMain`,
   so iOS remains a near-free future add.
6. **`serverId`** retained in the payload (cheap, single-server-safe).

---

## 12. Device types & roles (output selection + Chromecast)

A device's `caps` and an optional `output` descriptor let the picker show
Spotify-style targets. Three receiver kinds:

| Kind | Plays where | Notes |
|------|-------------|-------|
| `local` | the device's own audio output | default for Feishin / Navic |
| `bridge-cast` | a Chromecast on the LAN, driven by a bridging client | implemented (§12.2); registers as `platform: "chromecast"` |

### 12.1 Local output device selection (per-receiver)

Output selection is a **receiver-local** concern, not session intent. Optional
protocol additions so it can also be driven remotely:

- Receiver advertises outputs in `hello`/`devices`:
  `"outputs":[{"id":"sink-3","name":"Speakers (Realtek)"}...], "activeOutput":"sink-3"`.
- Controller action `act{action:"setOutput", outputId}` → hub `do{cmd:"setOutput"}`.
- **Feishin:** maps to web-audio `HTMLMediaElement.setSinkId(outputId)` (or mpv
  `--audio-device`) — real per-output switching on Windows.
- **Navic/Android:** output routing is OS-controlled (speaker/BT/wired follow the
  system route); `outputs` may be a single "System" entry. Lower fidelity by
  platform design.

### 12.2 Chromecast (implemented — bridged virtual receiver)

The hub does **not** speak Google Cast, and needs no changes for this: a bridged
Chromecast is simply another client. A bridging client discovers Cast devices over
mDNS and opens **one hub WebSocket per speaker**, registering it as

```json
{"t":"hello","token":"…","device":{
  "id":"cast-<txt.id>","name":"📺 <txt.fn>","platform":"chromecast",
  "caps":["receiver","loadAck"],"bridgedBy":"<bridging client's device id>"}}
```

so it joins the ordinary device picker and transfer flow. Casting is therefore a
normal `transfer`, and transfer-with-resume (§7) works unmodified. Audio never
passes through the bridging client — the speaker fetches the track's `streamUrl`
directly, which is why every published track carries `streamUrl` + `mime`.

Neither client uses a Cast SDK; both speak castv2 (TLS to port 8009) directly.
**Feishin** uses `bonjour-service` + `castv2-client`; **Navic** uses `NsdManager`
+ its own castv2 implementation (`domain/manager/cast/`).

**Reachability.** The bridge's hub socket lives in a client process on the LAN; the
speaker does not. So `online` on a `cast-<id>` row means *the bridge is up*, which is
a different and much weaker claim than *the speaker is up* — see §3.2. The bridge is
the only party that can tell the difference, and it must say so:

- Assert `deviceState {reachable, appRunning}` on change and at least every 30 s.
  Reachability is a cheap TCP connect to port **8009** with a short budget; do not
  LAUNCH the receiver app to find out, because launching seizes the speaker's audio
  output (a speaker playing over Bluetooth would go silent just from being probed).
- Do not report `reachable:false` on the first miss. A speaker genuinely powered off
  mid-session should carry a grace window (~90 s) plus a failed probe before the
  bridge gives up on it; a phantom receiver for 90 s is much cheaper than dropping a
  live one.
- Read the LAUNCH reply before acting on it. A `LAUNCH_ERROR` carries `reason` and
  no `status`, and a `RECEIVER_STATUS` routinely arrives *before* the receiver app
  is up, so a bridge that takes the first reply and looks for its appId reports a
  refusal and a merely-slow launch identically — and discards the device's own
  explanation while doing it. Wait for the status that carries the app, and put the
  reason in `loaded.error`.
- Answer `do:load` with `loaded` (§7.1). This is what turns "transferred to a TV that
  has been off for days" from a 20-second silent lie into an immediate, visible
  failure with the session handed back.
- Tear the bridge down once the speaker is gone for good — an unplugged Chromecast
  emits no mDNS goodbye, so a bridge that waits for one waits forever, and its
  healthy socket keeps advertising a dead speaker to every client on every network.

**Ownership.** Both clients can see the same speaker and would register the same
`cast-<id>`. The hub closes the older socket with **`4003 superseded`** (§3), so
without arbitration the two would kick each other off indefinitely. The rule:

1. Before registering, check the hub's `devices` for an **online** `cast-<id>`;
   if present, stand down.
2. Re-evaluate on every `devices` frame, so a bridging client quitting hands the
   speaker over.
3. Delay a random 0–3 s before claiming, then re-check — this settles a
   simultaneous start without any frames being exchanged.
4. On being closed with `4003`, **stay down for 5 minutes**. This is the circuit
   breaker; a client that reconnects immediately recreates the war.
5. A **reconnect is a claim**. Re-run steps 1-3 before *every* re-registration,
   not only the first — the hub evicts on any re-registration of the id, so a
   bridge that dials straight back in after an ordinary socket blip kicks off
   whoever took the speaker while it was away. Standing down for this reason
   carries no 5-minute penalty: nobody was evicted, so there is no war to break,
   and the bridge should be free to pick the speaker back up the moment the
   holder lets go.

**Adoption.** The bridge lives inside a client process, so restarting that client
kills it while the speaker keeps playing. On reconnect a bridge must re-join the
running receiver session rather than orphan it. It adopts when the hub still names
it active **or** when the session is orphaned (`activeDeviceId == null` and a
non-empty queue) — the latter is the common case, because the hub relinquishes the
active slot whenever the active device drops. Claiming an orphaned slot must be
earned: only if the receiver's current `contentId` matches a track in that
session's queue. The claim is an `act setQueue` carrying the session's own
`savedQueueId` — a `report` from a non-active device is discarded, and reusing the
id keeps the saved-queue history record from forking (§8.3).

---

## 13. Deliverables roadmap (after this spec is approved)

1. **Hub** container — Python (`websockets`/asyncio), session/registry/persistence,
   `savePlayQueue` mirror, Dockerfile + Unraid `docker run` snippet.
2. **Feishin** main-process `hub-client.ts` + renderer queue-publish + `do:load`,
   kept as an isolated module for easy upstream rebasing.
3. **Navic** `commonMain` `HubClient` + `MediaPlayerViewModel` wiring + device-id
   persistence + Android foreground-service keepalive.
4. Reference **controller test harness** (CLI) to exercise transfer/resume before
   touching UI.
5. **Device picker UI** in both clients (transfer button + local-output submenu).
6. Chromecast bridge (§12.2) — done in both clients, each speaking castv2 directly.

---

## 14. AudioMuse Tier-2 HTTP proxy (`/sonic/*`)

Not part of the WebSocket message catalog: these are plain HTTP requests served on
the **same port** (`4790`) by the same listener, so the clients can reach the
AudioMuse-AI core through the one component that is already authenticated and
remotely reachable.

Enabled when `AUDIOMUSE_URL` is set **and** `HUB_TOKEN` is non-empty (an empty
token would make it an open relay). Otherwise `/sonic/*` answers `503` — except
the probe, which answers `{"configured": false}`.

**Auth:** `Authorization: Bearer <HUB_TOKEN>` — the same secret as `hello`,
compared with `hmac.compare_digest`. Missing/wrong → `401`, no upstream call.

**Routes** (a whitelist — an unlisted path or method is `404` with no upstream
call, and unlisted parameters/body keys are dropped rather than relayed):

| Hub route | Upstream | Forwarded |
|---|---|---|
| `GET /sonic/fingerprint` | `GET /api/sonic_fingerprint/generate` | `n` |
| `POST /sonic/alchemy` | `POST /api/alchemy` | `items[] {op,id,type}`, `n`, `temperature`, `subtract_distance` |
| `POST /sonic/clap/search` | `POST /api/clap/search` | `query`, `limit` |
| `GET /sonic/clap/stats` | `GET /api/clap/stats` | — |
| `GET /sonic/score` | `GET /get_score` | `id` |

The hub injects `Authorization: Bearer $AUDIOMUSE_TOKEN` and, on the fingerprint
route, `navidrome_user` / `navidrome_password` from `HUB_ND_USER` / `HUB_ND_PASS`.
**Clients never send any of those** — a client-supplied `navidrome_password` is
dropped like any other unlisted parameter.

**Probe.** `GET /sonic/clap/stats` doubles as the Tier-2 liveness probe and always
returns `200`: the upstream stats body (`clap_enabled`, `song_count`, `loaded`)
plus the hub's own view, so both clients agree on Tier-2 state from one source.

```json
{ "clap_enabled": true, "song_count": 1234, "loaded": true,
  "configured": true, "upstreamReachable": true }
```

**Other statuses are passed through unchanged** (`503` cold index, `404`
unanalyzed track, `400` feature disabled), so the clients' existing fail-soft
branches work untouched. `502` = the hub could not reach AudioMuse.

**Limits.** At most 4 upstream calls in flight (a hung core must not stall the
1 Hz progress fan-out); 20 s timeout; successful `GET` responses and CLAP
searches are cached ~60 s and shared by all clients. `/sonic/alchemy` is **not**
cached — it is stochastic and Mood Flow re-asks with the same seeds.

---

## 15. lb-bot HTTP proxy (`/lb/*`)

The same mechanism as §14 — plain HTTP on port `4790`, whitelist, `HUB_TOKEN`
bearer, shared cache — pointed at lb-bot, which knows what is *missing* from the
library. Both proxies subclass one
`HttpProxy`; `process_request` tries each in turn and falls through to the
WebSocket handshake when neither claims the path.

One extra reason the proxy is mandatory here: **lb-bot's Flask API has no
authentication of any kind and binds `0.0.0.0:8899`.** It can never be exposed
directly, and the whitelist is what keeps `HUB_TOKEN` from unlocking its
delete-file, trash and prefs routes.

Enabled when `LBBOT_URL` is set **and** `HUB_TOKEN` is non-empty. Otherwise
`/lb/*` answers `503` — except `/lb/status`, which answers `{"configured": false}`.

`/lb/status`, `welcome.lb` and the `lb` frame all carry **`webUrl`**: where a
*person* opens lb-bot's own web UI, so a client screen that has to hand the user
over to lb-bot's workspace (Fill gaps with nothing left to pick) can link straight
to it, e.g. `webUrl + "/#/gaps/" + groupId`. It is `LBBOT_WEB_URL` when set, else
`LBBOT_URL` (the same address on a LAN install); `""` while the proxy is disabled,
and never anything but an `http(s)` URL. It is only a link. Nothing is proxied
through it, and it grants nothing: lb-bot has no auth, so whoever can reach that
address can already use it. With the default, the link works only on the LAN;
set `LBBOT_WEB_URL` to a reachable hostname for away-from-home use. Clients
validate it again (http(s) only) and hide the link when it is empty. Added
2026-09-24; an older hub omits it and clients treat that as `""`.

| Hub route | Upstream | Forwarded | Cache |
|---|---|---|---|
| `GET /lb/status` | `GET /api/health` (on a 404, once more on `GET /api/summary`) | — | 60 s |
| `GET /lb/index/changes` | `GET /api/index/changes` | `since`, `epoch` | **never**, and never coalesced (sync pool, §15.3) |
| `GET /lb/index/keys` | `GET /api/index/keys` | — | **never**, and never coalesced (sync pool, §15.3) |
| `GET /lb/artist/discography` | same | `nd_id`, `mbid` | 60 s (cleared by `/lb/notify` and by `/lb/index`) |
| `POST /lb/artist/discography` | same | `mbid`, `name`, `nd_id`, `external` | — (may answer `task_id: ""` with `covered: true`, §15.4) |
| `POST /lb/artist/release` | same | `rgid`, `mbid`, `nd_id`, `name`, `external`, `title`, `artist`, `type`, `year` | — |
| `GET /lb/fresh-releases` | same | `days`, `limit` | 60 s |
| `GET /lb/album/releases` | same | `rgid` | 6 h |
| `GET /lb/album/tracklist` | same | `release_mbid`, `album_ids`, `group_id` | 6 h |
| `GET /lb/album/similar` | same | `artist_mbid`, `artist_name`, `rgid`, `limit` | 6 h |
| `GET /lb/artist/similar` | same | `mbid`, `name`, `limit` | 60 s |
| `GET /lb/artist/related` | same | `mbid`, `name`, `limit` | 60 s (cleared by `/lb/notify`) |
| `GET /lb/deezer/chart` | same | `limit`, `genre` | 6 h (cleared by `/lb/notify`) |
| `GET /lb/deezer/editorial` | same | `limit`, `genre` | 6 h (cleared by `/lb/notify`) |
| `GET /lb/deezer/genres` | same | — | 6 h (**not** cleared by `/lb/notify`) |
| `POST /lb/resolve-link` | same | body `url` | — |
| `GET /lb/wishlist` | same | — | 60 s (cleared by `/lb/notify`) |
| `POST /lb/wishlist` | same | `rgid`, `artist`, `title` | — |
| `POST /lb/wishlist/remove` | same | `rgid` | — |
| `GET /lb/artist/lookup` | same | `q` | 60 s |
| `GET /lb/album/lookup` | same | `q` | 60 s (cleared by `/lb/notify`) |
| `GET /lb/meta/artist` | `GET /api/meta/artist` | `mbid`, `name`, `refresh` | 6 h |
| `GET /lb/meta/album` | `GET /api/meta/album` | `rgid`, `release_mbid`, `refresh` | 6 h |
| `GET /lb/album/sources` | same | `rgid`, `release_mbid`, `artist`, `album`, `total` | 60 s |
| `POST /lb/album/download` | same | `rgid`, `release_mbid`, `artist`, `title`, `total_tracks`, `sourceUsername`, `sourceFolder`, `quality`, `excludeUsers`, `allowMp3`, `allFiles` (boolean, §15.5) | — (45 s timeout) |
| `GET /lb/album/status` | same | `release_mbid`, `rgid` | **never** (fast pool) |
| `GET /lb/fills` | `GET /api/fills` | `release_mbids`, `group_ids` (comma-separated, ≤ 32 each) | **never** (fast pool) |
| `POST /lb/album/cancel` | `POST /api/album/cancel` | `release_mbid` | — (fast pool) |
| `POST /lb/album/allow-mp3` | `POST /api/gaps/{group_id}/allow-mp3` | `group_id`, `allow` | — |
| `GET /lb/gap` | `GET /api/gaps/{group_id}` | `group_id`, `sourcePage` | **never** (fast pool) |
| `GET /lb/gap/source-files` | `GET /api/groups/{group_id}/sources/{source_index}/files` | `group_id`, `source_index` | 60 s |
| `POST /lb/gap/search` | `POST /api/groups/{group_id}/sources` | `group_id`, `force` | — (45 s timeout) |
| `POST /lb/gap/auto` | `POST /api/gaps/{group_id}/auto` | `group_id` | — |
| `POST /lb/gap/fetch` | `POST /api/gaps/{group_id}/fetch` | `group_id`, `sourceId` | — |
| `POST /lb/gap/cancel` | `POST /api/gaps/{group_id}/cancel` | `group_id` | — (fast pool) |
| `POST /lb/gap/rescan` | `POST /api/gaps/{group_id}/rescan` | `group_id` | — |
| `GET /lb/overrides` | same | — | 60 s (cleared by any override or merge POST on a 2xx, by `/lb/notify`, by `/lb/index`, and by every answer of the two polled job routes, §15.5) |
| `POST /lb/artist/override` | same | `nd_id`, `mbid`, `name` | — (45 s timeout) |
| `POST /lb/artist/override/remove` | same | `nd_id` | — (45 s timeout) |
| `GET /lb/album/override` | `GET /api/album/override` | `album_id`, `id` | **never** (fast pool when the request carries `id`, §15.5) |
| `GET /lb/album/covers` | same | `rgid`, `release_mbid` | 6 h (45 s timeout) |
| `POST /lb/album/override/preview` | same | `album_id`, `release_mbid` | — (45 s timeout) |
| `POST /lb/album/override` | same | `album_id`, `release_mbid`, `cover_url`, `force` (boolean) | — (45 s timeout) |
| `POST /lb/album/cover` | same | `album_id`, `cover_url` | — (45 s timeout) |
| `POST /lb/album/override/remove` | same | `id` | — (45 s timeout) |
| `GET /lb/artist/merge/preview` | same | `nd_id`, `from_nd_id` | **never** (45 s timeout) |
| `POST /lb/artist/merge` | same | `nd_id`, `from_nd_id` | — (45 s timeout) |
| `GET /lb/artist/merge` | same | `id` | **never** (fast pool) |
| `POST /lb/artist/merge/undo` | same | `id` | — (45 s timeout) |
| `GET /lb/listening/status` | same | — | **never** (fast pool) |
| `GET /lb/listening/summary` | same | `period`, `tz` | **never**, and never coalesced (§15.4) |
| `GET /lb/listening/unowned` | same | `period`, `kind`, `limit` | **never**, and never coalesced (§15.4) |
| `GET /lb/listening/recent` | same | `before`, `limit` | **never** |
| `POST /lb/listening/sync` | same | — (any body is dropped) | — |

No upstream token is injected — lb-bot has none to inject.

**Three slot pools.** Every route above shares `PROXY_MAX_INFLIGHT` (4) upstream slots
per proxy, first come first served — except the routes marked *fast pool* (six, plus the two polled job routes of §15.5), which have
two slots of their own (`PROXY_FAST_INFLIGHT`), and the two marked *sync pool*, which
have one (`PROXY_SYNC_INFLIGHT`, §15.3). A status poll used to queue behind four `album/sources`
fan-outs (150 s each) until the handshake deadline dropped its socket with no
answer at all. Waiting for a slot times out (`PROXY_QUEUE_TIMEOUT_FAST`, 10 s on the
fast and sync pools; the route's own timeout on the default one) into `503 {"error": …,
"busy": true}` rather than a dropped connection; identical `GET`s in flight share
one upstream call (except the two sync routes and the listening `summary` / `unowned`, whose specs say `coalesce: False`); a timed-out upstream is `504 {"busy": true}` and a refused one
`502 {"down": true}`, so a client can say which. `POST /lb/wishlist`,
`/lb/wishlist/remove` and `/lb/artist/release` invalidate their own cached reads on
success — `/lb/notify` covers a landing, not the user's own action.

**Two TTLs that look wrong and are not.** `/lb/album/similar` is cached for six
hours and `/lb/artist/similar` for sixty seconds, from the same upstream
similarity merge. The difference is what each answer contains: the albums route
returns records the library already holds, which do not change; the artists route
marks each row `owned`/`indexed`, lb-bot recomputes that marking per call, and it
is the only part of the answer that moves. A six-hour TTL there would freeze the
badges that decide whether a row offers a download. For the same reason
`/lb/artist/similar` is in the set `/lb/notify` invalidates and
`/lb/album/similar` is not — a fill changes who you own, and nothing about
editions or tracklists.

**The two `lookup` routes are not symmetric either.** Both are a live
MusicBrainz search on lb-bot's global 1 req/sec lock, so both are short-TTL and
both expect the client to send only a debounced term of three characters or
more. But `/lb/album/lookup` additionally marks each candidate `releaseOwned`
with the Navidrome album id behind it (`releaseAlbumId`) — resolved from the
library index, at no MusicBrainz cost — so it is in the invalidation set and
`/lb/artist/lookup` is not. A client renders an owned candidate as a library
row that opens the album, never as a download; a stale badge there is the
"the tile said the library holds it and the tap opened the download page" bug
that the Fresh tab and the similar-albums shelf have each paid for once.

**The Deezer rows are cached long and invalidated anyway**, which looks
contradictory and is not. Their *content* — a chart, an editorial selection —
barely moves, so six hours is right. What moves is the ownership marking on each
row, and a six-hour-old "you don't own this" badge outlives the fill that makes it
false; a stale badge is a tile that offers to fetch a record already on disk.
`/lb/artist/related` is in the same invalidation set but is **not** cached long:
it keeps the 60 s default, like its sibling `/lb/artist/similar` and for the same
reason (the expensive half is cached upstream, the marking is recomputed per call).
`/lb/wishlist` is in the
invalidation set for the opposite reason: a landing is precisely what takes a row
*off* it, so the whole list is what the fill falsifies.

**`/lb/artist/related` is deliberately not more rows on `/lb/artist/similar`.**
Deezer is a third similarity source beside ListenBrainz Labs and Last.fm, but the
merge behind `similar` is a ranking those two agree on; folding a third provider
into it would move every existing row's position for the sake of adding some. A
client that wants both asks for both and says which is which.

**`POST /lb/resolve-link`** turns a pasted streaming URL into the MBIDs behind
it, so "share this to the app" lands on a real album page:

```jsonc
{ "kind": "artist|album|track|unknown", "mbid": "", "rgid": "",
  "artist": "", "title": "",
  "provider": "spotify|deezer|apple|ytmusic|tidal|qobuz|musicbrainz",
  "confidence": 0.0-1.0 }
```

`confidence` is on the wire because the providers resolve differently: a
MusicBrainz URL resolves directly with no network call, a Spotify or Deezer id
resolves through that provider's API, and an Apple/YT-Music/Tidal/Qobuz URL is
resolved by *searching* MusicBrainz for the artist and title scraped from the URL
or its page title. Not cached — it is a one-shot the user initiated.

**`refresh` is not part of the cache key.** On the two `meta` routes it means
"do not answer this from a cache", so the hub bypasses its own copy *and* writes
the fresh answer under the ordinary key — where the next plain read finds it.
Keying on it instead would give a refresh its own slot and leave the stale copy
every other caller reads untouched, for the full six hours.

**The `gap` group** fills the holes in an album the library already partly has —
a different pipeline from `album/download`, which acquires a release it lacks
entirely. Every route is scoped to one review group, and the client always has
that id already: lb-bot's discography scan *builds* the review group as it
classifies a release `incomplete`, and emits its `group_id` on the row. That holds
for every scan — manual build, per-artist scan and lb-bot's background auto-index
worker alike — so a `group_id` on a row from the index feed (§15.3) resolves here
too. `auto`
searches, ranks and enqueues in one shot; `fetch` queues a hand-picked source by
its index in the `sources` list; `rescan` re-reads the album from Navidrome and
walks its folder, so it gets the 45 s timeout.

**`search` and `auto` are the two halves of one operation, split on purpose.**
`auto` searches, ranks and commits to the top folder in one shot; `search` stops
after ranking so a client can show the candidates. Gap fills need the review step
more than whole-album downloads do, not less: the tracks land *inside* a record
the user already owns, so a different pressing contaminates the album rather than
merely disappointing — and a source covering 12 of 17 slots is a perfectly normal
ranking result that looks like a bug once it has happened.

**`search` flips the group to `picking` before it has found anything.** The POST
approves the pending missing tracks and *then* starts the background search, so
the very next poll reads `picking` with an empty `sources` list. A client that
treats `picking` as terminal — as "lb-bot wants a decision in its own web UI",
which is what that state means in every other context — stops polling one tick
after asking, and the results arriving thirty seconds later are never read. Gate
every terminal judgement on `sourceTask.status`: while it is `running` (or
`queued`) nothing the group says about itself is final. Note the task's own end
states are `complete` and `error`, never `finished`.

`search` also carries the 45 s timeout, and not because the search is slow — the
POST returns at once. A search already in flight holds lb-bot's process-wide
review lock in bursts, so a *second* press blocks inside the handler; on the
default 20 s that surfaced to the user as a bare 502 for what is really "busy".
Clients should disable the button while `sourceTask` is running regardless.

**Both answer with a `task_id`, and clients must ignore it.** The `GET` carries
`sourceTask` — the same background search's `{status, current, summary, error}` —
precisely so nothing needs `/api/tasks`, which is not whitelisted (see below) and
must stay that way. Gap *download* progress is likewise not a task: a gap fill is
per-track, so it is counted off `tracks[].state` in the same `GET`.

**`gap/source-files` is the counterpart to stripping `gap`.** The poll can't carry
peer file listings every five seconds, but coverage counts alone never settle
"does this peer have my seventeen missing tracks, or twelve from another
pressing". So the listing is fetched once, on demand, when a source is opened —
each file tagged with the tracklist slot it would fill, and a file matching no
slot marking the folder as the wrong album however plausible its name. Upstream
expands the peer's directory for real, hence the long timeout; `expanded: false`
means the peer was unreachable and the rows are search hits rather than the real
folder, which clients must say rather than imply otherwise.

**`GET /lb/gap` is projected.** Its upstream embeds every ranked source's entire
peer file listing, ten sources to a page: hundreds of KB on a path a phone polls
every 5 s, into a cache bounded by entry count rather than bytes. The hub strips
`sources[].files`, `sources[].filesTruncated`, `tracks[].placeFolder`,
`tracks[].placeFile` and `tracks[].manualPick` (`spec["strip"]`), keeping the
per-source summaries the pickers actually render — peer, format, bitrate,
coverage, risk flags, score.
`placeFolder`/`placeFile` are lb-bot HOST paths (an absolute `/downloads/<folder>`
path and a peer's raw filename) that only lb-bot's own SPA needs, reading them
directly rather than through the hub. `manualPick` (stripped since 2026-10-12) is
lb-bot's record of the file picked by hand for a track; its `filename` is a
peer's raw slskd path, and neither client reads any key of the object, so the
whole of it goes. It is a projection with two reasons —
bandwidth (`sources[].files`) and path hygiene (`placeFolder`/`placeFile`,
`manualPick`) — not an access control (the whitelist is that). A non-200, a
non-JSON body or an unexpected shape passes through untouched: a projection must
never fail a request.

**`placeFolder`/`placeFile` are stripped a second time, lb-bot-side, on the poll.**
`GET /lb/gap` is one read of one group; both clients also poll `GET /lb/fills`
(`?release_mbids=&group_ids=`) every 30 s for everything they are watching, and
until 2026-09-28 lb-bot's own `_fills_view` answered each gap with
`frame.pop("summary")` — the full `_gap_detail_view` minus `sources`, with
`placeFolder`/`placeFile` still on every track — so the two fields reached
devices unstripped on the most-polled route even though the hub was already
scrubbing the far less-polled single-group read. `_fills_view` now drops them
from every track of a gap summary before answering (a copy, not the same dicts
`GET /api/gaps/<id>` — lb-bot's own SPA's route — hands back with them intact).
The hub has no rule of its own on `/lb/fills`: what a gap summary's tracks carry
there, `manualPick` included, is `_fills_view`'s to decide.
**The POST replies that echo a gap are projected too** (since 2026-10-02).
`/lb/gap/fetch` (both its success and `alreadyActive` replies),
`/lb/gap/auto`, `/lb/gap/cancel`, `/lb/gap/rescan` and `/lb/album/allow-mp3`
nest the gap view under `gap`; their `strip` rules are the `GET /lb/gap` rules
with a `gap.` prefix (`_strip` walks a dotted key), derived from the same tuple so
the two cannot drift. No client reads `gap` from these replies — they read `ok`,
`error`, `alreadyActive` — so the projection removes nothing anyone renders. Still
unstripped, as for every route: an error body (a failed fetch is a 400 whose
`nextSource` carries a peer's filenames) — the projection only touches a 200.

**`album/status` says what kind of failure it was.** Alongside the free-text
`reason` — which stays verbatim, because "103 peers offered 2,047 files, but none
in FLAC, OPUS" is the evidence and paraphrasing it loses the answer — a failed
fill carries `failureKind` (`no_source` | `format_rejected` | `transfer_failed` |
`placement_failed` | `mb_unavailable`), `retryable` and `attempts`. All three are
present and falsey on anything that has not failed, so a client reads them
unconditionally. `retryable` means *a plain Retry is worth offering*: it is
deliberately **false** for a format rejection that MP3 would fix, because
re-running the identical search against the identical peers under the identical
format policy is not a retry — Allow-MP3-and-retry is the action, and
`mp3WouldHelp` is what says so; `allowMp3` on `album/download` is how a client
takes it on the whole-album path, which has no review group to set the opt-in on.
Only `mb_unavailable` and `transfer_failed` auto-retry upstream, once per
user-initiated fill; `no_source` never does, since lb-bot already walked its whole
ranked source list and the user asking again is the new information. `attempts`
counts fills **started** (the user's and lb-bot's own automatic retries) and
survives an lb-bot restart, so a client can tell one failure from four; `retryAt`
(epoch seconds, 0 when none) says when the automatic retry fires, so a client
renders a countdown rather than a dead "failed" that then quietly comes alive.

**`cancelled` is a state, not a failure kind.** `state: "cancelled"` is terminal,
with `retryable: false` and no `failureKind`; a cancel is the user's verdict and
neither client offers a Retry on it — a cancelled album is started again from the
album page. lb-bot marks the row and detaches the transfers before it talks to
slskd, refuses every later write over a cancelled row, and claims a row for
placement atomically against the cancel, so nothing cancelled is ever placed. The
cancel routes answer `cancelled: false` (still `ok: true`) with the current status
when it is too late — placement has begun — or when nothing was running; a client
shows lb-bot's status then, never a row claiming a cancel that did not happen.
(An lb-bot from before 2026-09-23 wrote a cancel as `failed` + `failureKind:
"cancelled"`; both clients still read that shape as `cancelled`.)

**The rest of the view is honest, and states the Cancel rule.** `done` is files
**completed**, never completed+failed (`failed` sits beside it); `percent` is
byte-based when the transfers report sizes and 100 on `placing`/`placed`/
`verified`; `bytesDone`, `bytesTotal`, `speedBps` and `activeFiles` come off the
transfer records lb-bot's poller already keeps; `updatedAt` moves on state changes
*and* transfer progress; `serverTime` is lb-bot's clock, for "last checked Ns ago"
without trusting the client's; `verifyGaveUp` marks a `placed` row past the
verifier's deadline (on disk, not in Navidrome); and **`cancellable`** is the one
Cancel rule, stated by the server — `searching`, `queued` or `downloading`, or a
`failed` row whose `retryAt` is in the future — so both clients render the button
from it rather than each keeping a list of states (one of them offered Cancel on a
row already being placed). `files[]` (title, slskd state, percent; ≤ 40) rides on
the single-item read only, never on `/lb/fills` or the `fill` frame. lb-bot's
poller — the only clock any of these counters move on — runs every 5 s while a
transfer is live and every 60 s idle, one slskd listing per tick whatever the
number of clients watching; the request path never touches slskd.

**`/lb/fills` is the poll.** `?release_mbids=a,b&group_ids=g1` (≤ 32 each) answers
`{ albums: { <release_mbid>: <status minus files> }, gaps: { <group_id>: <gap minus
sources> }, serverTime }` — every fill a client watches, in one read on the fast
pool. A Download Center with eight rows used to make eight requests per tick.
Both clients read it every 30 s while the hub socket is up and `fill` frames have
arrived within the last minute, and at 5 s backing off to 10/20 s on identical
answers otherwise; the modal or sheet on one album keeps its 5 s single-item
`album/status` read while open, for the file list.

**`artist/release` is the single-row index refresh.** The only other writers into
lb-bot's `release_groups` table are a whole-artist delete-and-reinsert (reachable
only from a full scan) and a flip-to-`present`, so the smallest unit of refresh
used to be an entire artist at one MusicBrainz request per second per
release-group. Since a stored discography is served immediately even when stale
*by design*, a release published last week is simply absent — which is what made
every Fresh row lead to an artist page that did not list the album it came from.
This adds or re-classifies one release-group for one or two MusicBrainz calls,
using the same classifier the full scan runs per row, so an `incomplete` result
gets a real `group_id` and the gap-fill workspace is reachable from it. Call it
when a rgid is not in the artist's discography; never offer a full rescan for one
album. It appears in `GET /lb/status`'s `routes`, which is how a client that
ships ahead of its hub finds out the button would do nothing.

Note the status value it can store: **`present` is a fifth value no scan ever
produces** (the scanner emits only `complete` / `incomplete` / `untagged` /
`missing`). Every status map must tolerate it, and it is the documented reason no
client may filter discography rows on `status === 'missing'`.

It also takes an **optional caller override** — `title` (required for the
override to apply), plus `artist`, `type`/`primary_type` and `year` — used only
when MusicBrainz does not answer. lb-bot caches a transient failure for five
minutes *per exact query string* and returns `{}` inside that window without
asking again, so one 503 turned this route into a hard 502 on that album for
everyone who asked next. Every caller reaches it from a Fresh row or an album
page that already holds all four fields, which is the same escape hatch
`album/sources` and `album/download` have. (The narrower half of that trap is
gone too: the index add now asks MusicBrainz with the *same* `inc=` string the
album page sends, so the two share a cache entry and a page that rendered cannot
fail to index. `GET /lb/album/releases` returns `primaryType` and `year`
alongside `title`/`artist` so a client has the override to hand.)

**It can refuse with `409 {"error", "code": "artist_conflict", "artistKey"}`.**
Navidrome keys artists by name, so an `mbid` plus a same-name library
artist's `nd_id` used to file the release under the *wrong* artist's discography.
lb-bot now answers 409 when the artist MBID the caller named (`mbid`, or the MBID
inside an `mb:` `nd_id`) disagrees with the MBID already on record for the artist
row it would file under; `artistKey` is that row's key. A row with no MBID on
record is not a conflict. The hub passes the 409 through unchanged. It is a
refusal, not a transient failure: retrying the same body gets the same answer.

**`album/releases` also names the artist's MBID (`artistMbid`).** It always
fetched the release-group with `inc=artist-credits` to build the display credit
and dropped the id sitting in the same payload, which mattered because of where
an album page is reached from: a **Deezer** browse row carries no MBIDs at all,
so a client arriving from one had the artist's *name* and no way to open their
page — and the artist page is the only route to "scan this artist's
discography". It is the **first** credited artist, not a merge: a tap has to land
on somebody, the display credit still carries the whole thing, and the lead
credit is the only defensible answer for a collaboration. Additive, and costs no
MusicBrainz request.

**lb-bot backfills the Navidrome album ids of `present` rows.**
Placement can only flip a row to `present`; nothing on that path knows the album
ids, and only a full rescan otherwise filled them in. But a `present` row counts
as owned, so a filled album badged "in library" carried `navidrome_album_ids:
[]` — and every "open the real album" path in both clients reads exactly that.
lb-bot resolves those rows against the cached Navidrome album index by normalized
title within the artist (no MusicBrainz) and persists what it finds. Until
2026-09-24 it did so on every `GET /lb/artist/discography`; it now runs right after
`albumIndexed` for that artist and from a 300 s sweep, so the read is pure SQLite
and the ids reach the §15.3 mirrors through the feed like any other write. A row
that matches nothing keeps its ids empty and stays
`present`: Navidrome may genuinely not have scanned yet, which is what Navic's
`pendingSync` state is for.

**`fresh-releases` is capped, and the cap is not cosmetic.** Unbounded, the route
answers with the entire site-wide ListenBrainz window for the period — routinely
past the proxy's `PROXY_MAX_RESPONSE` ceiling. Clients send `limit` (400 is
plenty; the page filters and buckets locally) and read back `total` and
`truncated` alongside `releases`, so they can say "showing N of M". lb-bot keeps
**every row whose artist is in the library** whatever the limit and fills the
remainder by listen count, so `truncated` never means "we dropped something you
own" — an obscure artist you have is exactly the row a plain popularity cut would
lose. The route also carries `PROXY_SLOW_TIMEOUT`: a cold call is a ListenBrainz
fetch plus a full Navidrome artist walk, which outlasts the default 20 s.

Each row carries **three** ownership fields, and they are not interchangeable:
`artistOwned` (+ `artistId`) says the artist is in the library, `releaseOwned`
says this exact release-group is on disk, and **`releaseAlbumId`** is the
Navidrome album behind `releaseOwned` — the handle a tile badged "in library"
opens. Without it a client could only send such a row to its virtual album page
and rely on that page's own redirect, which reads the index's album ids; an album
lb-bot filled itself has none until the backfill above runs, so a tile that said
the library holds the record opened a *download* page.

**An oversized upstream body is a 502, never a truncated 200.** `read(n)` on the
upstream response truncates *silently* — the status stays 200 and the body is a
half-finished JSON document, which every client then fails to parse and reports
as "couldn't reach the service". The proxy reads one byte past the ceiling and
refuses with `{"error": …, "tooLarge": true}` instead. This was invisible on
every route until the fresh feed grew past 4 MB and took both clients' tabs down
at once; a corrupt success is worse than a clean error.

**TTLs are not uniform, deliberately.** `artist/discography` is an instant SQLite
read upstream, so 60 s is plenty. `album/releases` / `tracklist` / `similar` sit on
rate-limited MusicBrainz and ListenBrainz calls and return immutable answers, so
they get 6 h and a 45 s timeout. `album/status` is the download-progress poll and
is never cached.

**`quality`** overrides lb-bot's global Source preference for one album only —
`flac-any` | `flac-16-44` | `highest-bitrate` | `prefer-opus`, or omitted for the
global setting. lb-bot rejects an unknown value with `400` rather than ignoring
it: quietly fetching 24-bit/96 kHz after being asked not to is the failure that
matters. The hub only whitelists the key; the enum lives upstream.

**Path parameters.** `allow-mp3` and the `gap` group are the upstream routes with
an id in the URL. Clients send it in the **body** (or, for the `GET`, as a query
parameter) so the route table stays a table of exact paths; the hub validates it
against `^[A-Za-z0-9_.:-]{1,64}$` before interpolating and removes it from what
is forwarded. A non-token id is `400`, not an escaped attempt to leave the
whitelist.

**`album/sources` is what makes a download reviewable.** It returns the ranked
slskd folders with coverage paired against the *canonical MusicBrainz tracklist*
rather than counted — a folder holding a different album with enough files in it
used to report as a complete match — plus `albumMatch`/`albumMatchOk`,
`artistMatch`, `yearInPath` and live/compilation risk flags. A client shows these
before committing, then passes the chosen `sourceUsername` + `sourceFolder` to
`album/download`, which floats that peer to the front of its own ranked list and
keeps the rest as failover. So the pick is a strong preference, not a guarantee:
if the peer has gone by transfer time, the best ranked folder wins.

Unlike `/lb/gap` this route is **not** stripped. It is a one-shot read the user
asked for, and the per-file `matchedTo` rows are the evidence being judged;
`_source_files_view` already caps the list upstream. Different cost profile from a
5-second poll, deliberately different rule.

**`artistVerified` on source rows.** Every source row, on
`album/sources` and in `/lb/gap`'s `sources`, may carry `artistVerified`:
whether lb-bot found evidence in the folder that it is the right *artist*, not
just a matching title. **A missing field means verified** (an lb-bot older than
the field, and every row that has the evidence). Both clients read it the same
way:

- A row with `artistVerified: false` is labelled **"Artist unverified"**.
- The picker **never preselects** an unverified row. When every row is
  unverified, nothing is preselected and the user must choose.
- **One-tap acquire never takes an unverified source** (Feishin
  `useAcquireAlbum`, Navic `LbBotManager.acquire`): it answers "needs review" and
  the user picks, whatever else the top row says. lb-bot's own `recommended` is
  already only true on a verified rank 1, and `album/sources` adds `verifiedCount`.
- A user's explicit pick of an unverified row is consent, for that source only.
  lb-bot's unattended paths (gap Auto, failover to another source) never pick one
  themselves.

**Probe.** `GET /lb/status` always returns `200` with just the verdict. Its upstream
is lb-bot's `GET /api/health` (`{ok, epoch, headSeq}`, no Navidrome and no
MusicBrainz behind it). Its `headSeq` is the head lb-bot has already **recorded** in
its high-water mark for that epoch (0 before one is), never a higher live one, and
it answers `200` even when that record cannot be written: a disk fault that only
affects the index mirror must not read as lb-bot down (the feed routes keep their
`503`, §15.3). An lb-bot older than that route answers it `404`, and the
hub then asks `/api/summary` once instead (the route spec's `fallback`) — a hub
deployed before its lb-bot must not report a healthy lb-bot down. Neither body is
passed through; the summary in particular is a large object about the Fill-gaps
workspace. Every `/lb/status` read that went upstream (a cache hit does not) also
sets the hub's own availability verdict, which `welcome.lb` and the `lb` frame
carry (§15.3):

```json
{ "configured": true, "upstreamReachable": true, "routes": ["GET /lb/gap", "…"],
  "webUrl": "http://…:8899" }
```

`routes` is every route this hub can proxy. Clients ship independently of the hub
and the hub is a long-running process, so "the client is newer than the hub" is a
permanent condition rather than an edge case — and without this it surfaces as a
button that does nothing, because an unknown route is an ordinary `404` and not an
error any HTTP client raises. A client that finds its route missing can say the hub
needs restarting instead of failing silently. Absent from an older hub, so treat an
empty list as "assume supported".

**Not proxied, on purpose:**

- **`/api/cover/<id>`** — it serves *Navidrome* art keyed by a *Navidrome album
  id*, which a release the library lacks does not have. Art for unowned releases is
  `https://coverartarchive.org/release-group/<rgid>/front-250`, fetched directly by
  the client. It would also put multi-megabyte bodies in a cache bounded by entry
  count, not bytes.
- **`/api/tasks` and `/api/tasks/<id>`** — both go through lb-bot's
  `_review_snapshot()`, a deep copy of its entire multi-MB review state under a
  process-wide lock. Polling that would be the single most expensive thing a client
  could do. Download progress comes from `/lb/album/status`, which reads only the
  one album's transfer group, and gap progress from `/lb/gap`, which reads one
  review group.
- **`GET /api/gaps`** (no id) — the whole-library gap list, unbounded in size and
  needed by nobody: a client only ever acts on a group whose id arrived on a
  discography row. The per-group routes above are whitelisted; the list is not.
- **The placement / match workspace**, `/api/gaps/<id>/duplicate-files`, prefs,
  delete-file, trash and the beets import paths. `picking` *with no sources* is
  three different cases, and only the first is a hand-off: **a pending match**
  (lb-bot's `needs_match` bucket — some track reads `downloaded`) is a decision
  only lb-bot's own match workspace can take, so the clients say so, point there
  and stop, rather than growing a second copy of that workspace; **a search that
  found nothing** (`noSourceReason` set) shows lb-bot's reason and offers a search
  again; **sources that aged out** (neither — the tracks were picked by a search
  whose results lb-bot no longer holds) say so and offer a search again.
  `picking` *with* sources is the ordinary "your move" of the picker above and
  must not be worded as a hand-off.

### 15.1 `POST /lb/notify` and `POST /lb/fill` — the two inbound routes

Everything else under `/lb/*` is the hub calling lb-bot. These are lb-bot calling
the hub. (A third inbound route, `POST /lb/index`, carries the index head rather
than a library or ledger event; it is specified with the mirror in §15.3, and the
auth and "answered whether or not `LBBOT_URL` is set" rules below apply to it
too.) **`/lb/notify`**: when a fill is placed into the library, lb-bot POSTs
`{event, release_mbid, rgid, artist, album}` and the hub rebuilds it, field by
field, into a `library` broadcast (§5.4) to every connected device.
**`/lb/fill`**: when a fill's ledger row moves — any state transition, and transfer
progress throttled to ≥ 5 points or ≥ 10 s — lb-bot POSTs the `album/status` view
(minus `files[]`) with `kind: "album"` and `key: <rgid>`, or a gap summary with
`kind: "gap"` and `key: <group_id>`, or `{kind: "wishlist", key: <rgid>, state:
"landed"}` when a wishlist row's record arrives. The hub rebuilds it, typed and
bounded (strings ≤ 200, `ndAlbumIds` ≤ 16, unknown keys dropped, unknown kinds
`400`), into a **`fill` broadcast** — its own frame, deliberately: a `library`
event makes every client refetch its discographies and flushes the hub's library
caches, which is right for a landing and wrong for "3 of 12 downloaded". A fill
push flushes nothing except the cached `album/sources` ranking when the fill ended
(`failed` or `cancelled`), so "try another source" is not handed the peer that
just failed. Nothing is replayed on `welcome`: the push accelerates the poll
(§15, `/lb/fills`) and never replaces it. Both clients hand a `fill` frame and a
poll answer to the **same** apply function, so push cannot disagree with poll.
`/lb/notify` also drops the hub's cached `GET /lb/overrides` since 2026-10-12 (§15.5). The frame's typed
fields include the per-fill `leftOut` and `allFiles` (§15.5).

- Configured **on lb-bot**: `LB_BOT_HUB_URL` + `LB_BOT_HUB_TOKEN` (the same
  `HUB_TOKEN`). Unset on either side means no ping, never an error — the placement
  path never waits on it and never fails because of it.
- Answered whether or not `LBBOT_URL` is set, because nothing is forwarded. It is
  handled ahead of the proxy dispatch for exactly that reason: `LbProxy` would
  404 it against its outbound route table.
- Bearer-authed like every other route, so any device token can send one. That is
  fine: the frame carries no authority, it only tells clients to re-read what they
  can already read. One side effect to know: since 2026-09-24 an authenticated
  call to any of the three inbound routes also marks lb-bot **available** in the
  hub's verdict (only ever towards true — a push is proof lb-bot is up), so a
  device posting here can flip `welcome.lb.available` to true, and broadcast one
  `lb` frame, until the next 60 s probe says otherwise (§15.3).

**Three `library` events.** `albumPlaced` is sent at placement, *before* Navidrome's scan — a
client re-reading the library then mostly sees it as it was. `albumIndexed` is sent
by lb-bot's verifier once Navidrome's scan has finished and a placed track is found
in it (it polls every 2 s for the first minute, then every 30 s). Only that event carries:

- `ndAlbumIds` — the Navidrome album ids the matched tracks belong to (≤16 strings);
- `ndArtistId` — the matched song's Navidrome artist id;
- `row` — the release's updated discography row, in exactly the snake_case shape
  `GET /lb/artist/discography` returns (rebuilt field by field, bounded).

**The hub clears its cached lb-bot answers before broadcasting a `library` frame**
(the nine routes in `LB_LIBRARY_ROUTES`: discography, fresh-releases, status, the
two lookups' ownership-marked one, `artist/similar`, `artist/related`, the two
Deezer rows and the wishlist; the long-TTL MusicBrainz routes are left alone), and bumps
those routes' cache generations so a read already in flight cannot write its
pre-landing answer back. Generations are **per route**: an invalidation voids only
in-flight reads of the routes it names, and a read of any other route still caches. Without that, every client's re-read was served the hub's own 60 s old copy.

Clients: Feishin re-reads the discography on either event, re-reads its Navidrome
album/album-artist queries on `albumIndexed` (on `albumPlaced` only after a 20 s
fallback, for an lb-bot that never sends the second event). Navic's `SyncManager`
answers `albumIndexed` + `ndAlbumIds` with `DbRepository.syncAlbumsById` (one
`getAlbum` per id, straight into Room — it used to buy a full library pull behind a
90 s debounce), falls back to a bounded `newest` walk for events without ids, and
then bumps `libraryRevision`, which makes the artist page re-read Room and the
discography.

**`artistScanned`** carries only `ndArtistId` and `artist`: a discography scan
(`POST /lb/artist/discography`) finished or failed. It changes lb-bot's index and
nothing in Navidrome, so clients re-read that artist's discography and nothing else.
The discography read now carries the scan's outcome — `scan: {state: running|done|failed,
error, taskId, startedAt, finishedAt}` — because a scan MusicBrainz failed used to
store an **empty** discography over the real one and report success, which every
client rendered as "no releases". lb-bot now retries MusicBrainz, fails the scan
with its reason and keeps what it had; a client matches `scan.taskId` against the
id its POST returned and shows `error` verbatim. A POST while that artist's scan is
running answers the running task's id instead of starting a second walk.

**Not every landing is announced.** Anything placed without a tracked fill (lb-bot's
own page, files matched by hand) or while the hub was down reaches Navidrome with
no frame at all. Navic therefore also watches `getScanStatus` every 30 s and, when a
scan ends or its count moves, runs `DbRepository.syncChangedAlbums`: the album list
(a few paged requests) diffed against Room by track count and cover id, re-reading
only what differs. Id-less `library` frames take the same sweep, which unlike the
old `newest` walk also sees a gap fill — an album that is not new, only longer.

Without the ping nothing breaks. lb-bot marks its own index row `present` at
placement, so the next discography read on any client is already correct — the
broadcast only closes the window where a page is *already open* somewhere else.

### 15.2 The acquisition vocabulary — what a fill row says and offers

Both clients render every fill — the Download Center / `/downloads` row, the
sheet or modal footer, the tile badge — from this table, implemented as a pure
module in each (`FillVocabulary.kt`, `fill-vocabulary.ts`). There is no shared
build, so this table is the contract and a side-by-side walk is the check. The
ledger outcomes are `running`, `done`, `failed`, `needsPick`, `cancelled`,
`gaveUp` (Navic persists the last two as `needs_pick` / `gave_up`).

| state / outcome | Headline | Sub-line(s) | Progress | Buttons |
|---|---|---|---|---|
| running · `searching` (and `unknown` inside the grace) | Looking for a source | lb-bot's switch note, if any | indeterminate | Cancel |
| running · `queued` | Waiting for the peer | `@peer` | indeterminate | Cancel |
| running · `downloading` | Downloading {done} of {total} | `{MB} of {MB} · {MB/s} · @peer`, `{n} failed` when > 0 | determinate (`percent`) | Cancel |
| running · `placing` | Adding to the library | — | 100 | — |
| running · `placed` | Added — waiting for the library scan | — | 100 | Open album |
| done (`verified`) | In your library | — | — | Open album · Dismiss |
| done (`placed` + `verifyGaveUp`) | Added — Navidrome hasn't indexed it yet | — | — | Open album · Dismiss |
| failed · `needs_match` | Downloaded, but needs sorting out in lb-bot | `reason` | — | Dismiss |
| failed, by `failureKind` | `no_source` → Nobody is sharing this one · `format_rejected` → No copy in an allowed format · `transfer_failed` → The download itself failed · `placement_failed` → Downloaded, but couldn't be filed into the library · `mb_unavailable` → Downloaded — MusicBrainz wouldn't answer, so it couldn't be tagged · else → Couldn't get this one | `reason` verbatim · "Tried {n} times" when n > 1 · "Retrying automatically in {N}s" while `retryAt` is ahead · one explanatory line (below) | — | per the rules below · Dismiss |
| cancelled | Cancelled | — | — | Open album · Dismiss |
| gaveUp | Stopped tracking this one | the reason ("lb-bot no longer knows this download" / "No progress for 20 minutes") | — | Open album · Dismiss |
| needsPick (gap) | Waiting for you to pick a source | "Open the album to pick a source." | — | Dismiss |
| any running row, ≥ 2 unanswered polls | (unchanged) | Can't reach lb-bot — last checked {N}s ago | (unchanged) | (unchanged) |

Button rules, identical in both:

- **Cancel** iff not settled and `cancellable` (the server's field), or a failed row
  whose `retryAt` is ahead. Never on `placing` or `placed`.
- **Retry** iff outcome is `failed` and (`failureKind` is empty or `retryable`).
- **Try another source** iff outcome is `failed`, not a gap, and a peer is known to exclude.
- **Allow MP3 and retry** iff outcome is `failed` and `mp3WouldHelp` — through the
  review group when the row has one, else through `allowMp3` on the download. The
  retry runs only if the opt-in took.
- **Add to wishlist** iff outcome is `failed`, `failureKind` is `no_source` and the row
  has an `rgid`; on success the row is dismissed — the wishlist is where it lives now.
- **Open album** → the external album page, where "Download" lives; a cancel is
  started again from there, never with a Retry on the row.
- **Dismiss** iff settled.

Explanatory lines: "Asking again would hit the same problem — this one needs fixing
in lb-bot." when nothing above is offered; "Nobody had it this time. The swarm
changes — add it to the wishlist and lb-bot will keep looking every few hours."
above a wishlist button. Three ledger rules go with the table: a **failed poll is
not an answer** (the row keeps its state and gains the "can't reach" line;
`unknown` is never inferred from an error), `unknown` from lb-bot is **bounded
patience on the clock** (a 30 s grace after the tap, then two minutes continuous
before `gaveUp`), and **expiry is a settle, never a delete**, measured from the
row's last progress.

**A stalled placement is the `needs_match` row.** lb-bot
marks a gap whose tracks downloaded but were never filed into the album (no track
has entered `downloaded` for 30 min) as `status: "failed"` with
**`stalledPlacement: true`**, `failReason: "stalled_placement"` and a `failDetail`
sentence saying what to do; the flat fill summary carries them as `failureKind` and
`reason`, and the `/lb/gap` view as they are. Both clients map such a gap onto the
"failed · `needs_match`" row of the table above, whatever the `failureKind` rules
would otherwise say: `failDetail` is the sub-line (never the raw
`stalled_placement` token), Dismiss, **no Retry**. In the gap sheet or modal it
offers **no search, fetch, fill or auto action** (no "Search again", no "Fill
{n} tracks", no Auto / "Pick the best source for me"): the files are already here,
so a fresh source search would only fetch them again. The remedies are **Recheck**
(`POST /lb/gap/rescan`, for files that are in fact already in place) and **Open in
lb-bot** (`webUrl`), where the Reconcile and Pick-the-release actions live.
`stalledPlacement` absent means false.

**So is a pending match.** A gap at `status: "picking"`
with some track `downloaded` is lb-bot's `needs_match` bucket, not a picker waiting on
the user: once no search and no transfer is in flight, it settles into the same
"failed · `needs_match`" ledger row (never needsPick; Dismiss, no Retry). The gap sheet or
modal makes **Open in lb-bot** the prominent action, but — unlike a stalled placement —
still offers search, fetch and Auto for the tracks that are still missing. A `picking`
gap with no `downloaded` track is still needsPick. lb-bot ranks a failed or needs_match
bucket above in-flight transfers, so a gap can read `failed` or `picking` while tracks are
still `queued`/`downloading`: both clients hold the settle until the transfer ends.
**Announcing it:** a row that settles into needs_match —
stalled placement, pending match, or an album fill's own `needs_match` — is announced
"Downloaded {album} — needs sorting out in lb-bot", never "Couldn't get {album}"; the
link goes to lb-bot's `#/gaps/<groupId>` for a gap and to `#/downloads` for an album
fill (whose key at `needs_match` is an import-record id, not a review group).

**Gap track states.** A gap view's `tracks[].state` (lb-bot `_TRACK_STATE_BY_DECISION`)
is one of: `present` (in the library, not part of the gap), `missing` (not yet
picked), `picked` (approved, or picked and waiting on a source), `queued`,
`downloading`, `downloaded` (fetched, not yet filed — this is also lb-bot's
`needs_match`), `failed`, `cancelled`, `skipped`, `done` (placed or verified).
`cancelled` is the user's verdict, not a failure (it used to arrive as `failed`):
clients label it "Cancelled" and count it as finished for progress, but as neither
a success nor a failure.

**Progress counts** (one rule in both clients' gap sheet and Download Center row):
"{done} of {total}" and the percent count `downloaded`, `done`, `skipped` and `cancelled`
as finished; `total` is every track that isn't `present`; `failed` is counted apart; the
percent rounds half up.

### 15.3 The index mirror — `/lb/index/*`, `POST /lb/index`, and the `index` and `lb` frames

Every client keeps a **local copy of lb-bot's library index** (its `artists` and
`release_groups` tables: ~2.3k artists and ~68k rows on the library this was built
against) and keeps it current by pulling deltas. It exists so an artist page paints
its missing albums in the same frame as the owned ones, offline too, instead of
~10 s later behind a status probe and a discography read. lb-bot's side of the feed
(triggers, epoch, high-water mark) is documented in the
[lb-bot](https://github.com/illuminator69/lb-bot) repository's `CLAUDE.md`,
§ "The index change feed".

**Only lb-bot writes the index.** A client never writes a mirror row from a user
action: index, rescan and acquire go to lb-bot, and the mirror changes only when
the result comes back through the feed. Anything optimistic (a fill in flight)
stays in the fill ledgers of §15.1–15.2.

**The version is a sequence, not a clock.** lb-bot's SQLite triggers bump one
counter on every row written and stamp the new value on the one artist the row
belongs to (`seq`); a deleted artist leaves a tombstone with its own seq. Every
seq value belongs to exactly one key. The **epoch** is `"<WIRE_VERSION>-<16 hex>"`
and changes when seqs stop meaning what a cursor assumes — a new index DB, a
`WIRE_VERSION` bump (the shape or meaning of a wire row changed, including the
code-derived `effective_type`), or an index DB that went backwards (restored, or
lost commits to a power cut; lb-bot detects that with a high-water mark it writes
before any seq leaves the process). A client whose epoch differs throws its mirror
away.

#### `GET /lb/index/changes?since=<int>&epoch=<str>` → `GET /api/index/changes`

`since` defaults to 0; a non-integer or negative one is `400`. `epoch` is the
client's, or empty for "no check".

```jsonc
{ "epoch": "1-a1b2c3d4e5f6a7b8", "headSeq": 42, "scanVersion": 2, "ttlDays": 30.0,
  "items": [
    { "type": "artist", "key": "<artist mbid | nd:<navidrome id>>", "ndArtistId": "",
      "mbid": "", "name": "", "scannedAt": 0.0, "scanVersion": 0, "seq": 41,
      "rows": [ /* every release_groups row, in GET /lb/artist/discography's
                   snake_case row shape, ordered year, title -- unless
                   rowsTruncated, see below */ ] },
    { "type": "tombstone", "key": "nd:7", "seq": 42 } ],
  "nextSince": 42, "more": false, "artistCount": 2, "seqSum": 83 }
```

- `items` is every artist and tombstone with `seq > since`, in one stream
  **ordered by seq**, so a tombstone and the artist that supersedes it never arrive
  out of the order they happened in.
- A page is capped by **bytes**, not artists (lb-bot's `INDEX_CHANGES_MAX_BYTES`,
  2 MB, under the hub's 4 MB `PROXY_MAX_RESPONSE`), and always carries at least one
  item when any exist. `more` says items with a higher seq remain; `nextSince` is
  the last item's seq when `more`, else `headSeq`.
- **A single artist can itself be over the cap.** The "always at least one
  item" rule above otherwise had nothing to stop one prolific artist's own `rows`
  from exceeding `INDEX_CHANGES_MAX_BYTES`, and past that the hub's
  `PROXY_MAX_RESPONSE`, which no retry could ever clear. When that happens, `rows`
  is cut to fit the same page budget and the item additionally carries
  **`rowsTruncated: true`** and **`rowsTotal: <n>`** (the full, untruncated count).
  Both are absent on every normal-sized item. lb-bot logs a warning server-side
  when this fires; **neither client stores or renders these fields yet** — a
  truncated artist's mirror entry is silently incomplete there (missing its
  newest release-groups, since rows are cut in `ORDER BY year, title` order) until
  something re-fetches its full discography. That is a known, accepted gap on the
  client side, not something the feed hides.
- `artistCount` and `seqSum` (`COUNT(*)` and `SUM(seq)` over `artists`) are for the
  drift check below. Head, items, count and sum are one snapshot.
- **Nothing computed at read time is on the wire as a fact.** There is no `stale`:
  the envelope carries `scanVersion` and `ttlDays`, each artist its `scannedAt` and
  `scanVersion`, and a client computes
  `now − scannedAt > ttlDays·86400 || scanVersion ≠ envelope.scanVersion` itself,
  so it cannot drift while nothing is written. lb-bot's own background worker
  rescans an aged row somewhere between 1× and 1.25× the TTL (a stable per-key
  jitter, so one bulk build does not age out on one day), so a client may show a
  row stale for up to a quarter-TTL before its rescan arrives. That is expected,
  not drift.
- **Resync.** When `epoch` is non-empty and not the server's, or `since > headSeq`,
  the answer is `{ "resync": true, "epoch", "headSeq", "scanVersion", "ttlDays" }`
  with no items. The client clears its whole mirror, adopts the new epoch, sets its
  cursor to 0 and pulls again from `since=0`. This is the **only** answer that wipes
  a mirror.
- **`503 {"error": …}` from lb-bot** means it could not record its high-water mark
  (a read-only `/config`). Treat it like the hub's `503 busy`: back off, cursor
  untouched.

An artist with no scan behind it is still an artist on the feed: lb-bot gives any
release row it holds without a parent (a fill landing for an artist it never
scanned) a stub parent with `scannedAt: 0, scanVersion: 0`, which reads as stale
and is what its background worker rescans next.

#### `GET /lb/index/keys` → `GET /api/index/keys`

`{ "epoch", "headSeq", "keys": [ {"key", "seq"}, … ] }` — every artist, no
tombstones (a tombstoned key is simply absent). ~120 KB. Read only when the drift
check fails.

#### How the hub proxies them

Both routes are `cache: False` **and** `coalesce: False` (a spec key these two
introduced; absent means the usual sharing of identical in-flight `GET`s). A client
pulls because it was just told the head moved; a cached page, or an in-flight read
it joined that started before the write, answers with the *old* head and leaves
the client one push behind until the next trigger. An up-to-date pull is ~100 B, so
sharing buys nothing worth that. They run in their own one-slot **sync pool**
(`PROXY_SYNC_INFLIGHT = 1`): not the default pool, where a pull would queue behind
150 s source searches, and not the fast pool, where a client pulling its mirror
from zero — dozens of 2 MB pages back to back — would starve the status polls that
pool protects. A second device pulling at the same moment waits
`PROXY_QUEUE_TIMEOUT_FAST` for the slot and is then told `503 {"busy": true}`, and
backs off with its cursor untouched.

#### `POST /lb/index` — lb-bot's head moved (inbound)

lb-bot POSTs `{ "seq": <int ≥ 0>, "epoch": <non-empty string ≤ 64 chars> }` from one
dedicated sender thread: it polls its head every second, sends on the leading
edge at most once per 2 s (a trailing debounce would never fire during a steady
bulk build), retries until a 2xx, and sends once at boot even if nothing changed.
It is deliberately **not** `/lb/notify`: that clears every library cache on the
hub and makes both clients refetch their whole library, which during a bulk index
build would happen every 2 s — and its field whitelist would drop these fields
anyway.

- Same bearer check as `/lb/fill` (`401` on a bad token), exact-path match (so the
  proxied `/lb/index/changes` and `/keys` fall through), `405` on anything but
  POST, `400` on a malformed body (a `bool` is not a seq).
- The hub invalidates **only** `GET /lb/artist/discography` — the clients' network
  fallback when an artist is not mirrored — and, since 2026-10-04, `GET /lb/overrides`
  (its `toCheck` list depends on scan results, §15.4); never `LB_LIBRARY_ROUTES`. `index`
  is not a library event, any more than `fill` is. Because cache generations are
  per route (§15.1), a push voids only a discography read in flight: a 45–150 s
  `/lb/album/sources` or a six-hour MusicBrainz read that is upstream across a push
  still caches. (With the original proxy-wide generation, a build's push every 2 s
  stopped every read slower than 2 s from ever being cached.)
- It stores the hint (`lbIndex`, persisted in `state.json`; a new epoch is saved at
  once, same-epoch pushes at most every `LB_INDEX_SAVE_THROTTLE` = 10 s, and a
  graceful stop flushes the rest) and broadcasts
  `{ "t": "index", "seq", "epoch" }`, rebuilt from the validated hint.
- Like the other two inbound routes, the bearer check alone marks lb-bot available
  (below) — before the body is validated.

#### What `welcome` carries: `lb` and `lbIndex`

- **`lb: { available, routes, webUrl }`** is always present on a hub that has the
  mirror: the hub's own verdict on lb-bot, exactly the `routes` list `/lb/status`
  returns, and the `webUrl` link (§15), so a client can gate its lb-bot surfaces
  without first awaiting a `/lb/status` round trip. With `LBBOT_URL` unset it is
  `{ available: false, routes: [], webUrl: "" }` — and `/lb/status`'s
  `{configured: false}` answer carries the same `routes: []`, so the two never
  disagree.
- **`lbIndex: { seq, epoch }`** appears once the hub has heard a push, and survives
  a hub restart. It is **only a hint**: a client pulls on **every** `welcome`
  regardless, because an up-to-date pull costs ~100 B and a push missed while the
  hub was down would leave the hint stale. This is the "client closed for days"
  path.
- An **older hub** sends neither. A client then keeps probing `/lb/status` as
  before, and a `404` from `/lb/index/changes` means "this hub has no feed" —
  skip quietly until the next `welcome`, and serve every page from the network
  path.

Right after a hub restart, the first `welcome`s wait up to
`LB_PROBE_STARTUP_WAIT` (3 s) for the hub's first probe, so a client reconnecting
the instant the hub listens is not told a healthy lb-bot is down. A wait that
expires ends the waiting for everyone; the late verdict then arrives as an `lb`
frame.

#### The `lb` frame — the verdict changed

`{ "t": "lb", "available": bool, "routes": [...], "webUrl": "..." }`, the same
object as `welcome.lb`, broadcast **only when the hub's verdict changes**; an unchanged
verdict sends nothing. Without it `welcome.lb` would be a connect-time snapshot,
and a client that trusts it (and so stops probing `/lb/status`) would never notice
lb-bot coming up or going away while it stays connected. Clients apply it exactly
like `welcome.lb`. The verdict moves from **three** sources, all through one
function (`LbProxy._set_available`):

1. **The hub's own probe**, at start and every `LB_PROBE_INTERVAL` (60 s): the
   `/lb/status` upstream with its 404 fallback, but with no pool slot and no cache
   (through the default pool it would queue behind four source searches and a
   probe that cannot tell busy from down would call lb-bot down), and
   `LB_PROBE_TIMEOUT` = 4 s on `/api/health` so an lb-bot that *hangs* reads as
   down in seconds. The `/api/summary` fallback keeps the route's own timeout: on
   an older lb-bot it is a genuinely heavy read.
2. **An authenticated inbound call** — `/lb/notify`, `/lb/fill` or `/lb/index`.
   This only ever moves the verdict to *true*; the probe is what notices lb-bot
   going away. The token is the shared `HUB_TOKEN`, so **any holder of it** (every
   device) can flip `available` to true, and broadcast one `lb` frame, by posting
   to an inbound route; the next probe corrects it within 60 s. Harmless — the
   frame grants nothing a device could not already read — but know it when a log
   says lb-bot "came back" while it was down.
3. **A client's `/lb/status` read** that went upstream (not a cache hit).

So a flapping lb-bot can produce two `lb` frames close together (a probe saying
down, then a push or a client read saying up). They are built at the moment of
the change and arrive in order.

#### How a client applies the feed

- **When to pull:** on **every** `welcome`, on **every** `index` frame, and every
  15 minutes as the backstop. One sync in flight at a time; a trigger arriving
  during one queues one follow-up.
- **Apply a page in one local transaction.** Per artist, replace *all* its rows at
  once, so a client never holds a half-updated artist — and only if the item's
  `seq` is greater than the seq held locally for that key, which makes duplicates
  and out-of-order delivery harmless. A tombstone deletes the key only if its `seq`
  is greater than the local one. After the commit, `cursor = max(cursor,
  nextSince)`; loop while `more`. A page that says `more` but does not move the
  cursor ends the pass.
- **Failure:** on `503` (busy), `502`/`504` or a network error, back off and leave
  the cursor and the rows exactly as they were.
- **Drift check**, once per pass after the final page: compare the local artist
  count and seq sum with that page's `artistCount`/`seqSum` (the final page's
  snapshot, so a bulk build running meanwhile does not cause repair loops). On a
  mismatch fetch `/lb/index/keys`, delete local keys the server no longer lists,
  and for keys whose server seq differs from the local one (or that are missing
  locally) rewind `cursor` to `min(their server seqs) − 1` and pull again — at most
  once per sync. There is no per-key fetch route, so the repair re-reads everything
  above that point; on a large mirror one drifting low-seq key means most of the
  index again. **Never wipe for drift.**
- **Lookup by Navidrome id** must reproduce lb-bot's `_index_get_artist` exactly:
  the mbid key first when the caller has an mbid; otherwise the rows where
  `key == "nd:" + id` or `ndArtistId == id`, ordered `scannedAt` descending then
  `key` (binary order). That is what makes the brief two-row state during an
  `nd:` → mbid key swap resolve the same way on every client and on the server.
  An empty id must match nothing.
- `stale` is computed on the client (above). A mirror row carries no `scan` record:
  rescan progress and a failed scan's reason still come from
  `GET /lb/artist/discography` and the `artistScanned` library event.
- **The mirror is local data and renders whatever `available` says** — offline, or
  with lb-bot down, an artist page still shows what the last pull knew. Only a
  disabled or unconfigured hub means "forget what the hub said".
- An artist the mirror does not hold falls back to `GET /lb/artist/discography`,
  as it did before the mirror existed.

### 15.4 Artist MBID overrides and the listening mirror

Added 2026-10-04; route rows are in the §15 table.

**Artist MBID overrides.** A library artist's MusicBrainz id normally comes from Navidrome's file
tags, or from lb-bot's own name-search guess when there is no tag. An override is the user's pick, stored in
lb-bot (`artist_overrides`) and followed by every consumer there. `GET /lb/overrides` answers
`{"artists": [{ndArtistId, mbid, name, tagMbid, setAt}], "toCheck": [{ndArtistId, name, kind, mbid, albums}]}`.
`toCheck` lists untagged album artists whose guess matched none of their owned albums (`kind: "unmatched"`)
or that have no MBID at all (`"unresolved"`); it is **absent**, not empty, from an older lb-bot and whenever
Navidrome's artist list is unavailable, and a client then shows no list. The client rule is
`effectiveArtistMbid(ndId, tagMbid) = overrides[ndId]?.mbid ?? tagMbid`, used wherever an artist MBID feeds
an lb-bot call.

A set carries a bare UUID (a URL is a 400) and answers `{ok, mbid, name, task_id}`; the answered `mbid` is
the one to use (MusicBrainz merges artists). lb-bot starts the discography scan itself.
**`task_id: ""` means lb-bot deliberately started no scan, and the client must not start one**: it answers
that when the MBID's discography is already held by another live library artist (a scan from this artist
would re-file that row against the wrong albums), when a remove leaves nothing resolvable, and for an artist
Navidrome no longer lists. `POST /lb/artist/discography` applies the same check **for an artist that has an
override** and answers `task_id: ""` with `covered: true`; only `covered: true` supports the sentence "this
discography is shared", and a blank `task_id` without it says only that lb-bot did not start the scan.
Errors pass through the hub unchanged: 400; 404 `code: "unknown_mbid"` /
`"no_override"`; 409 `code: "busy"` (a fill for that artist is in flight, including a placed fill not yet
verified, so up to about 11 minutes); 502 `retryable: true` (MusicBrainz or Navidrome's artist list
unavailable — nothing stored).

**Listening.** lb-bot mirrors the configured user's ListenBrainz listens into its own SQLite file
(`listens.db`) with one background writer: a first backfill (paged, resumable across a restart), a pull
every 10 minutes, and a daily catch-up of the trailing 14 days for listens submitted late. The routes only
read. `period` is one of `7d`, `30d`, `90d`, `365d`, `all`; `tz` is an IANA zone name from the client
(lb-bot's container runs in UTC). `status.state` is `off` (no ListenBrainz user), `importing`, `ready` or
`error` (the last sync failed; stored rows are still served). `owned` on a listening row means the library
holds the album: lb-bot's index says so, or Navidrome's album list has it by release MBID or by the same
artist and title. A route that cannot decide ownership answers 503 rather than a 200 that reads as
"nothing".

**Why `summary` and `unowned` are neither cached nor coalesced.** The hub cannot know when lb-bot's mirror
changes. A cached or joined answer taken mid-import was handed to a client that re-read at the moment the
import ended, which then showed partial totals as final. lb-bot keeps its own consistent memo of the
aggregation and joins ownership per answer, so the hub's copy bought nothing. `status` and `recent` stay
coalesced (two clients' polls share one fast-pool slot).

**Fresh rider.** Rows of `GET /lb/fresh-releases` gain `artistPlays` (int): all-time plays of the row's
artists in the listens mirror, 0 when none or while the mirror is off or importing. The route is still
cached 60 s, so the field can lag the mirror by that long. A client offers its "Artists you play" scope only
when some row has `artistPlays > 0`: the wire cannot tell "none of your artists released" from "the mirror
is off".

**Version skew.** An lb-bot older than the hub answers the new GETs `404` with no `code` and the new POSTs
`405` (its catch-all route is GET-only); the hub relays both, caches neither and invalidates nothing.
A client treats a code-less 404 or a 405 as "this lb-bot is older": a screen opens to one plain sentence, a
control inside a page is hidden after the first such answer. A newer lb-bot behind an older hub is simply
not advertised, and the surfaces stay hidden. A hub timeout on a write is `504 {"busy": true}` and does not
say whether lb-bot stored it; the overrides map heals on the next `index` frame.

**Refresh About.** `GET /lb/meta/artist|album` with `refresh=1` bypasses the hub's cache and coalescing for
that read and stores the fresh answer under the ordinary key. lb-bot answers 502 `retryable` and keeps its
stored entry when a source did not answer; `found: false` is stored and answered only when the sources
answered and had nothing.

### 15.5 Album release override, cover, artist name merge, and "download all files"

Added 2026-10-12; route rows are in the §15 table. The wire is **additive**:
an older lb-bot answers the new GETs `404` with no `code` and the new POSTs `405`, and a client treats that as
§15.4 does ("this lb-bot is older": a screen says so in one muted sentence, a control inside a page hides after
the first such answer, never an error state). A `404` that carries a `code` is lb-bot's own answer. Every error
body has an `error` string a client may show as is. Request keys are snake_case, response fields camelCase; ids
are `album_id` / `albumId` (a **Navidrome album id**), `nd_id` / `ndArtistId` (a Navidrome artist id),
`release_mbid`, `rgid`, `mbid` (bare UUIDs; a URL is a 400) and `id` (an override or merge id lb-bot mints,
opaque). No filesystem path is ever on the wire: files are named by title, albums by artist and title.
Clients gate the album surface on the advertised string `POST /lb/album/override` and the merge surface on
`POST /lb/artist/merge`.

**What it is.** *Album override:* for an album the library owns, the user picks which MusicBrainz release
it is. lb-bot **retags the album's files** to that release (release id, disc and track numbers and totals,
titles, date: tags only, never a rename or a move), keeps the previous tags so the change can be undone, and can
save a chosen Cover Art Archive image as the album's `cover.jpg`. *Name merge:* Navidrome keys artists by
**name**, so "Belle & Sebastian" and "Belle and Sebastian" are two artists even with one MusicBrainz id; lb-bot
retags the variant spelling's files to the kept name (undoable) when both entries resolve to the same
MusicBrainz artist. State lives in lb-bot (`library_index.db`) and is reached through the proxy.

**Navidrome must report real paths.** Navidrome hands a Subsonic client a *synthesized* path per song unless
"Report Real Path" is on for that client's player. `preview`, `apply`, `cover` and `remove` (and the merge's
preview and apply) answer **409 `{"code":"files_unreachable","reachable":n,"total":m}`** when lb-bot cannot find
every one of the album's files inside its own music folder; the `error` text names the remedy (turn the setting
on for the lb-bot player in Navidrome's Players settings). All or nothing: a retag of part of an album would
split it in two. A client shows lb-bot's `error` text as for any code it does not know.

#### Album override

```
AlbumState = {albumId, artist, album, releaseMbid ("" when untagged), rgid ("" when lb-bot cannot tell),
              tracks: [{disc, track, title, duration}] (as tagged, ordered), override: null | OverrideRow}
OverrideRow = {id, state: "applying"|"applied"|"failed"|"removing", releaseMbid, prevReleaseMbid ("" when the
              album was untagged), rgid, artist, album, albumId (the album's CURRENT id, "" while Navidrome
              has not indexed the retagged album), coverUrl ("" when none was chosen), setAt (epoch s),
              done, total (files written / to write), failures: [{title, reason}]}
Cover   = {thumb, full, releaseMbid ("" for a release-group image), source: "release"|"release-group"}
Preview = {albumId, releaseMbid, release: {title, disambiguation, year, country, format, trackCount, discCount},
           files: [{from: {disc, track, title}, to: {disc, track, title} | null, basis, changed}],
           matched, unmatched, missing: [{disc, track, title}]}
```

| Route | In | 200 body |
|---|---|---|
| `GET /lb/album/override` | `album_id` **or** `id` | `AlbumState` |
| `GET /lb/album/covers` | `rgid`, `release_mbid` (either or both) | `{"covers": [Cover]}` |
| `POST /lb/album/override/preview` | `album_id`, `release_mbid` | `Preview` |
| `POST /lb/album/override` | `album_id`, `release_mbid`, `cover_url`?, `force`? | `{ok, id, state: "applying"}` |
| `POST /lb/album/cover` | `album_id`, `cover_url` | `{ok, id}` |
| `POST /lb/album/override/remove` | `id` | `{ok, id, state: "removing"}` |

- **`GET album/override`**: with `album_id` it describes that album and carries the override row whose album it
  is; with `id` it describes the row's album, which is what a client **polls** after apply or remove, because
  the album id it started from is dead. 404 `unknown_album` when Navidrome has no such album; 404
  `no_override` for an unknown `id`: **after a remove finishes the row is gone, so this 404 is how a poll learns
  the remove completed.** `rgid` may be `""` (an untagged album of an artist lb-bot has not indexed); the client
  then resolves a release group through `GET /lb/album/lookup` (answer validated against artist and title;
  declining is fine) before listing editions with `GET /lb/album/releases?rgid=` and
  `GET /lb/album/tracklist?release_mbid=`, which are unchanged. A pasted release MBID or a
  `musicbrainz.org/release/<uuid>` URL is accepted by the picker (the client extracts the UUID).
- **`covers`**: the chosen release's own front first, then the release group's fronts, de-duplicated, at most
  12. `[]` means the Archive has none; 502 `retryable` that it did not answer. `thumb` / `full` are public Cover
  Art Archive URLs the client loads directly. For a release that is not one of the listed editions (a pasted id)
  a client asks with `release_mbid` alone; lb-bot then derives that release's own group. `cover_url` on apply or
  cover must be one of the `full` URLs `covers` answered (lb-bot fetches only from `coverartarchive.org` /
  `archive.org`; anything else is a 400).
- **`preview` writes nothing.** `basis` says how a file was matched to its slot: `mbid` (recording id tag),
  `exact` / `prefix` / `contained` / `fuzzy` (title), `duration`, or `""` for a file with `to: null`. `changed`
  is false when the file already carries exactly the slot's numbers, title and release id. A file with no slot
  is **left untouched** by an apply; Navidrome will list it as an album of its own, and the client says so
  before the user applies.
- **`apply`** answers at once and works in the background. Applying to an album that already has an override
  replaces the release on the **same row**; the undo record keeps the tags from before the first apply.
  `applied` with a non-empty `failures` is a partial result. A job that wrote **nothing** (`done == 0` after its
  last write) ends `failed` at once. **`cover`** changes the cover alone (the release stays), creates or updates
  the row, so the change is listed and undoable like any other; it is synchronous. **`remove`** restores every
  tag it changed and the previous cover (or deletes the `cover.jpg` it wrote when there was none), in the
  background; a remove that restored some files but not all ends `failed` after the same closing steps.
- **`applying` becomes `applied`** once every file is written **and** Navidrome has indexed the album again, or
  60 seconds after the last write, whichever comes first; so usually `applied` already carries the new
  `albumId`. While lb-bot waits for the index, `done == total` and the state is still `applying`. A client must
  still handle `applied` with `albumId` `""` (the 60 s ran out). A remove's row disappears (404 `no_override`)
  on the same condition. lb-bot then sends the ordinary **library notification** (`/lb/notify`, the `library`
  frame); there is no new frame.
- **Errors**: 400 missing or malformed id / MBID / `cover_url`; 404 `unknown_album`, `no_override`,
  `unknown_release` (MusicBrainz has no such release); 409 `busy` (a fill for the album is in flight); 409
  `in_progress` (an apply or remove is already running for it); 409 `poor_match` with `matched` and `total`
  (fewer than half the album's files have a slot on the chosen release and `force` was not `true`: the client
  asks for a confirmation that names both numbers and resends with `"force": true`); 409 `files_unreachable`;
  502 `retryable` (MusicBrainz or Navidrome did not answer, nothing written). `force` is a **JSON boolean**; the
  hub forwards body values untouched.
- **Restart.** lb-bot restarted mid-apply: the row reads `failed` ("interrupted") and `remove` restores what was
  written.

#### Artist name merge

```
MergePreview = {into: {ndArtistId, name}, from: {ndArtistId, name}, mbid, albums, files,
                sample: [{album, tracks}] (at most 10 albums)}
MergeRow     = {id, state: "merging"|"merged"|"failed"|"undoing", into: {ndArtistId, name},
                from: {ndArtistId, name}, mbid, done, total, failures: [{album, title, reason}], setAt}
```

| Route | In | 200 body |
|---|---|---|
| `GET /lb/artist/merge/preview` | `nd_id`, `from_nd_id` | `MergePreview` |
| `POST /lb/artist/merge` | `nd_id`, `from_nd_id` | `{ok, id, state: "merging"}` |
| `GET /lb/artist/merge` | `id` | `MergeRow` |
| `POST /lb/artist/merge/undo` | `id` | `{ok, id, state: "undoing"}` |

`nd_id` is the entry whose **name is kept**; `from_nd_id` the spelling that is rewritten. (`from.ndArtistId` is
the rewritten spelling's id at merge time, dead once Navidrome re-indexes; it lets a second device tell which
local artist to re-read, and a client may ignore it.) lb-bot merges only two library artists whose **effective**
MusicBrainz id (the user's override first, the tag second) is the same and not empty, so an artist override is
how two differently-tagged entries become mergeable. A merge is a **substitution of values and nothing else**:
in every key the merge reads (canonical and alias, album-artist and track-artist) each value that folds equal to
the `from` name becomes the kept name; other values and their order stay; no key is added or deleted. Folders
are not renamed; what changes is what Navidrome lists, and the library notification follows. `merging` becomes
`merged` on the same rule as `applying` (writes done and a scan finished, or 60 s); an undo's row disappears
(404 `no_merge`) likewise, and the poll ends on that 404.

Errors: 400; 404 `unknown_artist` (Navidrome lists neither), `no_merge`; 409 `different_artists` (the effective
MBIDs differ or are empty), 409 **`collaboration`** (the two entries are not in one merge set, below), `busy`
(a fill for either is in flight), `in_progress`, `files_unreachable` (the `from` artist's albums); 502
`retryable`. A job that wrote no file ends `failed` at once.

#### `GET /lb/overrides` gained three fields

```
"albums":   [OverrideRow]   every album override, newest first
"merges":   [MergeRow]      every merge that can still be undone, newest first
"siblings": [{"mbid", "artists": [{ndArtistId, name, albums, collab}]}]
```

**Absent is not empty.** Each of the three is absent when lb-bot could not work it out (a client reads a missing
field as "no list") and `[]` when there is nothing; an older lb-bot sends none. `artists` and `toCheck` are
unchanged (§15.4). A **`siblings` group is a merge set**: two or more library artists with the same effective
MBID that can be merged into one another. Names that fold alike are always in one set; an entry that is a
*collaboration credit* of another same-MBID entry (its name splits into several acts and one act is the other's
whole name: "JPEGMAFIA × Danny Brown" beside "JPEGMAFIA"; "and", "×", "&", "、", "と" and a leading "The" are
read as joiners) forms its own set per fold-equal name, and among the non-credits a name of several acts is
never in one set with a name of one act. An artist id is in at most one group, but **two groups may carry the
same `mbid`**, so a client finds a group by the artist id it contains, never by `mbid`. Within a group the entry
with the most albums comes first. `collab` is absent (false) on every listed entry of a current lb-bot and is
kept for a build before this rule; a client offers "Merge into one name" only between entries that are not
`collab`. The `409 collaboration` refusal uses the same definition and also asks the two names directly.

**How the hub treats them.** `GET /lb/album/override` and `GET /lb/artist/merge` are polled, so they are never
cached; the hub does flush its `GET /lb/overrides` copy on **every** answer lb-bot gives to either (200 and 404
alike: a 404 `no_override` / `no_merge` is how a finished remove or undo is seen), but not on a 5xx or the hub's
own error. So the list a client reads when its job ends is read fresh, including after a `failed` end that no
notice announces. `GET /lb/overrides` is also flushed by `/lb/notify` (the `library` frame), by `/lb/index`
(§15.3) and by any 2xx of the five mutating POSTs (`album/override`, `album/cover`, `album/override/remove`,
`artist/merge`, `artist/merge/undo`; they also flush `GET /lb/artist/discography`); `preview` flushes nothing.
`GET /lb/album/override` takes the fast pool only when the request carries `id` (a status poll); with `album_id`
it is an album read from Navidrome that can cost a MusicBrainz request and shares the default pool.
`GET /lb/artist/merge` is on the fast pool. Both polls keep the default timeout; every other new route takes
`PROXY_SLOW_TIMEOUT` (45 s), the two undo POSTs and `POST /lb/artist/merge` included: lb-bot plans the whole merge, or stats every file on record
before an undo, before it answers, and a hub 504 would leave a running job that no client follows.
Error statuses and bodies (400/404/409/502 with `code`, `matched`, `total`, `reachable`) pass through unchanged. While a client follows a job, other reads of `/lb/overrides` therefore go upstream instead of to the
60 s copy; that is the stated cost.

**`covered` beside a blank `task_id`.** `POST /lb/artist/override` and `.../remove` (§15.4) answer
`"covered": true` beside a blank `task_id` when no scan was started because the MusicBrainz artist the library
artist now resolves to is a row another live library artist holds. It is absent in every other case. A client
may say "shares its discography with another artist" of a blank `task_id` **only** when it reads this.

#### "Download all files in this folder"

A whole-album download used to ask a source folder for every file in it. lb-bot now asks a folder **only for the
release's tracks when the folder has them all**, and reports when a source has more.

- **`GET /lb/album/sources`**: each source row may carry **`extraFiles`** (integer): the audio files lb-bot has
  seen in that folder that a plain download will not ask for. It is `0` unless every track of the release is
  paired with a file of the folder, then the seen files less the paired ones. **Absent** when the tracklist is
  not known; a client reads absent as "nothing to say". It counts the files the search returned, which can be
  fewer than the folder holds. `fileCount`, `coverage`, `coverageFull`, `coverageDetail` and `files` are
  unchanged. A client computes nothing from `files` or `fileCount` itself.
- **`POST /lb/album/download`** body gains **`allFiles`** (boolean, default false): ask the folder I picked for
  every audio file in it. Only with `sourceUsername` (a click on a source); `allFiles: true` without it is a
  400 `{"error": "allFiles needs a picked source (sourceUsername)"}` and nothing starts. It is consent for **that
  folder only**: after a switch to another source the new source is asked for the release's tracks only. What is
  *filed* does not change: the import files an extra only when the album was shown and the file's own tags name
  the album.
- **The answer echoes `allFiles`** (boolean) on every 200 of this route: for a new fill what was asked; for
  `existing: true` (a fill already running) what **that** fill was started with. An older hub drops the key from
  the body without a word, so the echo is how a client knows: it asked for all and reads `allFiles` false or
  absent, so it says lb-bot is downloading the album's tracks only and never claims "all files".
- **The fill view** (`GET /lb/album/status`, each album in `GET /lb/fills`, and the pushed `fill` frame below)
  gains `allFiles` (the fill's current source was asked for every audio file at the user's request; false after
  a switch) and `leftOut` (integer: audio files of the current source's folder, the peer's full listing read at
  the download, that were not asked for; `0` when the folder was asked whole). `total` stays "files asked
  for". Old ledger rows read `false` / `0`.
- **The pushed frame.** `_lb_fill_frame` (§15.1) rebuilds a fill push field by field, so a field reaches a client
  in a frame only if it is listed: `leftOut` is in the integer fields and `allFiles` in the boolean ones
  (2026-10-12; before that they arrived on polls only). Semantics unchanged: a key the push does not carry is
  **omitted** from the frame (absent is not `0` or `false`), and a non-numeric `leftOut` is dropped like the other
  integers.
- The client rows: with `extraFiles > 0` the row keeps its primary download ("the album's tracks") and adds a
  quiet "N more files in this folder" (singular "1 more file") and a second, visibly secondary action "Download
  all M files" (`M = fileCount`) that sends the same request plus `allFiles: true`. Where a client already shows
  a status line for a running or finished album download, `leftOut > 0` adds "N other files in the folder were
  left out".

#### What both clients do (there is no shared build; this is the contract)

"Fix album match" (entry points: the owned album's page or menu, and a "Wrong edition?" link on the gap-fill
surface; only when `POST /lb/album/override` is advertised): open reads `GET /lb/album/override?album_id=` and
shows what the album is tagged as now and, when `override` is set, that lb-bot changed it, with **Undo**; picking
an edition runs `preview` and shows the release line, a from-to table (unchanged rows collapsed or dimmed) and
three counts (files that change, files with no slot, tracks still missing); the cover step is a grid from
`covers` with **"Keep the current cover" as the default**, and is skipped when there are no candidates; apply,
poll, then on `applied` say what was written and leave the dead album id (open `albumId` when non-empty, else the
artist page). The one fixed sentence before apply: "lb-bot rewrites the tags of N files and keeps the previous
ones, so this can be undone." Every sentence states only what lb-bot answered; **a client never says whether a
favourite, rating or play count survives** (not measured for this route). "Merge into one name", inside the
artist match surface, is shown when `siblings` has a group containing the artist and `POST /lb/artist/merge` is
advertised: the other spellings with their album counts, the kept name defaulting to the one with the most
albums, `merge/preview` giving "N files in M albums", apply, poll, done. The "lb-bot data" panel lists `albums`
and `merges` beside the artist overrides, each with Undo.

- A remove or undo that fails at once is seen: lb-bot flips the row to `removing` / `undoing` before it
  answers and can reach `failed` before the first poll, so the poll starts with "the active state has been seen"
  taken from the POST's own `state`; a row already `failed` at the first tick ends the job as failed.
- The give-up clock measures silence, not length: the three-minute bound restarts whenever the row's
  `state` or `done` differs from the previous tick (a poll every 2 s). A job that advances never times out.
- A job that timed out is not settled later; the surface says lb-bot is still working and that "lb-bot
  data" in Settings shows the result. A job that ended and was dismissed is remembered (`id@setAt`) and not
  followed again in that process.
- The wait for Navidrome reads as a wait: the writes are over when `total > 0 && done + failures.length
  >= total` while the state is still an active one; then the surface says the tags are written and lb-bot is
  waiting for Navidrome to index them (up to a minute), with no determinate bar.
- A client offers "Merge into one name" only between entries that are not `collab`.
- A preview refusal of any kind offers Try again, except "this lb-bot is older"; inside the merge block
  an older lb-bot hides the block.
- Undoing a cover-only change (`releaseMbid == prevReleaseMbid`, non-blank) stays on the album page; its
  id did not change.
- Covers for a release that is not a listed edition are asked with `release_mbid` alone.
- The result sentence counts problems, not files: a cover that could not be saved is a `failures` entry
  titled "Cover image"; say how many files were written, then "N problems" above the list.
- The override lists are re-read on the hub's `library` frame as well as on `index`.
- The dead id is the client's to forget: when a job ends and the album's id changed, a client keeping albums in
  a local store removes the dead id's row itself; the job outlives the surface that started it.

**Not measured, so not claimed:** what an album retag does to a Navidrome favourite, rating or play count. A
move with unchanged tags keeps track and album ids; a retag to another release changes the album id.

---

## 16. Preview sidecar (`/preview/*` + `ext:` track ids)

"What does this album I don't own sound like." A track the library does not have,
playable in the ordinary queue, alongside tracks it does.

### 16.1 The hub does not stream, and that is the decision

The obvious design is a third `PROXIES` entry relaying the audio. It is not
implementable on this transport, and the reasons are structural rather than
stylistic — they are written down here because this is exactly the kind of
decision that gets re-litigated in six months by someone who sees a proxy and
wonders why the media does not go through it.

- `HttpProxy.handle` terminates in `_http_response`, whose body is a single
  `bytes`. `process_request`'s return is coerced through `AbortHandshake` into a
  buffered body with a `Content-Length` computed by the library
  (`websockets/legacy/server.py:233`). There is no chunk-by-chunk path.
- `PROXY_MAX_RESPONSE` is 4 MB. A three-minute preview is larger — measured at
  11.9 MB for one four-minute track.
- `PROXY_MAX_INFLIGHT` is 4 slots per proxy, so one playing stream would hold a
  quarter of the preview proxy's for its whole duration.
  One playing stream would hold a quarter of them for its whole duration.
- Serving media on the WebSocket port at all would mean writing to
  `self.transport` by hand and raising `BrokenPipeError` to suppress the
  library's own write — and even then `open_timeout` (the handshake deadline,
  which the proxies answer from inside) truncates the
  body mid-stream. That is the exact bug class the slow lb-bot routes were just
  bitten by.

So the **sidecar is the media origin** and the hub keeps only the control plane,
which is ordinary buffered JSON and fits `HttpProxy` perfectly. The sidecar had
to be a separate process regardless — an extractor breaks when a site changes,
and that must not be able to take the session relay down — so serving its own
bytes costs nothing extra and deletes the whole problem.

```
client ──/preview/resolve──▶ hub (HttpProxy, buffered JSON) ──▶ sidecar /resolve
client ──────────────────── streamUrl ───────────────────────▶ sidecar /stream  (Range, 206)
```

### 16.2 `ext:` track ids

`ext:<provider>:<providerId>` — e.g. `ext:yt:1uYWYWPc9HU`. It extends the
`mb:<mbid>` convention lb-bot already uses for artists and albums. **Nothing
else in the stack may mint an `ext:` id.**

### 16.3 `GET /preview/resolve` → `GET /resolve`

Params: `artist`, `title`, `album`, `durationMs` (optional; used to *reject* a
bad match, not merely to rank one). Cached 6 h, `PROXY_SLOW_TIMEOUT`.

The answer is **queue-track-shaped**, because that is what both clients and the
hub already pass around:

```jsonc
{
  "id": "ext:yt:<vid>", "title": "...", "artist": "...", "album": "...",
  "durationMs": 213000, "imageUrl": "https://...",
  "streamUrl": "<PREVIEW_PUBLIC_URL>/stream?id=ext:yt:<vid>&exp=<unix>&sig=<hmac>",
  "mime": "audio/webm",
  "provider": "yt", "confidence": 0.0-1.0
}
```

**That shape needs no hub change at all**, which is the point of choosing it: the
hub's queue is opaque passthrough, and `SQ_TRACK_FIELDS` already whitelists
exactly `id, serverId, title, artist, album, durationMs, coverArtId, imageUrl,
streamUrl, mime`. An `ext:` track therefore survives saved-queue sanitisation,
`syncSavedQueues` and a state reload **unchanged** — so transfer, the device
picker and Continue Listening work with no protocol work.

**`{}` at HTTP 200 means "no preview found"**, a legitimate answer and never an
error — the same rule as lb-bot's `strict=False` metadata chain. Clients render
it as "no preview", not as a failure. A wrong preview is worse than none, so the
sidecar refuses a candidate whose length differs by more than
`PREVIEW_DURATION_TOLERANCE_MS` whatever its title claims (that is an album rip
or a different recording), and one scoring below `PREVIEW_MIN_CONFIDENCE`.

**`mime` is resolved, not assumed.** Both clients hand it to their Cast
`MediaItemConverter` and to their player, so a declared type that does not match
the bytes `/stream` will serve is a track that loads and then never plays. The
sidecar therefore extracts the format during `/resolve` and reports what it will
actually send — including `video/mp4` when no audio-only stream is on offer,
which is currently the common case on YouTube without a PO token (measured
2026-09-23: exactly one format, a muxed 360p MP4). A muxed container plays; one
mislabelled as audio does not.

### 16.4 `GET /preview/status`

The liveness probe, mirroring `/lb/status`:
`{configured, upstreamReachable, previewCastable, extractorBlocked, cookies}`.

`previewCastable` is the **hub's** answer, not the sidecar's — only the hub knows
whether it was given a publicly reachable address to sign stream URLs against.

`extractorBlocked` and `cookies` are the **sidecar's**, passed through. They
exist because "the extractor is being refused" and "nothing matched" are
otherwise the same thing to a client — both an empty resolve against a process
that reports itself healthy — so the whole feature can be dead while every probe
says fine. The sidecar additionally reports `consecutiveFailures` and
`lastExtractorError` on its own `/status`; the hub does **not** relay those, as
they are operator diagnostics rather than anything a client renders.

Both fields are additive: a client that does not read them behaves exactly as
before.

### 16.4.1 Cookies

Some egress addresses get *"Sign in to confirm you're not a bot"* from the
extractor. Measured 2026-09-23 on one host, in one second: the same code passed
over IPv6 and was challenged over IPv4. Where the address is challenged and the
network cannot be changed, `PREVIEW_COOKIES` points the sidecar at a
Netscape-format `cookies.txt` and yt-dlp authenticates with it.

Three things about it are load-bearing:

- **Use a throwaway account.** The file is bearer access to the account that
  exported it, it is stored unencrypted, and yt-dlp traffic can get an account
  rate-limited or terminated.
- **The player client changes when cookies are configured** — `web`/`mweb`
  instead of `android`. yt-dlp's guidance is not to send account cookies with the
  `android` client, and that pairing is the one most associated with accounts
  being limited. Without cookies `android` is still right, because `web`
  unauthenticated needs a challenge the sidecar deliberately does not solve.
- **Cookies expire**, which is the whole reason `extractorBlocked` exists. A
  missing file (a wrong bind mount) degrades to running without cookies and is
  logged and reported, rather than raising.

### 16.5 The stream URL is a capability, and the hub mints it

A Chromecast fetches `streamUrl` itself and sends no headers, so the credential
has to be in the URL — and `HUB_TOKEN` must never be, since it is the hub's
entire administrative surface. So there is a second secret, `PREVIEW_SECRET`,
shared by the two processes and **not** `HUB_TOKEN`. It authorises exactly one
thing: reading one `ext:` id's audio until `exp`.

```
sig = HMAC-SHA256(PREVIEW_SECRET, f"{id}\n{exp}")      # hex
exp = now + PREVIEW_TTL                                 # default 6 h
```

The newline matters: concatenated, `ext:yt:a` + `1` and `ext:yt:a1` + `` would
sign the same bytes. The sidecar verifies with `hmac.compare_digest`, checks the
signature **before** `exp` (so an unsigned request costs no clock read), and
refuses an expired one with 403.

**The sidecar returns `streamUrl` empty and the hub fills it in.** This is the
one place a proxy in `hub.py` transforms a body rather than relaying it, and the
reason is that `PREVIEW_SECRET` then never has to leave the two servers. It
happens *after* the cache, deliberately: a resolution is cached for six hours
because an `ext:` id is stable, but a capability is only good for
`PREVIEW_TTL` — signing before the cache would store the signature too, and
every hit past the TTL would serve a URL the sidecar rejects.

### 16.6 `GET /stream?id=&exp=&sig=` — Range, always

Answers `206` to a ranged request with a correct `Content-Range`, and `200` +
`Accept-Ranges: bytes` otherwise; `HEAD` answers the headers alone, which is how
a Chromecast and an iOS player ask whether a resource is worth committing to.
**iOS refuses to play a resource that does not answer Range, and seeking is
nothing but this header.**

Nothing is written to disk. The sidecar opens the upstream media URL with the
client's own `Range` header and relays the bytes, so Range support is the
upstream's rather than a cache that would then need a size bound. Only two
*resolutions* are cached, in memory, with different TTLs because they expire for
different reasons: the search result (long — an `ext:` id is stable) and the
direct media URL (short — those URLs are themselves signed upstream and expire,
and serving an expired one reads to the user as a track that plays for zero
seconds).

### 16.7 Casting a preview is decided, not discovered at the speaker

If `PREVIEW_PUBLIC_URL` is set, `streamUrl` is absolute and public and previews
cast. If it is unset the hub emits a LAN/internal URL and advertises
`previewCastable: false`, and **both clients must then refuse a transfer to a
cast target while any `ext:` track is in the queue**, with a stated reason.
Letting it fail at the speaker instead produces silence with a playing bar over
it, which is the failure this stack has paid for twice.

### 16.8 Configuration

| Env (on the hub) | Meaning |
|---|---|
| `PREVIEW_URL` | how the **hub** reaches the sidecar (an internal Docker name is normal). Unset = proxy disabled, clients hide previews |
| `PREVIEW_PUBLIC_URL` | how a **client or speaker** reaches it. Unset = `previewCastable: false`, **and** the hub falls back to signing `PREVIEW_URL` — which is normally a Docker-internal name no client can resolve, so in practice this is required unless `PREVIEW_URL` is itself a LAN address |
| `PREVIEW_SECRET` | the capability secret. Unset = proxy disabled, because an unsignable stream URL fails at *playback* rather than at configuration |
| `PREVIEW_TTL` | capability lifetime, seconds (default 21600) |

and on the **sidecar**, `PREVIEW_COOKIES` — see §16.4.1.

The sidecar takes `PREVIEW_SECRET` (the same value; it refuses to start without
one, since an unsigned `/stream` is an open media relay pointed at a third-party
extractor) plus its own tuning — see `preview/.env.example`.

---

## 17. "Mixed for You" — the hub stores recipes, the clients regenerate

**A saved queue stores a *result*; a mix stores a *recipe*.** That sentence is
the whole feature. Nothing in either client persisted one before: `RadioManager`
took `{mode, seedId, moodCharacter, count}` as arguments and dropped them,
`mixSig` and the Mood Flow centroid were in-memory only, and `playMix` exited
into a frozen `SavedQueueEntity` that replays rather than regenerates.

**The hub never generates anything.** It stores and broadcasts the recipe; each
client regenerates locally with the engine it already has (`RadioManager` /
`auto-dj/*`). That keeps the hub audio-free and AudioMuse-free, which is the
standing rule — and it is also why a second play of the same mix yields a
different queue, which is the observable difference from a saved queue.

**The name.** User-facing string "Mixed for You"; the noun in code is `mix`. It
deliberately avoids both occupied words: `Screen.RadioList` and Feishin's
create/edit-station forms are Subsonic internet radio, and
`SavedQueueSource.RADIO` / `RadioManager.startRadio` is the *ephemeral*
similarity mix. Navidrome's "Instant Mix" is ephemeral too, which is exactly
what these are not.

### 17.1 The record

```jsonc
{ "id": "mx_<ms>_<hex>", "name": "Your Mix",
  "kind": "similar|fingerprint|adaptive|genre|artist",
  "seedId": "", "seedName": "",
  "moodCharacter": "EchoMatch|SteadyVibes|TransitionMaestro",
  "count": 50, "coverArtId": "",
  "createdAt": 0, "updatedAt": 0, "lastPlayedAt": 0 }
```

`kind` is validated against a closed list and `moodCharacter` against AudioMuse's
three presets — they name a *generator on the client*, and an unknown one is a
recipe no client can run. A client meeting a `kind` it does not know hides the
row rather than guessing, so the list is additive-only. Optional strings are
absent rather than null, matching the saved-queue rule.

**`coverArtId` is the SEED's artwork, stamped once at save time** — not the cover
of whatever the recipe last produced. The seed is a fixed part of the recipe, so
it is still true on a second play; the tracklist is the one thing about a mix
guaranteed to change. The hub has always stored and fanned this field out
(`_sanitize_mix` copies it), and for a while no client wrote it, so every mix
rendered as bare text. A recipe with no seed — `fingerprint`, `adaptive` — has
none, and a client draws something from the `kind` instead.

### 17.2 Acts

`saveMix` (mints when `id` is absent), `renameMix`, `deleteMix`, `touchMix`.
Each answers with a `mixes` broadcast, which is also embedded in `welcome`.

- **`saveMix` with an unknown `id` is not an error** — a client can have minted
  it offline — but a recipe with no name or an unknown `kind` is refused with
  `bad_mix`, because these records are persisted and fanned out to devices that
  never saw the sender.
- **`createdAt` and `lastPlayedAt` are the hub's to keep.** A client re-saving
  sends the recipe, not its history; taking its word would reset the age of a mix
  every time its `count` was edited.
- **`touchMix` bumps `lastPlayedAt` and NOT `updatedAt`.** `updatedAt` is the
  sort and eviction key, so folding "played" into it would reorder the user's
  list under them on every play, and let a mix they listen to evict one they just
  made.

### 17.3 Storage

Capped at `MIXES_MAX = 30`, evicted by `updatedAt` like saved queues. Additive in
exactly the way `savedQueues` is: there is no schema version and no migration
path in `hub.py`, only `data.get(key, default)` — so a `state.json` written
before mixes existed loads with zero of them, and an older hub reading a newer
file silently drops the key.

**No tombstones**, unlike a saved queue, and the asymmetry is deliberate rather
than an oversight. A saved queue is published concurrently by several devices —
every receiver stamps the one it is playing — so a delete races a re-publish and
needs a tombstone to win. A mix is only ever written by the user, on one device,
through an explicit act; nothing republishes it, so there is no race to arbitrate
and union-merge has nothing to merge.
