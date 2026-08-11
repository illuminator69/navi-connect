# Navic — Handoff: AudioMuse Tier-2, blur & Apple-Music theming

Continuation doc for the current work thread (UI theming / blur / AudioMuse). The broad architecture
lives in `HANDOFF.md`; this is the *current state* so you can resume in a fresh session.

Build: open in Android Studio / `./gradlew :composeApp:assembleRelease` (debug Compose is misleadingly
choppy — test release). **Navic can't be compiled in the assistant sandbox** — code is written carefully
and you build it; expect occasional compile-fix rounds. iOS must still compile (commonMain changes).

---

## ✅ JUST DONE — POLISH ROUND (Navic NOT compiled; Feishin web+node tsc PASS): theming, queue-reset, device pickers
Three user-reported issues. Plan: `~/.claude/plans/root-yourself-in-the-quirky-sonnet.md`.

**1. Navic "richer & vivid" dynamic theming** (fixes black/flat text, non-adaptive top bar, ugly blend).
Root cause: the page paints an ambient gradient computed INDEPENDENTLY of the Material scheme, then
renders text via `scheme.onSurface` (derived for `surface`, not the painted ambient) → low-contrast/flat;
muted covers collapsed `primary` toward black (flat Play button/titles); top bars never adapted.
Centralized fix in `util/ui/CoverColorScheme.kt`: (a) scheme now `PaletteStyle.Vibrant` (was Content) so
accents stay punchy on muted art; (b) NEW top-level `coverAmbientGradient(seed,isDark)` — reduced
white/black wash (light 0.34/0.20, dark 0.22/0.45) so the page CARRIES the cover colour; (c) NEW
`onAmbientColor(bg,scheme)` = picks `onSurface` vs `inverseOnSurface` by luminance distance →
guaranteed-contrast text; (d) `CoverAmbient` gained `onAmbient`. `NavicTheme` (`ui/theme/Theme.kt`)
gained a `contentColor: Color?` param (overrides `LocalContentColor`). Wired: Collection+Artist detail
screens use `coverAmbientGradient` + pass `contentColor = onAmbient` to NavicTheme; `App.kt` root home-wash
passes `homeAmbient.onAmbient`; `ModalBottomSheet` wrapper uses `ambient.onAmbient`. Adaptive top bars:
`RootTopBar` containerColor→Transparent (+scrolled=surface); collection `TopBar` containerColor→
`surface.copy(alpha=titleAlpha)` (was Transparent) so it fades the cover surface in on scroll (artist bar
already did). `HeadingRow` genre → `LocalContentColor.current.copy(alpha=.7f)`; artist `DetailHeading`
bio → `onAmbientColor(ambientColor, scheme)`. NOTE: Vibrant + the wash constants are best-guess — field-test
and TUNE (constants in `coverAmbientGradient`). The HeadingRow hero fade is an alpha (DstIn) mask, not a
black tint — left as-is (reduced wash makes the bleed smoother).

**2. Queue resets on device switch/cast** — ROOT (high-confidence): Feishin `use-hub.tsx` `resolveSongs`
FILTERED OUT songs missing from the local library (or a transient getSongDetail fail) → shortened queue +
shifted index → hub `index` pointed at the wrong track = "reset". FIX: `resolveSongs` now returns a STRICT
1:1 list, synthesizing a placeholder `Song` from the hub track meta for any unresolved id (mirrors Navic's
`resolveQueue`; id is a valid Navidrome id so playback works). Verified the cast bridge's `currentTime>0` +
`releasing` guards are all intact (no change). Navic already resolves 1:1 (`resolveQueue`/
`resolveRemoteTracks`) AND its `do` dispatch is serialized (`for(frame in incoming)` awaits each
`handleFrame`) → no concurrent-resolve race; no Navic change made.

