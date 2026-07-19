# Handoff: Ambient Cover-Color Theming (Direction B)

## Overview
Navic already has a cover-color theming system (kmpalette extraction → materialKolor scheme, used on the album/artist detail pages and the Now Playing screen). It's inconsistent across screens: **Now Playing** uses a good, blurred/saturated cover-art background (`BlendBackground`); **Album/Playlist Detail** uses a flat two-stop gradient (`coverAmbientGradient`) that reads as a grey fog; **Library (home)** has no cover-color treatment at all — `rememberLibraryTabBackground()` is a stub that just returns the flat surface color.

This handoff is scoped to closing that gap: make Album/Playlist Detail use the same rich technique Now Playing already has, add a now-playing-keyed hero glow to Library home, and add a small cover-tinted accent to the Mini Player — **without** any screen's colors fighting each other, and with every color transition using the app's existing easing so nothing pops.

**This is not a request to reskin chrome.** Top bars, buttons, nav, typography stay exactly as they are today. Only the background/ambient layer and color transitions are in scope.

## About the design files
`Navic Redesign.dc.html` (included, section `1b` — "Ambient") is an HTML mockup built as a design reference, not code to port. It exists to communicate the *visual target* (what the blurred-cover wash should look like, how strongly it should fade, where it should and shouldn't appear) for a browser preview — you're implementing the equivalent effect in Kotlin/Compose Multiplatform using the primitives that already exist in this codebase (see below). Ignore sections `1a` and `0` in that file entirely — they're unrelated alternate directions/reference, not part of this task.

## Fidelity
High-fidelity for the **color/blur treatment and transition behavior** specifically (blur radius, saturation boost, scrim curve, where the glow fades to neutral). Low-fidelity / non-binding for anything else the mockup shows (a floating pill nav, a big "Good evening" greeting header, frosted mini-player shape) — those were exploratory and are **not** part of this handoff. Keep the current screen structure; only change how the background paints and how colors transition.

## The key discovery: don't build a new blur system — reuse `BlendBackground`
`ui/components/common/BlendBackground.kt` already does exactly what Album Detail and Library need: a `Modifier.blur(80.dp)` cover image, saturation boosted via `ColorMatrix().setToSaturation(1.5f)`, two rotating duplicate layers for subtle life, and a `Color.Black.copy(alpha = 0.4f)` scrim. It's currently only wired up in `NowPlayingScreen.kt`. **Extend/reuse this component rather than writing a new one.**

## Exact files to target

1. **`ui/components/common/BlendBackground.kt`**
   - Add `.crossfade(400)` to both `ImageRequest.Builder` calls in this file. Today it has no crossfade config, so swapping tracks pops the blurred image instantly — this is the #1 fix for "smooth transitions" and benefits Now Playing too, for free.
   - Consider exposing a `heightFraction` or wrapping the whole `Box` in an optional bottom fade mask (see `CollectionDetailScreenHeadingRow`'s existing `drawWithContent { … BlendMode.DstIn }` pattern below) so callers can cap it to a hero region instead of always `fillMaxSize()`. Keep the existing full-screen call in `NowPlayingScreen.kt` working unchanged.

2. **`ui/screens/collection/CollectionDetailScreen.kt`** + **`ui/screens/collection/components/HeadingRow.kt`**
   - Today: `coverAmbientGradient(animatedSeed, coverColors.isDark)` builds a flat two-stop `Brush.verticalGradient` used as the screen's background (`ambientBrush`).
   - Change: replace that flat brush with `BlendBackground(coverArtId = collection?.coverArtId, isPaused = false)` positioned behind the `LazyColumn`, same as `NowPlayingScreen` already does it (`Box { BlendBackground(...); LazyColumn(...) }`).
   - **Do not touch** `rememberCoverColorScheme` / `NavicTheme(coverColors.scheme, contentColor = onAmbient)` — that derivation is correct and already used for the buttons, track-row tinting, and status-bar brightness. Only the *painter* behind the content changes, not the color/theme derivation.
   - `HeadingRow.kt`'s existing full-width cover with the bottom `BlendMode.DstIn` alpha-fade can stay as-is — it will now fade into the richer blurred background instead of the flat gradient, which is a bigger visual upgrade for very little risk. (Shrinking it to a smaller floating card, per the mockup, is a fine follow-up but is NOT required for this handoff.)

3. **`util/ui/CoverColorScheme.kt`**
   - Implement `rememberLibraryTabBackground()` for real (today: `SolidColor(MaterialTheme.colorScheme.surface)`, a stub). Read the code comment above it carefully — **a previous cover-tinted home background attempt was removed for being laggy on scroll.** Do not repeat that mistake (see Performance below).
   - Recommended shape: don't return a `Brush` (too limited for a blurred image). Add a new composable, e.g. `LibraryHeroAmbient(modifier: Modifier, heightCap: Dp = 300.dp)`, that:
     - Reads `rememberNowPlayingCoverAmbient()` (already exists, already `distinctUntilChanged`s on `coverArtId` only — reuse it, don't write a new extraction path).
     - If `currentSong?.coverArtId` is null (nothing playing): renders **nothing** — falls through to the plain `MaterialTheme.colorScheme.surface`. Never fake a color that isn't there.
     - Otherwise: renders `BlendBackground(coverArtId, isPaused = true)` sized to `heightCap`, with a bottom fade mask (reuse the exact `drawWithContent { drawContent(); drawRect(Brush.verticalGradient(0.55f to Color.Black, 1f to Color.Transparent), blendMode = BlendMode.DstIn) }` pattern already in `HeadingRow.kt`) so it resolves to flat neutral **before** the album/playlist grids begin.

4. **`ui/screens/library/LibraryScreen.kt`**
   - Wrap the existing `PullToRefreshBox { LibraryScreenContent(...) }` in a `Box`, with `LibraryHeroAmbient(Modifier.align(Alignment.TopCenter).fillMaxWidth().height(300.dp))` painted first (behind), so the grid scrolls over it. `LibraryScreenContent`'s `LazyVerticalGrid` already has a transparent background (confirmed — no change needed there), so the hero shows through the gaps between the overview buttons / section headers, the same way Spotify's home hero fades into its list.
   - Remove the old `.background(homeBackground)` call — the hero `Box` replaces it.

5. **`ui/components/layouts/MiniPlayer.kt`**
   - Add `val ambient = rememberNowPlayingCoverAmbient()` (reuse — do not re-extract with a fresh `rememberCoverColorScheme` call).
   - The card's `.dropShadow(shape, Shadow(radius = …, alpha = 0.25f))` currently uses the default (black) shadow color — parameterize `color = ambient.seed` so the mini-player's elevation shadow itself carries the cover's color, like a soft glow.
   - Add a hairline ring: `Modifier.border(1.dp, ambient.seed.copy(alpha = 0.32f), shape)`.
   - **Important:** `CoverAmbient.seed` is not eased today (only `.top`/`.bottom` are, via `animateColorAsState(tween(450))` inside `rememberCoverAmbient`). Wrap the ring/shadow color in your own `animateColorAsState(ambient.seed, tween(450))` at the call site, or add an eased `seedAnimated` field to `CoverAmbient` — otherwise the ring will pop instead of crossfading on track change.

## Consistency rules — so colors never fight each other
- **One extraction path, shared.** Library hero, Mini Player ring, and Album Detail (when navigated into) must all derive from the *same* `rememberCoverColorScheme`/`rememberNowPlayingCoverAmbient` call for a given song — never call kmpalette separately per surface. It's already cached by `coverArtId` via `remember`, so as long as you call the same helper functions (not bespoke extraction), duplicate work and any risk of divergent color is avoided for free.
- **The Library hero must go fully neutral by the bottom of its `heightCap`.** It sits above a grid of a dozen different album covers — if any tint bleeds past the hero into the grid, it will visibly clash with covers of a different hue. No exceptions: the fade-to-transparent mask must reach `MaterialTheme.colorScheme.surface` (not just "mostly faded") before the grid starts.
- **Never hardcode white/black text over any of these washes.** Always resolve via the existing `onAmbientColor(background, scheme)` helper (already used on Detail) so text stays legible regardless of what color a given cover produces. If you add any new text over the Library hero, route it through the same helper.
- **Don't reuse `ExpressiveBlur`/Haze for this.** That system (`ui/components/common/blur/ExpressiveBlur.kt`) is explicitly for frosting chrome *over* live app content (nav bar, mini-player backdrop) and is a no-op when the user's "Expressive blur" setting is off. Blurring the cover art itself must keep working regardless of that setting — use native `Modifier.blur` (i.e., `BlendBackground`), per that file's own doc comment.

## Motion / transition rules
- Reuse `tween(durationMillis = 450)` for every new color transition introduced here (mini-player ring, any home-hero color animation) — this is the exact spec already used by `rememberCoverAmbient` and `CollectionDetailScreen`'s `animatedSeed`. Don't introduce a different duration/easing; the goal is that cover-color changes feel like one consistent system app-wide, not per-screen bespoke animations.
- Add `.crossfade(400)` to `BlendBackground`'s two `ImageRequest.Builder`s (see item 1) so the blurred image itself crossfades between songs/albums instead of popping — today it has no crossfade configured at all.
- On navigating Library → Album Detail (or Detail → Detail), the existing `AmbientColorHolder` (`util/ui/AmbientColorHolder.kt`) already carries the last resolved seed so the next screen's ambient eases in from where you came from instead of flashing neutral. No change needed there, but if you add any *new* cover-driven surface, follow the same pattern (read `ambientHolder.last` as your `initialSeed`, write your resolved seed back on change).

## Performance — read this before touching Library
The doc comment on today's `rememberLibraryTabBackground()` stub says a previous cover-tinted home background was **removed for being laggy on scroll**. To not repeat that:
- The hero must only recompose when the **song changes** (`coverArtId` via `distinctUntilChanged`, exactly like `rememberNowPlayingCoverAmbient` already does) — never on scroll position or the ~4Hz playback-progress tick.
- Implement it as a fixed-position `Box` behind the scrolling grid (see item 4), not as a grid item / not re-measured by `LazyVerticalGrid`.
- Cap it to `heightCap` (e.g. 300.dp) — do not blur a full-screen-height image behind a long scrolling list; keep the expensive draw small and static.

## Design tokens / parameters
These are derived algorithmically per-cover, not fixed hex values — the parameters that matter:
- Blur radius: `80.dp` (existing `BlendBackground` default — keep it; the mockup used a smaller radius for a phone-sized web preview, the existing Compose value is correct for real devices).
- Saturation boost: `ColorMatrix().setToSaturation(1.5f)` (existing — keep it).
- Scrim: existing flat `Color.Black.copy(alpha = 0.4f)` for full-screen use (Now Playing); for the Library hero, use a **vertical** scrim instead so it can fade to fully transparent-then-neutral: roughly `0% → 5% black`, `35% → 55% black`, `70%+ → fully opaque MaterialTheme.colorScheme.surface` (i.e. the last stop should be the flat surface color, not black, so it blends into the rest of the screen).
- Crossfade: `400`–`450`ms, matching the rest of the system's `tween(450)`.
- Mini-player ring alpha: `~0.32`; shadow alpha: keep existing `0.25f`, just swap the color.

## Assets
No new image/icon assets — this task is entirely about compositing the album/playlist cover art the app already loads (via `SessionManager.getCoverArtUrl`), using the app's existing Coil setup.

## Files (mockup reference, included in this bundle)
- `Navic Redesign.dc.html` — open in a browser; scroll to the section badged **1b**. Ignore `1a` and `0`.

## Suggested order of work
1. `BlendBackground.kt`: add crossfade (small, safe, immediately improves Now Playing too).
2. `CollectionDetailScreen.kt`: swap flat gradient → `BlendBackground` behind the `LazyColumn`. Verify against a very light cover and a very dark cover that text (via `onAmbientColor`) stays legible.
3. `CoverColorScheme.kt` + `LibraryScreen.kt`: implement the capped, fading, now-playing-keyed hero, with the "nothing playing → nothing rendered" fallback. Test scroll performance specifically (this is the one that broke before).
4. `MiniPlayer.kt`: tinted ring + shadow, eased.
5. Sanity pass: play three songs with very different cover colors back to back; confirm the Library hero, mini-player ring, and (if you open it) the Detail page all agree on hue for the same song, and all cross-fade rather than pop.
