# Navic — Expressive blur: Haze (backdrop chrome) + native (self-blur)

Status: **plan only** (not implemented). Companion to ROADMAP-V2.md / DESIGN-adaptive-audiomuse.md.

## Decision
Add frosted/backdrop blur across Navic's chrome in an M3-Expressive style. After comparing engines:

- **Haze** (`dev.chrisbanes.haze`) = true **backdrop blur**: an overlay (`hazeEffect`) samples and blurs
  the live content marked as `hazeSource` behind it — the frosted-bar-over-scrolling-content effect.
- **Cloudy** (`skydoves`) = **self/content blur**: snapshots and blurs its own children. It does NOT
  sample a live backdrop, so it's the wrong tool for translucent chrome.

Navic already self-blurs natively (`Modifier.blur`) and already has Haze wired on one surface. So
(user-confirmed): **Haze for chrome backdrop, native `Modifier.blur` for self-blur backgrounds, NO
Cloudy.** Each tool used for what it's good at; no redundant dependency.

## Current state (already in place)
- **Haze wired (NowPlaying only):** `haze` + `haze-blur` `2.0.0-alpha03` in `gradle/libs.versions.toml`
  + `composeApp/build.gradle.kts`. `NowPlayingScreen` builds a `hazeState`; `AdaptiveMoodBackground` is
  the `hazeSource`; the autoplay chip (`NowPlayingAutoplaySelector` via `ControlsRow`) uses `hazeEffect`.
- **Gate + toggle:** `PreferenceManager.expressiveBlur` (default off) + `AppearanceScreen` "Expressive
  blur" switch — the single on/off for all backdrop blur.
- **Native self-blur (keep, no change):** `Modifier.blur(...)` in `BlendBackground.kt`,
  `AdaptiveMoodBackground.kt`, `LyricsScreen.kt`, `TechnicalInfoRow.kt`.

## Plan

### Step 1 — settle Haze's version/build (prerequisite)
Confirm `haze 2.0.0-alpha03` compiles & runs on the current CMP 1.11 (it's in the build but may never
have been exercised at runtime). If sync/API mismatches occur, bump to the latest `2.0.x` (or a stable
line if it now supports CMP 1.11) and fix the `hazeSource`/`hazeEffect`/`blurEffect{blurRadius}` calls.

### Step 2 — shared helper (uniform call sites + gating in one place)
`ui/components/common/blur/ExpressiveBlur.kt`: `rememberExpressiveHazeState()` +
`Modifier.expressiveHazeSource(state)` / `Modifier.expressiveHazeEffect(state, radius)` — thin Haze
wrappers that **no-op when `expressiveBlur` is off or below the supported API level**, falling back to
the existing translucent surface (no regression). Migrate NowPlaying's direct Haze usage to it first.

### Step 3 — expand backdrop blur to chrome (priority order)
`expressiveHazeSource` on the scrolling content, `expressiveHazeEffect` on the overlay:
1. **Mini-player bar** — `RootBottomBar` (highest payoff).
2. **ModalBottomSheets** — `ui/components/sheets/*` (device picker, mood search, song/collection/sort).
3. **NowPlaying** — 1:1 migration of existing Haze wiring (lowest risk) first.
4. **Top app bars** — Search / library scaffolds.
Keep the current translucent tint as the off/unsupported fallback on each.

### Self-blur (no work)
Native `Modifier.blur` usages stay as-is (album art / mood aurora). Cloudy intentionally not added.

## Risks / notes
- **API level:** real blur is API 31+ (AGSL 33+); pre-31 → translucent fallback, no crash.
- **iOS:** Haze supports iOS; verify the overlay/sheet cases compile & render (commonMain helper).
- **Perf:** blur is GPU-heavy → keep gated + radius-capped; measure on a real device in `assembleRelease`
  (debug Compose is misleadingly choppy).
- **Don't double-blur:** `AdaptiveMoodBackground` already self-blurs (`Modifier.blur(90.dp)`) — it's a
  Haze *source*, not an *effect* target.

## Verification
- `./gradlew :composeApp:assembleRelease`.
- Toggle Appearance → "Expressive blur": ON = frosted bars/sheets sampling content behind; OFF =
  pixel-identical to today.
- Device matrix: API ≥33 (full), 31–32 (RenderEffect), pre-31 (fallback), + an iOS build.