**1b. THEMING REVISION 1** (field-test of 1 above): the Vibrant/reduced-wash approach still looked wrong —
dark album covers produced LIGHT pages with black text (page brightness followed the APP THEME, user in
light mode = brightness MISMATCH, not a contrast bug) and Vibrant invented a teal on a greyscale cover.
User chose: page brightness FROM THE COVER ART; greyscale covers stay neutral. Done in `CoverColorScheme.kt`:
`rememberCoverColorScheme` now derives `coverIsDark = dominantColor.luminance() < 0.5` and passes THAT as
`isDark` to `rememberDynamicColorScheme` (page follows artwork); `CoverColors` gained `isDark`. Style:
`Vibrant → Content` for colourful covers (a dark scheme already gives a bright `primary`, so accents stay
vivid without inventing colours) and `Monochrome` for low-chroma covers (new `Color.isLowChroma()` =
max−min RGB < 0.10 → kills the invented teal). Callers use `coverColors.isDark` for `coverAmbientGradient`/
`onAmbientColor`: CollectionDetailScreen, ArtistDetailScreen, `rememberCoverAmbient`. STATUS BARS: the unused
`ForceDarkSystemBars()` expect/actual was renamed/parameterized to `ForceSystemBars(isDarkBackground)`
(android sets `isAppearanceLightStatusBars = !isDarkBackground`; iOS no-op) and called from both detail
screens with `coverColors.isDark` so icons contrast the cover-driven page. Thresholds (0.5 luminance, 0.10
chroma) + wash constants TUNABLE. Brief eased light→dark transition as a dark cover loads is expected
(`initialSeed` carry-over reduces it). NowPlayingScene `colorSchemeForCurrentSong` is SEPARATE (unchanged).

**1c. THEMING REVISION 2** (field-test of 1b): SUNSHOWER (muted-but-colourful cover) rendered FULLY
GREYSCALE (black text + BLACK Play button + grey rows) because `isLowChroma → Monochrome` over-triggered;
and with "Dynamic home background" ON the home/tabs didn't cohere (top bar dark-flash on scroll, overview
buttons / list-header bars / cards clashing). ROOT (Explore-confirmed): the QUEUE looks right because it
uses the cover SCHEME's tokens (surface/surfaceContainer/onSurfaceVariant, all hue-harmonised by
materialKolor); the home/tabs painted a SEPARATE `coverAmbientGradient` wash (raw seed→white/black lerp)
that DIVERGED from those tokens, so every chrome element (OverviewButton=surfaceContainer, ListContent
count/letter headers=.background(surface), RootTopBar scrolledContainerColor=surface) sat on a mismatched
bg. FIXES (all in `CoverColorScheme.kt` + App/LibraryScreen): (1) dropped the `isLowChroma→Monochrome`
branch — `style = if(coverUri!=null) Content else Monochrome` (Content leaves true-greyscale covers grey
WITHOUT inventing a hue, so SUNSHOWER keeps colour); removed the now-unused `Color.isLowChroma()`. (2) tint
the scheme's text so light pages aren't "just black": after building the scheme, `rawScheme.copy(onSurface=
lerp(onSurface,primary,0.14), onSurfaceVariant=lerp(onSurfaceVariant,primary,0.18))` → body/row/header text
carries the cover hue on light AND dark pages. (3) KEYSTONE — `rememberLibraryTabBackground()` now returns
`Brush.verticalGradient(colorScheme.surface, colorScheme.surfaceContainerLow)` (SCHEME tones, read under the
app-root `NavicTheme(homeAmbient.scheme)`) instead of the divergent `ambient.top/bottom`; App.kt NavDisplay
bg + LibraryScreen homeBackground now REUSE it. So the painted bg == the family the chrome paints → count/
letter header bars blend (no black bar), overview buttons read as subtle cards, and the top-bar scroll color
(surface) matches the bg top (no dark flash). Detail pages keep their immersive coverAmbientGradient wash
(Monochrome-drop + tinted onSurface fix their text without changing the look). Bottom nav already uses
NavigationBarDefaults.containerColor = colorScheme.surfaceContainer (cover-derived under the theme) — fine.
Tunable: text-tint 0.14/0.18, wash stops surface/surfaceContainerLow. NowPlayingScene colorSchemeForCurrentSong
SEPARATE (unchanged). NOTE: LibraryScreen has harmless unused imports (Brush/SolidColor/MaterialTheme/
preferenceManager) post-refactor.

