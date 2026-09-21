This is a publish clone, not a working tree — nothing is developed here. The hub is copied in from
its own repo; both clients and lb-bot live in separate repositories and are only linked from here.

Docs split: `README.md` is the project description and stays short (what it is, screenshots,
repositories, where to go next). Architecture belongs in `docs/ARCHITECTURE.md`, the wire spec in
`PROTOCOL.md`, setup in `TESTING-SETUP.md`. Don't let architecture drift back into the README.

When picking up work, check the markdown by last-modified rather than reading in order. Update
these files only on a real architectural change, and keep them succinct.
