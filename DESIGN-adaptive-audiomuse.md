# navi-connect — AudioMuse Integration & Adaptive Radio (design)

Status: **design** (not started). Companion to `ROADMAP-V2.md`. Covers the
recommendation/autoplay work for both clients (Feishin + Navic) plus the
Yandex-"Моя волна"-style adaptive mode and its visualizer.

Decisions locked with the user:
- **Tier 1 first, Tier 2-ready** behind a capability probe.
- **Autoplay = Off / Similar / Sonic Fingerprint / Adaptive**, user-selectable in *both*
  clients from day one (modes needing Tier 2 grey out until configured).
- Radio "character" presets (Echo Match / Steady Vibes / Transition Maestro) are **bias
  settings on one engine**, not separate features.
- The adaptive visualizer is the **last** thing built, shown **only** in Adaptive mode.

---

## 1. Capability tiers (what AudioMuse exposes)

**Tier 1 — Navidrome plugin, zero config, existing Subsonic auth.** Endpoints take only
`count`; no tuning, no vectors returned:
- `getSimilarSongs`/`getSimilarSongs2` → Instant Mix (song). Routes via `api.provider.SimilarSongs`
  (agents path), NOT the sonic plugin → works vanilla; AudioMuse upgrades it server-side when set
  as an agent (`ND_AGENTS=audiomuseai`). · `getArtistInfo` → Artist Radio.
- OpenSubsonic `sonicSimilarity` ext: `getSonicSimilarTracks(id, count=10)` scored,
  `findSonicPath(startSongId, endSongId, count=25)` journey. Response = array of
  `SonicMatch{Entry(child song), Similarity float}`.

**CONFIRMED present in Navidrome 0.62.0** (the user's server; verified against source
`Downloads/_nd_src/navidrome-0.62.0`): all four endpoints implemented (`server/subsonic/api.go`
127-130, `sonic_similarity.go`). The two sonic endpoints **404 unless a sonicsimilarity-capability
plugin is loaded** (`api.sonic.HasProvider()`); `getSimilarSongs2` does not require it.
**Capability probe = `getOpenSubsonicExtensions`**: it advertises `"sonicSimilarity"` v1 ONLY when
the provider is loaded (`opensubsonic.go:19`) — use this as the Tier-1 sonic probe. So on this
server Instant Mix + Artist Radio + Song Journey + scored-similar all work at Tier 1 today (sonic
ones need the AudioMuse plugin installed).

**Tier 2 — AudioMuse core API direct** (`http://host:8000`, `Authorization: Bearer <API_TOKEN>`,
single token fine for single-user). Synchronous in-memory lookups (fast); only ML jobs are
RQ-queued (off the client path). Client-relevant:
- `/api/similar_tracks` — knobs: `n`, `radius_similarity`(bool), `mood_similarity`(bool),
  `eliminate_duplicates`(bool, caps `MAX_SONGS_PER_ARTIST`), `mood`+`centroid_index`, `anchor_id`.
  **Returns `mood_vector` + `other_features` per track.**
- `/api/find_path` — `max_steps`, `path_fix_size`, `path_space`(audio|lyrics), `mood_pct`.
- `/api/sonic_fingerprint/generate` — playlist from listening habits (autoplay seed). Param `n`.
- `/api/alchemy` — **ADD/SUBTRACT item mixing → centroid + nearest songs**; knobs
  `subtract_distance`, `temperature`; **returns the full centroid vector** + per-result features.
- `/api/anchors` (save a named centroid), `/api/chatPlaylist`(+Stream, LLM, **slow/streaming**),
  `/api/clap/search` (text→sound), `/api/lyrics/search/text`, voyager `mood_centroids`.

---

## 2. The `SonicEngine` abstraction (the consistency backbone)

One thin engine per client — Feishin `features/sonic/`, Navic rename `RadioManager`→`SonicManager`
— exposing the **same verbs** regardless of stack:

- `instantMix(seed)` · `artistRadio(artist)` · `journey(from,to|mood)` · `fingerprint()`
- `startAdaptive(seed, character)` + `feedback(event)` (see §5)
- `search(text|mood)` (Tier 2)

Each maps to a provider (plugin or core) chosen by a **capability probe** run once per session:
detect plugin sonic-similarity support; detect a configured core API URL+token; mark Tier-2
features "available / unavailable-not-configured / unavailable-index-cold". **Fail-soft:** a cold
index returns 503/404 ("run analysis first") → grey the feature + fall back to Tier 1, never error.

