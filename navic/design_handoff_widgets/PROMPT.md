You're working in the Navic codebase (Kotlin Multiplatform + Compose Multiplatform, Android/iOS Navidrome client). Read `README.md` in this same folder first — full spec. This is just the entry point.

## Goal
Add a new 4×2 Glance home-screen widget, "Quick Picks": a now-playing transport row on top, 5 shortcut tiles below (3 recently-added albums + 2 random-discovery albums), with a blurred/saturated cover-art background wash matching Navic's existing ambient look — not a flat color panel.

## Start here
1. Read `androidApp/.../widgets/miniplayer/MiniPlayerWidget.kt` and `.../widgets/turntable/TurnTableWidget.kt` — the two existing widgets. Copy their structural pattern (both extend `NowPlayingWidget`, both have a paired `*Receiver` extending `NowPlayingReceiver`, both use `SizeMode.Exact` + `PreferencesGlanceStateDefinition`).
2. Read `androidApp/.../widgets/nowplaying/NowPlayingWidget.kt` and `NowPlayingReceiver.kt` — the shared base class/receiver you're extending.
3. Read `ui/components/common/BlendBackground.kt` — the blur+saturation+scrim technique you need to replicate as a **pre-baked bitmap transform** (Glance/RemoteViews can't run live Compose modifiers).
4. Find wherever the widgets' cover `Bitmap?` is currently produced (check the widget update path in `MediaPlayer.android.kt`, which the earlier `widgets kept rendering the previous song's title` comments reference) — that's where you'll add the derived blurred bitmap.
5. Find the existing "Recently added" and "Random" data calls used by `LibraryScreen.kt` — reuse those repository calls for the tile content; don't add new queries.

## Do, in this order
1. Create `QuickPicksWidget.kt` + `QuickPicksReceiver.kt` mirroring the existing pair, registered in `AndroidManifest.xml` and `res/xml/quick_picks_widget.xml` + `xml-v31` variant + preview layout/drawables.
2. Build the transport row: 56dp cover, title/artist, prev/play-pause/next — reuse `MiniPlayerWidget`'s icons and `actionSendBroadcast(createMediaIntent(...))` wiring exactly.
3. Build the tile row: 5 equal-weight tiles (cover + 1-line title), sourced 3 from recently-added + 2 from random, each tile's tap target deep-linking to that album via `actionStartActivity`.
4. Add the ambient background: pre-bake a blurred (RenderEffect or existing blur util) + saturated (`ColorMatrix` saturation ~1.5) + 40%-alpha-black-scrim bitmap from the now-playing cover, cached by `coverArtId`. Fall back to `GlanceTheme.colors.widgetBackground` when nothing is playing.
5. Use a fixed white circular play/pause button (not the themed `CircleIconButton`) so it stays legible over an arbitrary cover wash — this is deliberate, matches the mock, don't "fix" it to use theme colors.

## Constraints
- No fake personalization — only "recently added" and "random," clearly distinguished (label the random ones, e.g. "Shuffle: …").
- The ambient wash must be a real bitmap transform, not a Compose modifier — Glance widgets are RemoteViews under the hood.
- Reuse existing icon drawables, corner-radius helper (`appWidgetInnerCornerRadius`), and media-intent broadcast pattern — don't introduce parallel implementations.

## Visual reference
`Navic Redesign.dc.html` in this folder — open in a browser, look at the "Home screen widgets" block in section `0` (the "Quick picks — 4×2" mock specifically). Reference for layout/wash intensity only, not code to port.

## When you're done
Confirm: widget shows on the home-screen widget picker with a preview, transport controls work, all 5 tiles deep-link to the correct album, and the background legibly shifts hue when a different song is playing — with the play button staying readable regardless of cover color.