**3. Device pickers (both) + "playing on another device" indicator (both).** Rows now show name +
platform + status (Desktop/Android/Cast · playing/online/offline/this device). Manual HIDE persists
(Feishin `HubSettingsSchema.hiddenDeviceIds: string[]`; Navic `PreferenceManager.hubHiddenDeviceIds`
comma-joined). OFFLINE auto-hide: offline + manually-hidden devices drop out of the main list behind a
"Show offline & hidden (N)" toggle; per-row Hide/Unhide. Files: Feishin `hub-device-picker.tsx` (rewrite,
uses `useSettingsStoreActions().setSettings({hub:{hiddenDeviceIds}})`); Navic `DevicePickerSheet.kt`
(rewrite, injects `PreferenceManager`). Indicator: Feishin `left-controls.tsx` shows a green
`Icon success radio` + "Playing on <device>" (via `useHubIsRemoteActive`/`useHubActiveDeviceName`) above
the title when remote; Navic `MiniPlayer.kt` supportingContent shows primary-coloured "Playing on
<device>" when `isRemoteActive`, and `nowPlaying/components/rows/InfoRow.kt` adds a primary "Playing on
<device>" label above the title. Device name resolved from `hubManager.devices`+`activeDeviceId`.

---

## ✅ JUST DONE — ROUND 8 (not compiled): library-wide dynamic theming (perf-safe) + 2 regressions
Round 7's App.kt root theming had been REVERTED (perf: it recomposed the whole app every progress tick).
Re-done correctly:
- **Perf-safe now-playing helper:** `rememberNowPlayingCoverAmbient` (`CoverColorScheme.kt`) now collects
  ONLY `currentSong.coverArtId` via `map { }.distinctUntilChanged()` → recomposes on SONG change, not per
  ~4Hz tick. (This was the round-7 revert cause.)
- **Root theming (toggle ON):** `App.kt` — `NavicTheme(if (homeWashOn) homeAmbient.scheme else null)` +
  NavDisplay background = now-playing gradient/surface. Themes the chrome (top bar, nav bar, mini player,
  overview buttons, text) via the scheme. Detail screens/sheets/now-playing override with their own cover.
- **Wash on EVERY tab:** new `rememberLibraryTabBackground(): Brush` helper (gradient when on, else
  `SolidColor(surface)`); each browsing tab's `PullToRefreshBox` `.background(...)` now uses it
  (Album/Artist/Playlist/Genre/Song/Starred — Library already had its own equivalent). Was the blocker:
  each tab painted an opaque `surface`. Off-state is identical to before.
- **Artist "Frequently played" overlap FIXED:** round-7 wrapped header+grid in `BoxWithConstraints` (a Box →
  they stacked). Added a `Column` inside the BoxWithConstraints so they're vertical again.
- Scope (user-confirmed): whole-app dynamic when toggle on (Settings etc. tint too); detail covers win.