All results route through the **existing hub plumbing** so a mix started anywhere plays on the
active device (Feishin `remoteAct`/`addToQueueByData`; Navic `loadRemoteQueue`).

### Responsiveness rules (Tier 2 will NOT throttle the UI)
Audio is decoupled (calls only build a queue; playback still streams from Navidrome), so a slow
call delays *time-to-playlist*, never tap/scroll/audio. Enforce: (1) all calls off the UI thread
(already true for radio); (2) short connect timeout + cancel-in-flight + debounce text/mood input;
(3) cache results by seed/query; (4) `chatPlaylist` modeled as a streaming long-op, never a
blocking spinner.

---

## 3. Feature surfaces (identical placement in both clients)

1. **Context menu / song-sheet** on song·album·artist → Instant Mix / Artist Radio / "Make a
   Journey to…". (Feishin `play-*-radio-action.tsx`; Navic `SongSheet`/`SongRowDropdown`.)
2. **Dedicated "Sonic" page** — Feishin sidebar item, Navic home row + screen — hosts
   Describe-a-playlist, mood/lyric search, Fingerprint, Alchemy.
3. **Now-playing "more" menu** → "Instant mix from here", "Start a journey".
4. **Playback settings** → the autoplay mode + character control (§4–6), worded identically.

Reintroduce the (previously removed) AudioMuse indicator as a **scoped chip near the queue header**
naming the active generator ("Instant Mix" / "Sonic Fingerprint" / "Journey →" / "Adaptive"), logo
palette (periwinkle/pink/orange) — not an always-on bar tint.

### Catalog as shared data
Preset/character definitions live as a **declarative table** (`{id, displayName, description,
params}`) the `SonicEngine` reads in each client, so Feishin and Navic render the same options and
the engine just translates `preset → query params`. Add a preset → both clients get it free.

---

## 4. Autoplay modes (one control, four modes)

`Off / Similar / Sonic Fingerprint / Adaptive` — same setting in both clients.
- **Similar** (Tier 1): queue-end top-up seeded from the current track via
  `getSimilarSongs2`/sonic-similar. Mirrors Feishin's existing `use-auto-dj.ts` gating
  (remaining < N, dedupe vs queue ids, hub-aware). Closes Navic's autoplay gap.
- **Sonic Fingerprint** (Tier 2): top-up seeded from listening habits (`/sonic_fingerprint`).
- **Adaptive** (Tier 2): the Yandex-style reactive station (§5). Greys out until Tier 2 ready.

Reuse one shared trigger/dedupe policy for all top-up modes so behavior matches across clients.

---

## 5. Adaptive "Mood Flow" — the Yandex-"Моя волна" mechanic

**Goal:** an implicit, feedback-driven station. No mood picker required: skips/likes/play-throughs
steer the vibe; a skip-storm pivots the mood (their example: skipping metal → drifts to jazz).

**Why it's feasible here:** maps almost 1:1 onto **Song Alchemy used dynamically**. The alchemy
endpoint already computes a centroid from ADD/SUBTRACT items and returns it — that centroid *is*
the "current mood vector."

### Session mood state (owned by `SonicEngine`, identical in both clients)
```
MoodState {
  addIds:   [trackId…]   // liked / played-through
  subtractIds: [trackId…]// skipped (esp. early skips)
  centroid: vector?      // last centroid returned by /api/alchemy
  params:   CharacterParams   // from the active preset (§6)
}
```

### Signal policy (the rule-based stand-in for their TinyML)
Fed by transport events we already intercept (skip/next, favorite, track-complete):
- skip < ~20s → strong SUBTRACT · skip late → weak SUBTRACT
- like → strong ADD · play-through → mild ADD
- **recency decay**: cap set sizes / down-weight old signals so the mood keeps drifting and
  stale signals fade (prevents lock-in).
- ignore tracks with no AudioMuse vector (unanalyzed) as seeds.

### Re-splice loop (their "discard precomputed queue, rebuild from fresh feedback")
On each meaningful signal (debounced):
1. POST `/api/alchemy` with current ADD/SUBTRACT (+ `subtract_distance`, `temperature` from
   character) → new centroid + candidate list.
2. **Discard the un-played queue tail**, splice in the new candidates **ahead of the playhead**
   (keep ~1–2 tracks of runway so the pivot is imperceptible; audio never stalls).
