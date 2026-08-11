---
name: audiomuse-api
description: AudioMuse-AI capability surface (plugin Tier 1 vs core API Tier 2) + sync/async classification for the navi-connect recommendation work
metadata: 
  node_type: memory
  type: reference
  originSessionId: 213138fd-e09b-449a-b005-2fcfaf1aa233
---

AudioMuse-AI integration surface for [[navi-connect-project]]. Source zips were at
`~\Downloads\AudioMuse-AI-main.zip` (core) + `AudioMuse-AI-NV-plugin-main.zip`
(Navidrome plugin), extracted to `Downloads\_am_src\`.

**Two tiers (decided: build Tier 1 first, Tier 2-ready behind a capability probe):**

**Tier 1 — via Navidrome plugin, zero config, existing Subsonic auth.** `getSimilarSongs2`/
`getSimilarSongs` = Instant Mix (song); `getArtistInfo` = Artist Radio (+component matches).
OpenSubsonic `sonicSimilarity` ext: `getSonicSimilarTracks(id,count=10)` scored +
**`findSonicPath(startSongId,endSongId,count=25)`** = sonic journey. **CONFIRMED: user's
Navidrome is 0.62.0 and implements all four natively** (verified vs source
Downloads/_nd_src/navidrome-0.62.0, api.go:127-130 + sonic_similarity.go). The two sonic
endpoints 404 unless a sonicsimilarity plugin (AudioMuse) is loaded; getSimilarSongs2 doesn't
need it (routes via agents — set ND_AGENTS=audiomuseai to upgrade). **Probe = getOpenSubsonicExtensions**:
advertises "sonicSimilarity" v1 only when provider loaded (opensubsonic.go:19). SonicMatch
response = {Entry(child song), Similarity float}. So Tier 1 (Instant Mix/Artist Radio/Journey/
scored-similar) all work TODAY on this server (sonic ones need the plugin installed). Current
navi-connect code (Navic RadioManager, Feishin auto-dj) uses ONLY getSimilarSongs2.

**Tier 2 — core API direct** (`http://host:8000`, single `Authorization: Bearer <API_TOKEN>`,
fine for single-user). Not reachable via Subsonic. Client-relevant endpoints:
`/api/find_path` (song|mood|anchor endpoints, path_space audio|lyrics), `/api/sonic_fingerprint/generate`
(playlist from listening habits = the autoplay engine), `/api/chatPlaylist`(+Stream) (NL→playlist, LLM),
`/api/clap/search` (text→sound mood), `/api/lyrics/search/text`, voyager `/api/similar_tracks`+`mood_centroids`,
alchemy anchors/radios (persistent stations, ADD/SUBTRACT mixing), clustering, map.

**Throttling assessment (KEY): Tier 2 will NOT hurt client responsiveness.** The
client-facing endpoints (find_path, sonic_fingerprint, voyager similar_tracks, clap, lyrics)
are all SYNCHRONOUS in-memory index lookups (`return jsonify(results)`, no task queue) —
one HTTP round-trip like getSimilarSongs2. Only heavy ML (analysis/clustering/embeddings) is
RQ-worker-queued and never on a client path. EXCEPTION: `chatPlaylist` is LLM-backed +
streams (`stream_with_context`) → must be a streaming/long-op UI, not a blocking spinner.
Audio is decoupled (calls only produce a queue; playback still streams from Navidrome), so a
slow call delays time-to-playlist, never tap/scroll/audio. Three rules for the SonicEngine:
(1) fail-soft capability probe — cold index returns 503/404 "cache not loaded, run analysis
first" → grey the feature out + fall back to Tier 1, don't error; (2) short connect timeout +
cancel-in-flight + debounce; (3) cache results by seed id/query.

**Full design doc written** at `DESIGN-adaptive-audiomuse.md`
(companion to ROADMAP-V2.md) — read it before building. Covers everything below plus the
adaptive mode + visualizer.

**ADAPTIVE "Mood Flow" mode (Yandex "Моя волна"-style, assessed FEASIBLE at Tier 2):** maps
~1:1 onto AudioMuse **Song Alchemy used dynamically**. `/api/alchemy` takes ADD/SUBTRACT item
ids → computes a centroid + nearest songs AND returns the full centroid vector (+knobs
`subtract_distance`, `temperature`). So: ADD = liked/played-through tracks, SUBTRACT = skipped
tracks; recompute centroid on each skip/like/complete signal (rule-based policy w/ recency
decay — the stand-in for Yandex's on-device TinyML), discard the un-played queue tail + splice
fresh candidates ahead of the playhead (their "discard precomputed queue, rebuild from fresh
feedback"). No offline/TinyML needed (always online); latency hidden by 1-2 track runway; audio
decoupled so never stalls. Tier-2 only (needs vectors + analyzed library). Becomes the 4th
autoplay mode (Off/Similar/Fingerprint/Adaptive) and SUBSUMES the character presets: Echo/Steady/
Transition = bias on subtract_distance/temperature/radius (Steady resists mood change, Transition
drifts readily). Character params shipped as a shared declarative catalog table both clients read.

**ADAPTIVE VISUALIZER (LAST phase, only shown in Adaptive mode):** Yandex "Моя волна" look = fluid
animated mesh/aurora/gooey-metaball gradient (NOT bars). Palette ← mood (from centroid/mood_vector/
top_genre, morph not cut on pivot); motion (blob amplitude+drift) ← energy/tempo (other_features or
live audio amplitude). Feishin = swap the sidebar visualizer for it (WebGL/Canvas shader; reuse
existing audiomotion-analyzer for amplitude). Navic = drive the extended player's existing
BlendBackground blur+colors (AGSL RuntimeShader on Android 13+, fallback animated Brush gradient +
graphicsLayer blur + rememberInfiniteTransition). Shared palette-map + motion-from-energy curve
defined once, implemented per platform.

**Consistency plan:** one shared `SonicEngine`/capability-probe per client (Feishin
`features/sonic/`; Navic rename RadioManager→SonicManager) exposing the SAME verbs in the SAME
4 surfaces (context menu, dedicated Sonic page, now-playing more-menu, playback settings), all
routed through existing hub plumbing so a mix started anywhere plays on the active device.
Autoplay = Off/Similar/Sonic-Fingerprint user-selectable in BOTH clients from day one
(Fingerprint greys out until Tier 2 configured). Song Journey (findSonicPath) = shared
flagship (works in both tiers). Reintroduce the removed AudioMuse indicator as a scoped chip
near the queue header naming the active generator (logo palette periwinkle/pink/orange), not
an always-on bar tint.