## ✅ ROUND 7 (not compiled): full home integration, reliable artist width, remote-queue fix
- **Home wash fully integrated (toggle ON):** `App.kt` now themes the ROOT by the now-playing cover —
  `NavicTheme(if (homeWashOn) homeAmbient.scheme else null)` + the NavDisplay background becomes the
  now-playing gradient. So every library tab + the shared top/nav bars + the overview buttons + text adapt
  (they all read `MaterialTheme.colorScheme`). Detail screens / sheets / now-playing still override with
  their own cover (nested `NavicTheme` wins). `homeAmbient = rememberNowPlayingCoverAmbient()`. Toggle still
  off by default. (Non-Library tabs get a now-playing-tinted `surface` from the scheme rather than the full
  gradient — to put the gradient on every tab too, make their Scaffolds' containerColor transparent.)
- **Artist "Frequently played" cutoff — real fix:** replaced the unreliable `LocalWindowInfo` width with
  `BoxWithConstraints` around the top-songs block; rows now use `maxWidth` (measured) so they're truly
  full-width. Removed the dead `LocalWindowInfo` width code + import.
- **Remote-queue mismatch (Navic controlling Feishin) FIXED:** `HubManager.startRemoteMirror` was resolving
  the session via `resolveSongs` (DROPS songs not in Navic's local DB → shorter queue → shifted index → wrong
  "current song", and a jump sent the wrong index so Feishin restarted the queue). Now uses a new
  `resolveRemoteTracks` (1:1 with placeholders, mirrors `resolveQueue`) and the hub's `session.index`
  directly, so the mirror length/order/index match the session exactly. (`resolveSongs` now unused.)

## ✅ ROUND 6 (not compiled): backing-shape, queue red bleed, artist cutoff, home toggle
- **Album "underlying shape" fixed:** the distinguishing backing behind the frosted album rows was the
  `SwipeToDismissBox` action background (`primaryContainer`) showing through the now-translucent card, but it
  was clipped to `largeIncreased` (wrong shape). `collection/components/SongRow.kt` now clips it to
  `itemShape.shape` so the accent backing matches the segmented card.
- **Queue red bleed prevented:** the queue card's delete background (`errorContainer`) would bleed red
  through the frosted card; `queue/components/Item.kt` now paints it only while actively swiping
  (transparent when `Settled`).
- **Artist "Frequently played" cutoff:** grid cell height was clipping rows — raised per-row height
  84→100dp; also `coerceAtLeast(280.dp)` on the computed row width (guards a 0 window size on first frame).
- **Home wash → Appearance toggle (user choice):** new `PreferenceManager.homeAmbientBackground` (default
  OFF) + "Dynamic home background" `SettingSwitchRow` in `AppearanceScreen`; `LibraryScreen` uses the
  now-playing gradient only when enabled, else plain `surface`. (Avoids the dynamic-bg / system-chrome
  clash by default; user opts in.)

## ✅ ROUND 5 (not compiled): text actually adapts + visible gradient + cutoff + frosted cards
- **Root text fix:** `NavicTheme` (`ui/theme/Theme.kt`) now also provides `LocalContentColor =
  colorScheme.onSurface` when a cover scheme is passed (it only set `colorScheme` before, so default-colored
  text kept the system content colour "no matter the background"). One change → all themed surfaces adapt.
- **Accent headers/titles = cover `primary`** (user choice): artist name (`DetailHeading`), artist
  "Frequently played" header, `ArtCarousel` titles (Albums/Similar artists — also affects starred), sheet
  titles (`DevicePicker`/`MoodSearch`), and item-sheet headlines (`Song`/`Collection`/`ArtistSheet`).
- **Unified, more visible gradient:** album + artist + `rememberCoverAmbient` now share eased constants with
  a clear delta — light top 0.58 / bottom 0.34, dark top 0.30 / bottom 0.55 (tunable). Artist already paints
  the brush on its scroll Column.
- **Frequently-played cutoff:** `SongRow` gained a `width: Dp = 400.dp` param; the artist grid passes a
  full-width value (`LocalWindowInfo.containerSize` via `density`, −24dp) so rows aren't clipped and 1–2-song
  artists page cleanly (commonMain-safe — no `LocalConfiguration`).
- **Frosted (translucent) cards:** queue (`queue/components/Item.kt`), album song rows
  (`collection/components/SongRow.kt`), and artist accent rows are now `…copy(alpha = 0.6f)` so the gradient
  shows through.
- **Home wash:** `LibraryScreen` background swapped from `surface` to the now-playing ambient gradient.

## ✅ ROUND 4 (not compiled): artist gradient + eased lerp, few-songs grid, sheet anim
- **Sheet colour transition regression FIXED.** Round 3 swapped the animated wash for a static gradient, so
  the cover colour POPPED in. `rememberCoverAmbient` (`CoverColorScheme.kt`) now wraps `top`/`bottom` in
  `animateColorAsState(tween(450))` → fades neutral→cover like before (and like the detail screens).
- **Eased white-lerp** (richer, less washed-grey on colourful covers): light-mode mute toward white reduced
  (top 0.66→0.52, bottom 0.50→0.40); dark unchanged. Applied in `rememberCoverAmbient` (sheets) AND the
  artist page.
- **Artist page → gradient** (was a flat `containerColor`): `ArtistDetailScreen` now computes
  `ambientTop`/`ambientBottom` from `animatedSeed` + `Brush.verticalGradient` on the scrolling Column;
  Scaffold `containerColor = ambientTop`; heading fades into `ambientTop`.
- **Few-songs "Frequently played" fix**: the `LazyHorizontalGrid` rows/height are now
  `songs.size.coerceIn(1,3)` × ~84dp, so 1–2-track artists don't show a cut-off half-empty 3-row grid.
- **NOTE — album page NOT yet changed**: it still uses the OLD (more-washed) inline constants. Pending user
  review of the artist look, then extend the gradient + eased lerp to the **album page** and **home
  (LibraryScreen)**, and explore making the **queue / album song cards** frosted (user wants to scope that
  after seeing the artist page). `ArtAmbientBackground` remains unused (available for reuse).

## ✅ ROUND 3 (not compiled): adaptive theming for sheets/queue/artist (dark palettes)
Field test showed round-2's wash only set BACKGROUNDS; content stayed system-coloured. Fixes:
- **Content now themed with the cover scheme.** New `CoverAmbient(scheme, seed, top, bottom)` +
  `rememberCoverAmbient` / `rememberNowPlayingCoverAmbient` in `CoverColorScheme.kt` (replaces
  `rememberNowPlayingAmbientSeed`). The shared `ModalBottomSheet` wrapper now takes `ambient: CoverAmbient?`:
  `containerColor = ambient.top` (opaque → the **drag-handle notch blends**, no more detached/transparent
  notch), `contentColor = ambient.scheme.onSurface`, and content wrapped in `NavicTheme(ambient.scheme)` over a
  `top→bottom` gradient Column. So sheet text + the queue current-song card (`surfaceContainerHigh`/`primary`)
  adapt to the album.
- **Queue**: hosted via `BottomSheetScene` → now passes `ambient = rememberNowPlayingCoverAmbient()`; the
  round-2 in-`QueueScreen` ambient Box was REVERTED (the sheet provides bg+theme).
- **Item sheets** (Song/Collection/Artist) pass `rememberCoverAmbient(itemCoverId)`; **Sort** passes
  now-playing. **DevicePicker + MoodSearch** migrated from Material3-direct to the shared wrapper (same package)
  with `ambient` — fixes their notch + blend.
- **Artist "Frequently played" rows**: `SongRow` gained a `containerColor` param; `ArtistDetailScreen` passes
  `secondaryContainer` (cover accent under NavicTheme) instead of the near-white default `surface`.
- STILL OPTIONAL (flagged, not done): richer artist-page ambient (gradient instead of solid) + easing the
  light-mode white-lerp if covers still look washed. `ArtAmbientBackground` is now unused (kept for that).

---

## ✅ EARLIER (not yet compiled/verified by user): theming polish + remote queue/volume
Plan file: `~/.claude/plans/the-border-between-the-sunny-shell.md`. Earlier rounds (transparent top bar,
always-valid `takeOrElse` seed, `animateColorAsState`) are folded into the below.

**1. Album hero seam → continuous-ambient alpha mask (the field-test seam, with an Apple-Music ref).**
The old approach faded the cover to an OPAQUE `ambientColor` (= `ambientTop`) overlay, but the page
gradient at the cover's bottom edge is partway to `ambientBottom` → colour discontinuity (worse on bright
art). Fix: `collection/components/HeadingRow.kt` now masks the cover bottom to **transparent**
(`graphicsLayer { compositingStrategy = Offscreen }` + `drawWithContent { drawContent(); drawRect(verticalGradient
0.55→Black, 1→Transparent, BlendMode.DstIn) }`) so the page's `ambientBrush` shows through unbroken — one
gradient, no second colour. Dropped the `ambientColor` param (+ call-site arg in `CollectionDetailScreen.kt`).
NOTE (user decision): track rows stay segmented `surfaceContainer` cards (NOT flattened onto the wash).