3. Persist `centroid` for the visualizer (§7) and the queue-header chip.

**Cold start:** first 1–2 tracks seed from the current song (plain similar), then signals take over.
**Hub-aware:** the device driving autoplay owns the state (same as today's Auto-DJ); two thin
event adapters feed the one shared policy.

---

## 6. Radio "character" presets (bias on the one engine)

Presets are points in a small `CharacterParams` vector `{tightness, moodLock, variety, evolution,
length}`, translated by the engine to query params. They tune *both* one-shot radios and how
reactive Adaptive mode is:

| Preset | Feel | similar_tracks / alchemy mapping |
|---|---|---|
| **Echo Match** | sounds just like the seed | tight radius, `mood_similarity=true`, low `temperature`; in Adaptive, skips barely move the centroid |
| **Steady Vibes** | stay in one lane, low variance | mood-centroid/anchor seed, `eliminate_duplicates=true`, **high `subtract_distance`, low `temperature`** → resists mood change |
| **Transition Maestro** | evolving journey | `find_path` for one-shot; in Adaptive, **high `temperature`** → drifts readily |

UI: **presets for everyone + an "Advanced" expander** with the raw sliders (power-user). Tier-1
collapses this to count + light client-side artist-cap/dedupe (no vectors → no real character).

Caveat: some server knobs are coarse (`radius_similarity` is bool; the radius value is server
config). For finer/continuous control, **re-rank client-side** using the `mood_vector`/
`other_features` returned by `similar_tracks`/`alchemy`.

---

## 7. Adaptive visualizer (LAST phase; only in Adaptive mode)

Inspired by Yandex "Моя волна": a **fluid animated mesh gradient** (aurora / gooey-metaball family),
not a bar/waveform. Two data-driven dimensions:
- **Palette** ← mood: derive colors from the current `centroid` / `mood_vector` / `top_genre`
  (map mood axes → hue set). Morph (don't cut) when the mood pivots.
- **Motion** (blob amplitude + drift speed) ← energy/tempo: from `other_features`, or from live
  audio amplitude where available.

Shown **only** while Adaptive mode is active; otherwise the normal visualizer/background stays.

**Feishin** — swap the **sidebar visualizer** for the fluid gradient when Adaptive is active.
Implementation: WebGL/Canvas fragment shader (mesh gradient / metaballs) or an animated CSS/Canvas
mesh gradient. Feishin already ships `audiomotion-analyzer` for amplitude → reuse for motion;
palette from the session `centroid`. Revert to the standard visualizer when mode changes.

**Navic** — drive the **extended player's blurred background** (existing `BlendBackground`):
animate blur radius + color stops from mood/energy. Implementation: AGSL `RuntimeShader`
(Android 13+) for true fluid motion; fall back to animated `Brush.radial/linearGradient` +
`graphicsLayer` blur with an `rememberInfiniteTransition` on older devices. Intensify only in
Adaptive mode; normal blend background otherwise.

Keep the look consistent: same palette-mapping function and same motion-from-energy curve defined
once (shared spec), implemented per platform.

---

## 8. Build order

1. `SonicEngine` + capability probe (each client). 
2. Tier-1 features everywhere: Instant Mix, Artist Radio, **Song Journey**, **Similar autoplay**
   (closes Navic gap). Shared trigger/dedupe + the queue-header chip + the shared catalog table.
3. Tier-2 client behind the probe: Sonic Fingerprint autoplay, Describe-a-playlist, mood/lyric
   search — light up when configured.
4. **Adaptive mode** (§5) + character presets (§6) wired into the autoplay control.
5. **Adaptive visualizer** (§7) — last.

## 9. Dependencies / open questions
- ~~Confirm Navidrome version supports `sonicSimilarity`~~ **RESOLVED: 0.62.0 implements all four
  endpoints natively** (see §1). Remaining: confirm the AudioMuse plugin is actually installed +
  enabled on the server (the two sonic endpoints 404 without it; probe via getOpenSubsonicExtensions).
- Tier-2 requires the AudioMuse core reachable + `API_TOKEN` + a completed library analysis
  (warm indexes) — surface analysis state in the probe.
- Decide where the core API URL/token live (per-client settings vs hub-distributed config).
- Re-splice UX: how aggressively to drop the queue tail vs. let the current "next" play first.
