# Handoff: Quick Picks home screen widget (new)

## Overview
Navic has two Glance home-screen widgets today: `MiniPlayerWidget` (4×1 transport row) and `TurnTableWidget` (2×2 circular cover). This handoff adds a third: **Quick Picks**, a 4×2 widget combining a now-playing transport row with a row of 5 tappable shortcut tiles — similar in *layout* to Spotify's home widget, but with Navic's own visual language (ambient cover-color wash, not a flat brand-color panel) and content sourced from what Navidrome actually exposes (no algorithmic mixes/Discover Weekly — Navidrome has no such feature).

## About the design files
`Navic Redesign.dc.html` (included) is an HTML mockup, not code to port. Look at the **"Home screen widgets"** block inside section `0` ("Current build") — it shows the two existing widgets as faithful recreations of the real `MiniPlayerWidget.kt`/`TurnTableWidget.kt`, plus the new **"Quick picks — 4×2"** mockup below them. Ignore sections `1a`/`1b` — unrelated.

## Fidelity
High-fidelity for layout, sizing, and the ambient-wash visual treatment. The exact tile content (album names, cover art) in the mock is placeholder — wire it to real data per the content spec below.

## Content spec — what the 5 tiles show
Navidrome has no personalized/algorithmic recommendations (no daily mixes, no "Discover Weekly"). Do **not** fake this. The tiles are:
- **Tiles 1,3,5: Recently added albums** — same source as the Library screen's "Recently added" row (`getAlbumList?type=newest` / equivalent repository call already used elsewhere in the app — reuse it, don't add a second query path).
- **Tiles 2,4: Random discovery** — `getRandomSongs` / `getAlbumList?type=random`, no subtitles under the tiles since they do not fit anyway (mock prefixes these "Shuffle: "). Reuse the same random-fetch call the Library screen's "Random" quick-filter already uses.

Do not build new categories (no genre/mood tiles, no fake mixes) — this widget is intentionally just "new" + "surprise me," which is an honest reflection of what Navidrome can do.

## Layout (4×2, ~296×~180dp based on the mock)
- **Top row (transport):** 56dp cover thumbnail, title + artist (single line, ellipsized), previous/play-pause/next controls — same icon set and sizing convention as `MiniPlayerWidget.kt`'s row (reuse `ic_previous`/`ic_play`/`ic_pause`/`ic_next`, same `CircleIconButton` component, same `actionSendBroadcast(createMediaIntent(...))` wiring).
- **Bottom row:** 5 equal-width square tiles, each a cover image, laid out in a `Row` with `defaultWeight()` per tile (mirrors the mock's flex `1 1 0` tiles).

## The ambient wash — the key design choice to preserve
The mock's background is **not** a flat color panel. It's the same blurred/saturated cover-art wash Navic already uses elsewhere (`ui/components/common/BlendBackground.kt`: `Modifier.blur(80.dp)` + `ColorMatrix().setToSaturation(1.5f)` + a `Color.Black.copy(alpha = 0.4f)` scrim), keyed to the now-playing cover, exactly like the Library hero and Now Playing screen.

**Important Glance constraint:** Glance app widgets render via `RemoteViews`, which cannot apply live Compose modifiers like `Modifier.blur()` — there's no equivalent of `BlendBackground` you can just call inside `Content()`. The blur/saturation/scrim has to be **pre-baked into a bitmap** before it reaches the widget:
- Wherever the widget's `bitmap: Bitmap?` is currently produced/loaded for `MiniPlayerWidget`/`TurnTableWidget` (check `NowPlayingWidget`'s update path / whatever loads the cover art for widgets today), add a second derived bitmap for Quick Picks: apply a software box/gaussian blur (e.g. via `android.graphics.RenderEffect` on API 31+ with a fallback blur implementation for older APIs — check if the app already has one for other bitmap processing) plus a saturation boost (`ColorMatrix().setSaturation(1.5f)` applied via `Paint.colorFilter` when drawing to a new `Bitmap`/`Canvas`), then composite a ~40%-alpha black scrim on top.
- Cache this derived bitmap the same way the sharp one is cached, keyed by `coverArtId`, so it isn't recomputed every widget refresh tick.
- If nothing is playing: fall back to the plain `GlanceTheme.colors.widgetBackground` (same fallback `MiniPlayerWidget` uses via `R.drawable.ic_note`) — never fake an ambient color.

## Colors / controls to reuse (don't reinvent)
- Icons: `ic_previous`, `ic_play`, `ic_pause`, `ic_next` (same as `MiniPlayerWidget.kt`).
- Play/pause button: white circular button, dark glyph — matches the mock and is legible over any cover-derived wash (unlike relying on `onPrimaryContainer`, which isn't guaranteed to contrast against an arbitrary blurred cover — this is why the mock uses a fixed white circle here instead of `CircleIconButton`'s themed variant).
- Corner radii: 24dp outer widget, 12dp cover thumbnail, 10dp tiles — matches the existing `appWidgetInnerCornerRadius` convention in `WidgetCorners.kt`; use `appWidgetInnerCornerRadius(24.dp)` on the outer container the same way `MiniPlayerWidget` does.
- `actionStartActivity(launchIntent(context))` on tap-to-open (whole widget), same as the other two widgets; each tile should carry its own `actionStartActivity` deep-linking to that album, not just the generic launch intent.

## Files
- New: `androidApp/.../widgets/quickpicks/QuickPicksWidget.kt` (extends `NowPlayingWidget`, same pattern as `MiniPlayerWidget`/`TurnTableWidget`), `QuickPicksReceiver.kt` (extends `NowPlayingReceiver`), `res/xml/quick_picks_widget.xml` + `res/xml-v31/quick_picks_widget.xml`, a preview layout + preview drawables (mirror `widget_preview_mini_player.xml`).
- Register the new receiver in `AndroidManifest.xml`, mirroring the two existing `<receiver>` blocks for `MiniPlayerReceiver`/`TurnTableReceiver`.
- Reference: `ui/components/common/BlendBackground.kt` (technique to replicate as a bitmap transform), `util/ui/CoverColorScheme.kt` (existing extraction, for reference only — Glance doesn't need the Compose color scheme, just the blurred bitmap).

## Assets
Placeholder gradients in the mock stand in for real album art — no new image assets needed; widget uses the same cover-art loading path as the two existing widgets.