**2. Album→artist colour carry (no neutral-default flash).** New `util/ui/AmbientColorHolder.kt`
(Koin `singleOf` in `ManagerModule.kt`) holds the last resolved seed. `CoverColorScheme.kt` gained an
`initialSeed` param passed as kmpalette `defaultColor`, so `coverColors.seed` STARTS at the carried colour
and `animateColorAsState` eases carried→new. Both detail screens read `holder.last` as `initialSeed` (once,
via `remember`) and write `coverColors.seed` back in a `LaunchedEffect`.

**3. Queue drag.** (a) LOCAL smoothness: `QueueScreen.kt` `displayQueue` is now `QueueEntry(uid, song)`
with a STABLE per-slot `uid` (key = `entry.uid`) so `animateItem` animates reorders again (index keys had
killed it; uid wrapper needed because a queue can hold the same song twice). (b) REMOTE reorder now wired:
drag enabled while remote (`dragEnabled = true`, Item.kt comment updated); on release it commits via
`HubManager.actMoveQueueItem(from,to)` → hub `act move` (which already existed at hub.py:448 — I added
`s.index` adjustment so reordering across the playing track doesn't restart it). Feishin parity:
`player-context.moveSelectedTo` routes single-item moves to `remoteAct('move',{from,to})` (computing the
hub's post-pop insert index from the `remote:<i>` row ids); `play-queue.tsx` `enableDrag` now always on.

**4. Remote volume.** `HubManager.actSetVolume(level)` (→ hub `act volume`, already handled at hub.py:512,
forwards `do setVolume` to the active device ONLY). `DevicePickerSheet.kt` shows a `Slider` for the active
remote device (seeded from `HubDevice.volume`), committing on release (`onValueChangeFinished`).

**Typecheck:** Feishin web `tsc` PASSES. Navic NOT compiled (sandbox) — expect a possible compile-fix round
(watch `CompositingStrategy`/`drawWithContent` imports in HeadingRow, and the kmpalette `defaultColor` param).
**Verify (user build):** bright-art album → cover dissolves into the list with no seam (light+dark);
album→artist eases colour with no neutral flash; queue drag animates smoothly + commits (local AND remote);
device picker shows a working volume slider for the active remote device.

---

## ✅ Done & confirmed by the user
- **AudioMuse Tier 1** (similar-songs radio, Song Journey, artist radio) — both clients.
- **AudioMuse Tier 2 — Navic:** Sonic Fingerprint autoplay; Mood Flow (Adaptive) with skip/play-through
  signals; character presets; adaptive visualizer + mood-reactive palette. **Feishin:** autoplay-source
  dropdown (Auto DJ / Fingerprint / Mood Flow) + Mood Flow feedback signals + blob-visualizer mood palette.
- **CLAP text→mood search** ("Mood search") — Feishin (command palette) + Navic (search screen, preview
  sheet). Fail-soft, capability-gated. (Chat/`chatPlaylist` skipped — user has no LLM on the server.)
- **AudioMuse generator chip** (names the active generator + centroid_2d tint) — both clients.
- **Mini-player + bottom-nav Haze frost** (Navic) — confirmed working.
- **Mood Flow character quick-access button** in NowPlaying (Navic) — confirmed working.
- **Album/playlist + artist pages: theme-aware dynamic colour** (light page in light mode, dark in dark)
  from the cover/photo dominant colour, readable text via materialKolor — confirmed (artist colours look
  a bit "weird" on muted photos; see below).
- **Album/playlist full-bleed hero** (cover edge-to-edge, transparent top bar with floating buttons) —
  confirmed "looks good" (pending the smooth-bleed fix above).

## 🔧 Remaining roadmap (after the current task)
1. ~~**Artist page polish**~~ — DONE (not yet compiled by user). `ArtistDetailScreen.kt` now uses the same
   `animateColorAsState(coverColors.seed, tween(450))` always-valid/animated ambient as the album; the
   photo's bottom fade in `artist/components/DetailHeading.kt` now fades into the page `ambientColor` (new
   param) instead of `colorScheme.background`, unifying heading fade with the page background (was the
   "weird" mismatch). `Color.Unspecified`-pop is gone now that `CoverColorScheme` returns an always-valid
   seed. NOTE: the artist page still uses a solid `containerColor = ambientColor` (not a top→bottom
   gradient like the album) — looked consistent enough; revisit if the user wants the gradient too.
2. ~~**Sheets + Queue = dominant-colour wash**~~ — DONE (not yet compiled by user). `ArtAmbientBackground`
   now drawn behind: `QueueScreen.kt` (now-playing seed, full-screen Box) and the sheets. Infra:
   `CoverColorScheme.kt` gained `rememberAppIsDark()` + `rememberNowPlayingAmbientSeed()`; the shared
   `ui/components/sheets/ModalBottomSheet.kt` wrapper gained `ambientSeed`/`ambientIsDark` (transparent
   container + ambient Box behind a fillMaxWidth Column when set). Wired: Song/Collection/Artist sheets use
   the item's own cover seed; Sort + the Material3-direct DevicePicker/MoodSearch sheets use the now-playing
   seed (both manually wrapped since they don't use the shared wrapper). Haze still can't backdrop a
   `ModalBottomSheet` (separate window) — this is the wash, not a true blur.
3. Optional later: Haze "expressive blur" remains gated behind the Appearance toggle for the mini-player/
   nav; the unused `ForceDarkSystemBars` expect/actual (`util/core/PlatformContext.*`) can be reused or
   removed.

## 🧱 Key building blocks (reuse — do NOT add libs)
- **kmpalette** `rememberDominantColorState` / `rememberNetworkLoader` — dominant colour from art.
- **materialKolor** `rememberDynamicColorScheme` — seed colour → readable M3 scheme (on-colours handle
  text contrast for free).
- `util/ui/CoverColorScheme.kt` — `rememberCoverColorScheme(coverArtId, isDark)` → `CoverColors(scheme, seed)`
  (generalised from `NowPlayingScene.colorSchemeForCurrentSong`).
- `ui/components/common/ArtAmbientBackground.kt` — animated dominant-colour wash (eases toward white/black
  per theme); for sheets/queue.
- `ui/components/common/BlendBackground.kt` — self-blurred album-art ambient (alt look; works in any window).
- Haze: `ui/components/common/blur/ExpressiveBlur.kt` (`expressiveBlurEffect`/`LocalExpressiveBlur`) +
  global `hazeSource` on the App `NavDisplay`. **Haze CANNOT sample behind a `ModalBottomSheet`** (separate
  window) — that's why sheets/queue use an ambient wash, not Haze.
- Detail-screen theming pattern: wrap the Scaffold in `NavicTheme(coverColors.scheme) { … }` (like the
  NowPlaying sheet) so all text/rows adapt automatically.

## ⚠️ Gotchas
- **Async seed:** `dominantColorState.color` is `Color.Unspecified` until extraction — never `lerp()` it
  raw; use `takeOrElse {}` + `animateColorAsState` (the current task).
- **Theme-aware, not forced dark:** dynamic pages follow `preferenceManager.themeMode` + `isSystemInDarkTheme()`.
  Don't force `isDark = true`.
- **Status bar:** once the ambient matches the app theme, the global handling in
  `rememberPlatformContext` (`isAppearanceLightStatusBars = !isDark`) is already correct — no override.
- **Tab indentation:** Navic files use tabs; multi-line `Edit` matches are fragile — match single bare
  lines (no leading whitespace) when possible.
- Kotlin is whitespace-insensitive — imperfect indent from edits is harmless.

## Field-test follow-ups still open (non-theming)
- Navic queue drag: edge auto-scroll while dragging toward the bottom is limited (vendored
  `ReorderUtils` only overscrolls past the first/last item) — enhance to trigger on edge *proximity* if wanted.
