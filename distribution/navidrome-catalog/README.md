# Navidrome catalog entry for Worbus

`worbus/index.yaml` is Worbus's entry for the app catalog on [navidrome.org](https://www.navidrome.org/apps/). The
catalog lives in the `assets/apps/` folder of the `navidrome/website` repository; the files here are the copy that is
versioned and reviewed with the rest of Worbus. `server/tools/test_catalog_entry.py` checks the entry's shape.

**Do not open the pull request before the owner says so.**

## The gate

Navidrome's validator wants an open-source app's `repoUrl` repository to have **at least 15 stars**. `repoUrl` here
is the umbrella (`illuminator69/worbus`), so every star on it counts toward the one gate. It is not met yet: the
entry is made and checked except for the stars, and waits.

## The three images

The entry names `thumbnail.webp`, `screen1.webp` and `screen2.webp`. They sit in this folder next to `index.yaml`
once the screenshots are taken. WebP, at most 1200 px wide and 500 KB each. The thumbnail is an Android Home
screenshot and must not be a logo; the two gallery images are desktop screenshots. They pass the same privacy list
as the README's screenshots (no address, hostname, username, token or path).

## Steps, when the owner says go

1. Have 15 stars on `illuminator69/worbus`.
2. In a scratch clone of `navidrome/website` (from the owner's fork, which the owner's account makes then), copy this
   `worbus/` folder, with its three images, to `assets/apps/worbus/`.
3. Run Navidrome's own `npm install` and `npm run validate:app` there. Every check must pass, the star count included.
4. Open a pull request to `navidrome/website` titled "Add Worbus" and describe the app in a sentence. The entry
   says Worbus is a fork of Navic and Feishin and is not affiliated with Navidrome.
5. `macos` is added to `platforms` only once someone has run the desktop app on a Mac.
