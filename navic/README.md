# Navic — navi-connect fork

> ### This is a modified version of **[ssalggnikool/Navic](https://github.com/ssalggnikool/Navic)**
> **Modified by [illuminator69](https://github.com/illuminator69), starting July 2026, based on
> upstream `v1.0.0-alpha40`.** All credit for Navic itself belongs to
> [paige](https://github.com/ssalggnikool) and its contributors. Licensed **GPL-3.0**, same as
> upstream.
>
> ⚠️ **The download badges further down are upstream's, not this fork's.** They install the
> original Navic. Builds of *this* fork are published in
> [navi-connect's releases](https://github.com/illuminator69/navi-connect/releases).
>
> Bugs in Navic itself belong [upstream](https://github.com/ssalggnikool/Navic/issues); bugs in the
> hub, cast, lb-bot or AudioMuse layers belong in
> [navi-connect](https://github.com/illuminator69/navi-connect/issues).

## What this fork adds

It makes Navic a client for **[navi-connect](https://github.com/illuminator69/navi-connect)** — a
shared playback session across devices — and a front end for
**[lb-bot](https://github.com/illuminator69/lb-bot)**, which knows what your library is missing.

<p align="center">
  <img src="https://raw.githubusercontent.com/illuminator69/navi-connect/main/docs/screenshots/navic-artist-missing-albums.png" width="45%" alt="Artist page with albums marked 7 missing, 3 missing and Not in your library" />
  <img src="https://raw.githubusercontent.com/illuminator69/navi-connect/main/docs/screenshots/navic-missing-album-review.png" width="45%" alt="Bottom sheet showing edition options, tracklist, quality preference and Find sources" />
</p>

- **The albums you don't own**, shown on the artist page beside the ones you do, with a review sheet
  that checks a release against the canonical MusicBrainz tracklist before downloading it.
- **Remote control + transfer with resume** — a blended player state mirrors the shared session
  across mini-player, now-playing, queue and artwork pager, and the Android notification, lock
  screen and Bluetooth controls drive the *remote* session.
- **Native Chromecast**, via `NsdManager` and a hand-rolled castv2 client — no Cast SDK, no Play
  Services.
- **AudioMuse-AI**: Sonic Fingerprint autoplay, adaptive Mood Flow, character presets, a
  mood-reactive visualizer, and CLAP text→mood search.
- **Saved Queues / Continue Listening** and a **Download Center**, synced through the hub and shared
  with the desktop client.

**Setup:** don't start here — see the
**[navi-connect setup guide](https://github.com/illuminator69/navi-connect/blob/main/TESTING-SETUP.md)**.

---

<sub>Upstream README follows, unmodified.</sub>

---

<div align="center">

# Navic

A modern Navidrome client for Android and iOS.

[![Add to Obtainium](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/obtainium.svg)][ADD_TO_OBTAINIUM]
[![AltSource provides links for most sideloading apps, like Feather](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/altsource.svg)][ALTSOURCE]
[![Link to the latest release where you can download the APK or IPA directly](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/direct_download.svg)][LATEST_RELEASE]
[![Discord](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/discord.svg)](https://discord.gg/TBcnNX66PH)
[![Translate](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/translate.svg)](#translating)
[![Codeberg](https://raw.githubusercontent.com/NavicApp/Branding/refs/heads/main/assets/codeberg.svg)](https://codeberg.org/paige/Navic)

</div>

## Features

* Customisable: large selection of settings and tweaks
* Secure & private: zero permissions, telemetry, or analytics
* Integrated: shows up on the lock screen + quick settings
* Lightweight & fast: zero bloat
* Feature rich: covers almost the entirety of the Subsonic API
* Works offline: syncs your entire library locally, and allows you to download songs

## Screenshots

|                                       Library                                        |                                       Player                                        |                                       Lyrics                                        |                                       Albums                                        |
|:------------------------------------------------------------------------------------:|:-----------------------------------------------------------------------------------:|:-----------------------------------------------------------------------------------:|:-----------------------------------------------------------------------------------:|
| ![](https://github.com/NavicApp/Branding/blob/main/screenshots/library.png?raw=true) | ![](https://github.com/NavicApp/Branding/blob/main/screenshots/player.png?raw=true) | ![](https://github.com/NavicApp/Branding/blob/main/screenshots/lyrics.png?raw=true) | ![](https://github.com/NavicApp/Branding/blob/main/screenshots/albums.png?raw=true) |

## Translating

You can help translate Navic by contributing on [Weblate](https://hosted.weblate.org/engage/navic/).

[![Translation status](https://hosted.weblate.org/widget/navic/navic/svg-badge.svg?threshold=0)](https://hosted.weblate.org/engage/navic/)

## Star History

<a href="https://star-history.com/#ssalggnikool/Navic&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=ssalggnikool/Navic&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=ssalggnikool/Navic&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=ssalggnikool/Navic&type=Date" />
 </picture>
</a>

[ADD_TO_OBTAINIUM]: https://apps.obtainium.imranr.dev/redirect?r=obtainium://app/%7B%22id%22%3A%22paige.navic%22%2C%22url%22%3A%22https%3A%2F%2Fgithub.com%2Fssalggnikool%2FNavic%22%2C%22author%22%3A%22ssalggnikool%22%2C%22name%22%3A%22Navic%22%2C%22preferredApkIndex%22%3A0%2C%22additionalSettings%22%3A%22%7B%5C%22includePrereleases%5C%22%3Atrue%2C%5C%22fallbackToOlderReleases%5C%22%3Atrue%2C%5C%22filterReleaseTitlesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22filterReleaseNotesByRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22verifyLatestTag%5C%22%3Afalse%2C%5C%22sortMethodChoice%5C%22%3A%5C%22date%5C%22%2C%5C%22useLatestAssetDateAsReleaseDate%5C%22%3Afalse%2C%5C%22releaseTitleAsVersion%5C%22%3Afalse%2C%5C%22trackOnly%5C%22%3Afalse%2C%5C%22versionExtractionRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22matchGroupToUse%5C%22%3A%5C%22%5C%22%2C%5C%22versionDetection%5C%22%3Atrue%2C%5C%22releaseDateAsVersion%5C%22%3Afalse%2C%5C%22useVersionCodeAsOSVersion%5C%22%3Afalse%2C%5C%22apkFilterRegEx%5C%22%3A%5C%22%5C%22%2C%5C%22invertAPKFilter%5C%22%3Afalse%2C%5C%22autoApkFilterByArch%5C%22%3Atrue%2C%5C%22appName%5C%22%3A%5C%22%5C%22%2C%5C%22appAuthor%5C%22%3A%5C%22%5C%22%2C%5C%22shizukuPretendToBeGooglePlay%5C%22%3Afalse%2C%5C%22allowInsecure%5C%22%3Afalse%2C%5C%22exemptFromBackgroundUpdates%5C%22%3Afalse%2C%5C%22skipUpdateNotifications%5C%22%3Afalse%2C%5C%22about%5C%22%3A%5C%22%5C%22%2C%5C%22refreshBeforeDownload%5C%22%3Afalse%7D%22%2C%22overrideSource%22%3Anull%7D "Add to Obtainium"

[ALTSOURCE]: https://stikstore.app/altdirect/?url=https://raw.githubusercontent.com/ssalggnikool/Navic/refs/heads/master/app-repo.json

[LATEST_RELEASE]: https://github.com/ssalggnikool/Navic/releases/latest
