# Session summary — audit bug fixes (2026-07-20)

Worked through the three same-day audits (`AUDIT-HUB-`, `AUDIT-NAVIC-`,
`AUDIT-FEISHIN-2026-07-20.md`) and implemented the **bug fixes** (not the
feature/parity work). One commit per repo. See
`SESSION-2026-07-20-audit-remaining.md` for everything left untouched.

| Repo | Commit | Pushed? | Verified? |
|---|---|---|---|
| `hub/` (Python) | `b4c5b90` | **yes** (origin/main) | tests pass (`test_transfer.py` + new `test_edits.py`) |
| `navic/` (KMP) | `bbc1f51` (main) | no | **`:androidApp:assembleRelease` PASSES** (release APK built). iOS targets not built, but commonMain compiles. |
| `feishin/` (TS) | `f67b6226` (master) | no | typecheck clean (web + node); **not dev-run against a live hub** |

---

## hub/ — `hub.py` (commit `b4c5b90`, pushed)

All findings from `AUDIT-HUB-2026-07-20.md` in the suggested fix order:

- **HIGH — stale-socket clobber.** `_disconnect(dev, ws)` now guards `if dev.ws is not ws: return`, so a superseded old socket can't null the live new one or spuriously pause/relinquish active. `_register` evicts (`close(4003)`) any prior live socket before adopting the new one.
- **MED — position-intent grace.** `seek`/`jump`/`next`/`previous` stamp `_position_intent_at` + capture the pre-move position. In `_on_report`, a position report that sits closer to where we just left than to the new target is rejected within `INTENT_GRACE` (stops the scrubber rewinding); genuine forward progress still passes.
- **MED — shuffle order preserved on edits.** `enqueue`/`remove`/`move` patch `order` incrementally (`_order_after_insert/remove/move`) instead of a full reshuffle; `_rebuild_order` is now only for `setQueue`/shuffle-toggle.
- **MED — mirror single-flight + debounce.** One `_mirror_worker` coalesces the latest snapshot and debounces (`MIRROR_DEBOUNCE = 2.5 s`); no more concurrent, out-of-order `savePlayQueue` writes.
- **MED — constant-time token compare** via `hmac.compare_digest`.
- **S cleanups.** `HUB_DEBUG` gates the per-frame DIAG logs (new `dlog`); index clamping on ingest (`_clamp_index`); non-dict first-frame guard; `get_running_loop` over deprecated `get_event_loop`.
- Added `tools/test_edits.py` (shuffle-edit preservation + reconnect-clobber); documented `HUB_DEBUG` in `.env.example`.

> Build note: the first `assembleRelease` FAILED — `pingInterval = 10.seconds` is
> the *server* WebSockets property; the client `WebSockets.Config` uses
> `pingIntervalMillis: Long`. Fixed to `pingIntervalMillis = 10_000` and the
> commit was amended (`13d841b` → `bbc1f51`); release APK now builds at
> `androidApp/build/outputs/apk/release/Navic.apk`.

## navic/ — Kotlin (commit `bbc1f51`, NOT pushed; release build PASSES)

Phase 1 + the well-contained Phase 2 items from `AUDIT-NAVIC-2026-07-20.md`:

**`HubManager.kt`** — Ktor `pingInterval = 10.seconds`; capped exponential reconnect backoff (1s→30s, resets on a live link) + **stop-on-auth-error**; `restart()` `cancelAndJoin`s the old job before reconnecting; new `connectionError` StateFlow; `progress` frames keep prior `index`/`isPlaying`/`positionMs` when omitted. Also `RemotePlaybackRouter.seek(positionMs)` added and implemented (`= actSeek`).

**`MediaPlayer.android.kt`** — adopt an already-live cast session at construction (fixes playback reverting to phone on relaunch during casting); track/cancel `progressJob`; `switchSessionPlayer` relocates the current item by `mediaId` after URI-less filtering; auto-skip stops on the last unavailable track; `refreshCurrentCollection` retries on a miss; `clearQueue` nulls the saved-queue identity; `seek()` routes to the hub when remote; `distinctUntilChanged` on the 7-flow media-rebuild combine.

**`MediaPlayer.kt`** — `routeRemotely` made `protected`; interface `seek` added.

**UI/perf** — `BlendBackground` three `.rotate()` → `graphicsLayer { rotationZ }` (kills the ~60fps recomposition storm); `DownloadCenterViewModel` caches the whole-library song query + terminal-section builds (rebuild only on membership change); deleted dead `AdaptiveMoodBackground.kt`.

## feishin/ — TypeScript (commit `f67b6226`, NOT pushed; typecheck clean)

Phase 1 + well-contained Phase 2 from `AUDIT-FEISHIN-2026-07-20.md`:

**Main process** — `hub/index.ts` + `cast/index.ts`: emit synthetic `{t:'disconnected'}` to the renderer on ws close; WS heartbeat (ping/pong + terminate on missed pong); capped exponential reconnect backoff with jitter (1s→30s) replacing the fixed 3s/5s.

**`use-hub.tsx`** — handle `{t:'disconnected'}` (clear `connected`/`activeDeviceId`, reset guards); handle hub `{t:'error'}` → toast; fix the `do:load` report echo (set `lastQueueSig` in the load branch + gate `publishQueue` on `hubDrivenUntil`); throttle the progress-path `report()` to ~1 Hz; memoize resolved stream URLs by track id.

**`remote-queue.ts` / `player-context.tsx`** — `Play.NOW` on a specific track threads the clicked song → `setQueue.index`; multi-item remote reorder toasts "unsupported" instead of silently no-oping.

**`use-remote-aware.ts`** — optimistic remote scrub (`useRemoteSeek` advances the mirror so the thumb stays put).

**`preload/hub.ts`** — `onMessage` returns a per-listener disposer instead of `removeAllListeners`.

> Housekeeping: the feishin working tree already had three `SESSION-*.md` files showing as deleted (pre-existing, unrelated) — left untouched; only the 7 source files were committed.
