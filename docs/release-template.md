<!--
Template for the combined release page "Worbus {{version}}" on illuminator69/worbus (made by hand with
`gh release create v{{version}} --draft --title "Worbus {{version}}" --notes-file ...`). Fill every {{ }} and delete this
comment. Placeholders:
  {{version}}             the version without the v, for example 0.6.0
  {{server_digest}}       the pushed worbus-server image digest, sha256:...
  {{android_sha256}}      the SHA-256 of Worbus-v{{version}}.apk, from the Android release's SHA256SUMS
  {{desktop_sha256sums}}  the lines of the desktop release's SHA256SUMS file, pasted as they are
  {{server_new}}, {{android_new}}, {{desktop_new}}  what is new in each piece (a short list)
The client releases' own notes are each piece's section below, set with `gh release edit --notes-file`.
Publish this page last, after the Android and desktop releases it links to.
-->

Worbus is your Navidrome music library, finished and shared: one playback session across every device, and the
albums your library is missing shown right on each artist's page. Worbus is not affiliated with Navidrome.

New to Worbus? Start with [INSTALL.md](https://github.com/illuminator69/worbus/blob/v{{version}}/INSTALL.md).
Found a bug, or have an idea? [Open an issue](https://github.com/illuminator69/worbus/issues/new/choose); one tracker
covers every piece.

## Worbus Server

{{server_new}}

```sh
docker pull ghcr.io/illuminator69/worbus-server:{{version}}
```

Image digest: `{{server_digest}}`

The server has no files to download: it is the image, which `compose.yaml` in the INSTALL page pulls.

## Worbus for Android

{{android_new}}

- Download: [Worbus-v{{version}}.apk](https://github.com/illuminator69/worbus-android/releases/download/v{{version}}/Worbus-v{{version}}.apk)
  on [worbus-android's release](https://github.com/illuminator69/worbus-android/releases/tag/v{{version}}).
- SHA-256 of the APK: `{{android_sha256}}`
- Worbus {{version}} updates 0.5.0 in place: install it over the one you have.
- To be told about updates, add `https://github.com/illuminator69/worbus-android` to Obtainium with the APK filter
  `Worbus-v.*\.apk`.

**Check who signed it.** Every Worbus for Android release is signed with the same key. Its certificate's SHA-256
fingerprint is

```text
0fe4af344c6d766f7f98796de8cb234906d181fe48d41ecbabf93eaee9401d03
```

and you can check it yourself with the Android SDK's `apksigner`:

```sh
apksigner verify --print-certs Worbus-v{{version}}.apk
```

The `certificate SHA-256 digest` it prints must be the fingerprint above. If it is not, do not install the file.

## Worbus for desktop

{{desktop_new}}

- Download: the files are on [worbus-desktop's release](https://github.com/illuminator69/worbus-desktop/releases/tag/v{{version}}):
  Linux (AppImage, deb, tar.xz), Windows (a portable zip) and macOS (dmg).
- **macOS is built, not tested:** there is no Mac to run it on here. It should work; tell us in an issue if it does
  not.
- The builds are not signed; INSTALL.md says what Windows and macOS show on the first start.
- Checksums (`SHA256SUMS` on that release; check with `sha256sum -c SHA256SUMS`):

```text
{{desktop_sha256sums}}
```

Each file lives only on its own piece's release; this page links to it and never holds a second copy.
