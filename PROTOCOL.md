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

### 3.1 Connection handshake

```
Client → Hub : { "t":"hello", "token":"<shared-secret>",
                 "device": { "id":"<persisted-uuid|null>", "name":"Feishin-PC",
                             "platform":"desktop|android", "caps":["receiver","controller"] } }
Hub → Client : { "t":"welcome", "deviceId":"<assigned-uuid>",
                 "session": <Session>, "devices": [<DeviceInfo>...],
                 "savedQueues": [<SavedQueue>...] }   // §8.3
```

- Bad/missing `token` → hub sends `{ "t":"error", "code":"auth", ... }` and closes.
- If `device.id` is null the hub mints a UUID; the client persists it and reuses
  it on every future `hello` (so reconnects don't create duplicate devices).

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
  "caps":["receiver","controller"], "online":true,
  "isActive":false, "lastSeen": 1730000000000 }
```

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

### 5.4 Hub → all: **broadcasts**

| `t` | Payload | When |
|-----|---------|------|
| `session` | `<Session>` | any intent change (new queue, index, repeat…) |
| `progress` | `{ positionMs, index, isPlaying }` | throttled live position from active receiver |
| `devices` | `[<DeviceInfo>...]` | device joins/leaves/goes active |
| `savedQueues` | `{ queues:[<SavedQueue>...] }` | saved-queue history changed (§8.3); also in `welcome` |
| `library` | `{ event, releaseMbid, rgid, artist, album }` | lb-bot placed an album into the library (§15.1) |
| `error` | `{ code, message }` | auth, bad target, etc. Codes: `bad_action`, `target_offline`, `no_active_device`, `unknown_saved_queue` (§8.3) |

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
  whitelisted to the §4.1 fields; and `currentIndex`/`positionMs`/`createdAt`/`updatedAt` are
  coerced to integers with `currentIndex` clamped into the track list.
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
  playback, so a card never renames or re-skins itself as the queue advances. A client that
  renders card art **by song id** rather than from `coverImageUrl` (Navic does, because a
  peer's cover URL carries that peer's server and auth) must therefore use the record's
  **birth** track — its first — not its resume track, or one shared record shows different
  art on each client.
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
  "id":"cast-<txt.id>","name":"📺 <txt.fn>","platform":"chromecast","caps":["receiver"]}}
```

so it joins the ordinary device picker and transfer flow. Casting is therefore a
normal `transfer`, and transfer-with-resume (§7) works unmodified. Audio never
passes through the bridging client — the speaker fetches the track's `streamUrl`
directly, which is why every published track carries `streamUrl` + `mime`.

Neither client uses a Cast SDK; both speak castv2 (TLS to port 8009) directly.
**Feishin** uses `bonjour-service` + `castv2-client`; **Navic** uses `NsdManager`
+ its own castv2 implementation (`domain/manager/cast/`).

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

| Hub route | Upstream | Forwarded | Cache |
|---|---|---|---|
| `GET /lb/status` | `GET /api/summary` | — | 60 s |
| `GET /lb/artist/discography` | same | `nd_id`, `mbid` | 60 s |
| `POST /lb/artist/discography` | same | `mbid`, `name`, `nd_id`, `external` | — |
| `GET /lb/fresh-releases` | same | `days` | 60 s |
| `GET /lb/album/releases` | same | `rgid` | 6 h |
| `GET /lb/album/tracklist` | same | `release_mbid`, `album_ids`, `group_id` | 6 h |
| `GET /lb/album/similar` | same | `artist_mbid`, `artist_name`, `rgid`, `limit` | 6 h |
| `GET /lb/album/sources` | same | `rgid` | 60 s |
| `POST /lb/album/download` | same | `rgid`, `sourceUsername`, `sourceFolder`, `quality` | — |
| `GET /lb/album/status` | same | `release_mbid`, `rgid` | **never** |
| `POST /lb/album/allow-mp3` | `POST /api/gaps/{group_id}/allow-mp3` | `group_id`, `allow` | — |
| `GET /lb/gap` | `GET /api/gaps/{group_id}` | `group_id`, `sourcePage` | **never** |
| `GET /lb/gap/source-files` | `GET /api/groups/{group_id}/sources/{source_index}/files` | `group_id`, `source_index` | 60 s |
| `POST /lb/gap/search` | `POST /api/groups/{group_id}/sources` | `group_id`, `force` | — (45 s timeout) |
| `POST /lb/gap/auto` | `POST /api/gaps/{group_id}/auto` | `group_id` | — |
| `POST /lb/gap/fetch` | `POST /api/gaps/{group_id}/fetch` | `group_id`, `sourceId` | — |
| `POST /lb/gap/cancel` | `POST /api/gaps/{group_id}/cancel` | `group_id` | — |
| `POST /lb/gap/rescan` | `POST /api/gaps/{group_id}/rescan` | `group_id` | — |

No upstream token is injected — lb-bot has none to inject.

**The `gap` group** fills the holes in an album the library already partly has —
a different pipeline from `album/download`, which acquires a release it lacks
entirely. Every route is scoped to one review group, and the client always has
that id already: lb-bot's discography scan *builds* the review group as it
classifies a release `incomplete`, and emits its `group_id` on the row. `auto`
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
`sources[].files` and `sources[].filesTruncated` (`spec["strip"]`), keeping the
per-source summaries the pickers actually render — peer, format, bitrate,
coverage, risk flags, score. This is a bandwidth control, not a security one (the
whitelist is that), but it also keeps other people's file paths off the device. A
non-200, a non-JSON body or an unexpected shape passes through untouched: a
projection must never fail a request.

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

**Probe.** `GET /lb/status` always returns `200` with just the verdict — lb-bot has
no cheap health route, so this rides on `/api/summary`, whose body (a large object
about the Fill-gaps workspace) is deliberately **not** passed through:

```json
{ "configured": true, "upstreamReachable": true, "routes": ["GET /lb/gap", "…"] }
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
  delete-file, trash and the beets import paths. When a gap fill ends in
  `picking` *with no sources*, lb-bot wants a human decision in its own web UI —
  the clients say so and stop, rather than growing a second copy of that
  workspace. `picking` *with* sources is the ordinary "your move" of the picker
  above and must not be worded as a hand-off.

### 15.1 `POST /lb/notify` — the one inbound route

Everything else under `/lb/*` is the hub calling lb-bot. This is lb-bot calling
the hub: when a fill is placed into the library, lb-bot POSTs
`{event, release_mbid, rgid, artist, album}` and the hub rebuilds it, field by
field, into a `library` broadcast (§5.4) to every connected device.

- Configured **on lb-bot**: `LB_BOT_HUB_URL` + `LB_BOT_HUB_TOKEN` (the same
  `HUB_TOKEN`). Unset on either side means no ping, never an error — the placement
  path never waits on it and never fails because of it.
- Answered whether or not `LBBOT_URL` is set, because nothing is forwarded. It is
  handled ahead of the proxy dispatch for exactly that reason: `LbProxy` would
  404 it against its outbound route table.
- Bearer-authed like every other route, so any device token can send one. That is
  fine: the frame carries no authority, it only tells clients to re-read what they
  can already read.

Clients treat it as "drop what you think the library holds": Feishin invalidates
its Navidrome album queries and lb-bot's discography read. **Navic ignores it**
today — it has no lb-bot surface yet (phase 3), so there is nothing there to
refresh; the frame is forward-compatible with its unknown-`t` fallthrough.

Without the ping nothing breaks. lb-bot marks its own index row `present` at
placement, so the next discography read on any client is already correct — the
broadcast only closes the window where a page is *already open* somewhere else.
