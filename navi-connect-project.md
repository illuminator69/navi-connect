---
name: navi-connect-project
description: "Spotify-Connect-style remote-control project for the user's Navidrome setup (Feishin + Navic forks + relay hub)"
metadata: 
  node_type: memory
  type: project
  originSessionId: cce9fc4a-821d-4d9d-b809-e26588172831
---

User is building **navi-connect**: a Spotify-Connect-style control layer over their
personal Navidrome (single-user, all on Unraid `media` Docker network). Goal:
real-time queue control + transfer-with-resume between clients, in-app (no web UI / no
bot required).

Architecture (decided): **central headless relay "hub"** (Python `websockets`/asyncio,
port **4790**) holds session intent (`queue,index,positionMs,isPlaying,activeDevice`);
each client is both controller + receiver; audio never flows through hub (receivers
stream from Navidrome by track id). Active receiver = source of truth for live position.

Clients = forks: **Feishin** (Electron, Windows) and **Navic** (Kotlin Multiplatform,
Android only — iOS out of scope). Key seams found in source:
- Feishin already ships a WS remote server (`src/main/features/core/remote/index.ts`,
  port 4333) — NOT reused (per-instance, single-song). Player store has
  `setQueue(songs,index,position)` = the transfer-load primitive.
- Navic uses `dev.zt64.subsonic:subsonic-client`; `MediaPlayerViewModel.syncPlayerWithState(PlayerUiState)`
  = its load+seek primitive; Ktor + media3 already deps. `seek()` takes normalized 0–1, not ms.

Decisions: port 4790; hub computes shuffle order; mirror to Navidrome `savePlayQueue` ON;
hub in Python; no iOS v1. Chromecast + local-output-device = Phase 2 (Cast via Navic
`media3-cast` as a bridged "virtual receiver").

Spec at `C:\Users\icher\navi-connect\PROTOCOL.md`. **Hub BUILT** at
`C:\Users\icher\navi-connect\hub\` (Python/asyncio/websockets, port 4790): hub.py,
Dockerfile, docker-compose.yml (separate container, `media` network), .env.example,
README, and tools/ (fake_receiver.py, controller.py, test_transfer.py). Navidrome URL
= https://music.example.com; savePlayQueue mirror ON (needs HUB_ND_USER/PASS). Transfer-
with-resume **verified PASS** via tools/test_transfer.py (resumes same index+position).
**Feishin fork now lives at `C:\Users\icher\navi-connect\feishin\`** (moved out of Temp).
Node/pnpm are NOT installed on the machine — user must install Node 20 LTS + `corepack enable`,
then `pnpm install` && `pnpm dev`. NOT yet typechecked here (no Electron toolchain in sandbox).
Engine + settings UI + device picker all BUILT, typecheck PASSES (pinned @types/react
19.2.17 in pnpm-workspace.yaml overrides to fix a duplicate-types error; pnpm 11.5 ignores
package.json pnpm.overrides). Node/pnpm now installed.

QUEUE-PUBLISH FIX (done): bug where Feishin sent only position reports, never its queue, so
the hub had 0 tracks → transfers carried empty queue and transfer-back hit `if(!songs.length)
return` so Feishin stayed paused. Fix: use-hub.tsx now publishes queue via `act setQueue`
(claims active, gated to active-or-nobody so it won't hijack); hub.py setQueue no longer echoes
do:load to the publishing device (active==sender → skip), avoiding reload loop. Verified by
tools/test_transfer.py (queue carries both ways, A resumes on transfer-back, no echo).

RESUME-RESET FIX (done): transfer-back to Feishin reset position to 0. Cause: do:load always
called setQueue, which rebuilds the queue with fresh song objects + reloads the audio source,
and setQueue's QUEUE_RESTORED seek fires after only 100ms so it races the reload and is lost.
Fix in use-hub.tsx handleDo 'load': if incoming queue ids == current local queue ids
(transfer-back case), DON'T reload — just mediaSeekToTimestamp(targetSec) (+ mediaPlayByIndex
if index differs) + play. Only full setQueue when the queue actually differs. Typecheck passes.

REMOTE-CONTROL VIEW (built, typecheck passes, pending user runtime test): when another device
is active, Playerbar swaps to RemotePlayerbar
(src/renderer/features/hub/components/remote-playerbar.tsx) showing remote now-playing
(title/artist/"Playing on <device>"), prev/play-pause/next routed to hub via act, and a seek
slider with live position that ticks locally at 250ms interpolating between the hub's ~1Hz
progress frames (hub.store remotePositionMs + remotePositionAt wall-clock snapshot).
hub.store extended with remoteQueue/remoteQueueIndex/remoteIsPlaying/remotePosition*,
selectors useHubIsRemoteActive/useHubRemoteNowPlaying/useHubActiveDeviceName. use-hub.tsx
now stores session/progress frames + publishes durationMs in tracks. playerbar.tsx renders
<RemotePlayerbar/> when useHubIsRemoteActive. Transfer-back via the device picker in the
remote bar. ROUND-2 FIXES (done, typecheck passes): (1) transfer-back-after-skip reset → pendingSeek ref
armed on do:load when index changes; applied in onCurrentSongChange +150ms (immediate seek is
lost during source reload). (2) auto-unpause on transfer → hub _transfer now defaults play to
session is_playing CAPTURED BEFORE release (release sets it False); device picker no longer
sends play:true; e2e test asserts paused transfer stays paused. (3) spacebar during remote →
use-playback-hotkeys.ts routes ALL transport hotkeys to hub via act when remote active
(sendRemote/remoteSeekBy/cycleRemoteRepeat helpers); falls back to local otherwise.
(4) UI parity → RemotePlayerbar rebuilt on playerbar.module.css 3-col grid: 60px thumbnail
(imageUrl now published via getItemImageUrl useRemoteUrl:true), shuffle/prev/play/next/repeat
(repeat cycles none→all→one, icons mediaRepeat/mediaRepeatOne), time labels, remote volume
slider (hub DeviceInfo now includes volume), device picker. hub.store adds
remoteRepeat/remoteShuffle/HubTrack.imageUrl/HubDevice.volume + selectors.
ROUND-3 FIXES (done, typecheck + hub e2e pass): root cause of "play=true still sent" was NOT
the hub — Feishin auto-unpaused after receiving a paused do:load (mediaPlayByIndex starts async
playback; the immediate mediaPause loses the race), then reported isPlaying:true, corrupting
session state so the NEXT transfer legitimately sent play=True. Fixes: (a) pendingSeek now
carries a `pause` flag — after the armed seek applies, mediaPause is re-asserted (+100ms);
(b) hub _transfer broadcasts session/devices BEFORE sending do:load so the target knows it's
active before load side effects fire; (c) hubDrivenUntil ref (2s window set in handleDo) marks
local player events as hub-driven. SPOTIFY-STYLE LOCAL PLAY ROUTING (built):
routeLocalPlayToRemote() in use-hub.tsx — if user starts local playback while another device
is active (and not within hubDrivenUntil), send local queue as act setQueue (hub loads it on
the ACTIVE remote device), sync lastQueueSig, mediaPause local. Wired into onPlayerStatus +
onCurrentSongChange with 1s dedupe (lastRoutedAt). ROUND-4 FIX (done, e2e passes): the REAL unpause culprit was a hub race — receivers tick
report{isPlaying:true} at 1Hz on their own socket; after act pause set is_playing=False, a
stale in-flight tick from the OTHER device's socket flipped it back to True, so the next
transfer captured play=True. (Same-device reports can't race acts — same socket, TCP-ordered;
cross-device ones can.) Fix: INTENT_GRACE=2.0s in hub.py — _mark_play_intent() on every act
that sets is_playing (play/pause/playpause/setQueue/transfer/next-at-end); _on_report ignores
CONTRADICTING isPlaying within the grace window (position/index still accepted; 'ended' exempt).
fake_receiver.py now also reports immediately on every state change like a real client.
e2e has a stale-report race regression test. Fake receiver doesn't auto-advance tracks
(sim limitation).
NAVIC CLIENT v1 BUILT (not compiled — no JDK/Android SDK on machine; user builds in Android
Studio). Navic fork moved to C:\Users\icher\navi-connect\navic. Files: (1) libs.versions.toml
+ ktor-client-websockets in ktor bundle; (2) PreferenceManager + hubEnabled/hubUrl/hubToken/
hubDeviceName/hubDeviceId prefs; (3) MediaPlayer.kt base + OPEN no-op hooks loadRemoteQueue/
setPlayerVolume/applyRemoteRepeat/applyRemoteShuffle (iOS untouched); (4) MediaPlayer.android.kt
overrides (loadRemoteQueue = unconditional setMediaItems+seekTo(idx,posMs)+prepare+play/pause +
uiState update — NOT syncPlayerWithState which bails if mediaItemCount>0; repeat = media3 ints
0=off,1=one,2=all); (5) HubManager.kt in domain/manager (commonMain) — full protocol engine:
Ktor WS client, hello/welcome (persists deviceId), do handlers (load resolves ids via
songDao.getSongsByIds — reorders to hub order — then toDomainModel), reporterLoop collects
mediaPlayer.uiState (posMs = progress*durationMs; immediate on index/pause change + 1Hz tick,
gated to active device), publishQueueIfOurs (claim-active w/ sig dedupe + hubDrivenUntilMs 2s
guard), release = final report then released, transfer(); StateFlows connected/devices/
myDeviceId/activeDeviceId; (6) ManagerModule single(createdAtStart) HubManager(...).start();
(7) Screen.Settings.NaviConnect + strings title/subtitle_navi_connect + MainScreen PageRow
(Icons.Outlined.Radio) + App.kt entry → NaviConnectScreen; (8) NaviConnectScreen.kt =
enable/url/token/name + Save&reconnect + device list with Transfer/Play-here buttons
(hardcoded EN strings deliberately). NAVIC v1 CONFIRMED WORKING by user (transfer both ways incl. paused, Feishin can control
Navic). NAVIC REMOTE-CONTROL VIEW BUILT (round 2, not yet compiled by user): HubManager +
RemoteTrack/RemoteSessionState (positionAtMs wall-clock snapshot), applySession() parses
welcome.session/session frames, progress frames update pos/index/isPlaying,
isRemoteActive StateFlow (combine connected/active/me, stateIn Eagerly), act helpers
actPlayPause/actNext/actPrevious/actSeek + currentTimeMs(). UI: DevicePickerSheet
(ui/components/sheets, ModalBottomSheet w/ transfer buttons), RemotePlayerBar
(ui/components/layouts — AsyncImage cover, title, "Playing on X", prev/playpause/next via
act, LinearProgressIndicator ticking 250ms via LaunchedEffect interpolation, tap→picker
sheet), mounted in RootBottomBar replacing MiniPlayer when isRemoteActive (same
graphicsLayer), + SheetActionButton (Icons.Outlined.Radio) in NowPlayingScreen actions
opening DevicePickerSheet. Remote view round 2 confirmed working by user except: expanded NowPlayingScreen showed frozen
local player after transfer → FIXED via auto-minimize (LaunchedEffect on isRemoteActive in
NowPlayingScreen pops NowPlaying/Queue/Lyrics/PlaybackSpeed from backStack; chosen over
building a full live remote now-playing screen). Also fixed a compile break where my
showDevicePicker insert had split the multi-line isPlayerCurrent boolean expression.
RemoteControlSheet BUILT (ui/components/sheets): tapping RemotePlayerBar now opens a full
Spotify-style remote view — header (art/title/artist/"Playing on X"), live seek Slider
(fraction-based, actSeek on release), prev/playpause/next, favorite (resolves track id via
songDao.getSongById→toDomainModel, songRepository.star/unstarSong — local, same Navidrome),
add-to-playlist (reuses PlaylistUpdateDialog(persistentListOf(song))), device picker button
(nested DevicePickerSheet), session queue LazyColumn (current highlighted, tap → actJump).
HubManager gained actJump(index). ROADMAP COMPLETED (final round, pending user build): (1) local-play routing —
HubManager.routeLocalPlayIfRemote() in reporterLoop (gates: connected, !paused, not
hubDriven, 1s dedupe, active!=me) sends act setQueue + pauses local. (2) queueChanged
reconcile — MediaPlayer reconcileRemoteQueue open hook; android impl rebuilds tail then head
around current media item (no restart), falls back to loadRemoteQueue if current id changed.
(3) CHROMECAST: media3-cast added to media3 bundle; manifest OPTIONS_PROVIDER meta-data →
CastOptionsProvider (Default Media Receiver — Navidrome URLs public via music.example.com so
chromecast can fetch); PlaybackService holds exoPlayer+castPlayer, SessionAvailabilityListener
swaps mediaSession.player (state carried: items/index/position/playWhenReady) — transparent
to MediaController so hub reports keep working while casting; toMediaItem now sets mimeType
(Cast converter throws without it); CastManager interface (commonMain) + NoopCastManager (iOS
DI) + AndroidCastManager (MediaRouter, main-thread handler, CALLBACK_FLAG_PERFORM_ACTIVE_SCAN,
devices StateFlow CACHED across discovery stops per user req); DevicePickerSheet: "Cast
devices" section, DisposableEffect starts discovery on open/stops on close (lazy, no menu
throttle). CAVEATS: downloaded tracks cast as file:// URIs → won't play on chromecast (stream
URLs fine); Chromecast is bridged by Navic (hub still sees Navic as active device, not the
TV); CastPlayer(CastContext) ctor may warn deprecated on media3 1.10. Chromecast build SUCCEEDED (user hasn't tested cast yet).

V2 ROADMAP defined by user, written to C:\Users\icher\navi-connect\ROADMAP-V2.md (phased,
grounded in source surveys). KEY FINDINGS: AudioMuse-AI-NV-plugin = standard Subsonic
getSimilarSongs2/getArtistInfo (so Navic radio == AudioMuse support, one feature);
Feishin ALREADY has smart playlists (query-builder → rules JSON → Navidrome native API,
create-playlist-form.tsx); Navidrome 0.62 native API accepts criteria rules (model/criteria,
fields incl. loved/rating/playcount/dateadded); home rows = getAlbumList2 frequent/newest.
Phases: P1 quick wins (F remote-bar queue/fav/rating; N star-ratings+Favorites rename; N
similar-songs radio; N home rows) → P2 (F metered transcode profile; N smart-playlist editor
via native API; N+F playlist downloads w/ rolling-vs-permanent cache + quality/format) → P3
(F chromecast via castv2-client in main process; full virtual-receiver hub model, protocol:
bridge-cast caps + bridgedBy + hub routing) → P4 (F synced lyrics in remote mode, low prio).
Sources extracted at /tmp/audiomuse + /tmp/nd-src (Temp — re-extract from Downloads zips if
wiped). PHASE 1 PROGRESS: Item 1 DONE (Feishin remote-bar queue popover [DropdownMenu, act jump] +
favorite heart + Rating stars via own mutations; HubTrack +favorite/rating; both Feishin
track publishers send userFavorite/userRating; typecheck ✅). AUDIOMUSE INDICATOR DONE:
Feishin = AUDIOMUSE_GRADIENT (#7c4dff→#00bcd4→#ff4081) + useIsAudioMuseDriven (autoDJ.enabled
&& strategy==similar) exported from playerbar-seek-slider.tsx, gradient on seek bar styles.bar
+ AudioMuseBadge chip in play-queue-list-controls (typecheck ✅). Navic = RadioManager
isAudioMuseMix (mix sig vs queue ids), MiniPlayer progress lerps #7C4DFF↔#00BCD4 via
rememberInfiniteTransition 4s reverse, QueueScreen header shows "AudioMuse mix" tinted.
Item 2 DONE: ratings ALREADY existed in Navic (SongSheet RatingRow + NowPlaying
songRating) — only renamed strings title_starred/option_sort_starred → "Favorites".
Item 3 DONE: SessionManager.fetchSimilarSongIds (raw getSimilarSongs2, salted token) +
SimilarEnvelope DTOs; RadioManager.startRadio(seedId, seedSong) resolves ids via songDao →
loadRemoteQueue; DI single; SongSheet +onStartRadio param/row (Icons.Outlined.Radio);
wired in SongRow + NowPlaying MoreButton. Works for song seeds; album/artist seeds work via
same endpoint (wire ArtistSheet/CollectionSheet later). NOT COMPILED (user builds).
REMAINING PHASE 1: item 4 Navic home rows (getAlbumList2 frequent/newest scrollable
categories + See all).
AUDIOMUSE COLORS CORRECTED to logo palette (periwinkle #93A2E8, pink #EE7B90, orange
#F5A661, navy #2E4057): Feishin bar = SOLID AUDIOMUSE_PINK (no gradient per user), badge
keeps gradient w/ navy text; Navic MiniPlayer animates periwinkle→pink→orange (0..2 segment
lerp, 6s reverse), QueueScreen label solid pink.
PHASE 2 item 5 DONE (typecheck ✅): Feishin metered transcoding — TranscodingConfigSchema
+meteredOnly; use-effective-transcode.ts (useIsMeteredConnection via navigator.connection
saveData/cellular/2g-3g + change listener; useEffectiveTranscode gates enabled by metered
when meteredOnly); all 4 engines (mpv-player-engine, web-player, wavesurfer-player,
playerbar-waveform) swapped from usePlaybackSettings().transcode to useEffectiveTranscode();
"Only when metered" Switch in transcode-settings.tsx. NOTE: navigator.connection on desktop
Chromium may not report cellular/saveData reliably — manual transcode toggle still works;
documented as best-effort.
SMART PLAYLIST EDITOR (Navic) DONE (not compiled): NativeApiManager (domain/manager) —
Navidrome NATIVE API: POST {base}/auth/login {username,password}→token; requests carry
X-ND-Authorization: Bearer; 401→relogin retry; createSmartPlaylist POST /api/playlist
{name,comment,public,rules}. SmartPlaylistEditorScreen (ui/screens/playlist): rule rows
(field/operator/value pickers; curated FIELDS incl title/artist/album/genre/year/rating/
playcount/loved/dateadded/lastplayed — artist+genre are dynamic tag fields, valid), ALL/ANY
segmented, sort+order+limit, builds criteria JSON {all/any:[{op:{field:val}}],sort,order,
limit}; on success dbRepository.syncPlaylists() + back. Entry: PlaylistCreateDialog gained
optional onSmartRequested → "Smart…" FormButton (only passed from PlaylistListScreen →
Screen.SmartPlaylistEditor route in App.kt). DI singleOf(::NativeApiManager).
BUGFIX ROUND (user field-tested chromecast — it works): (1) "Start radio" added to ALL
long-press SongSheet sites: SongRowDropdown.kt, SearchScreen.kt, song/components/Item.kt
(radioManagerItem) — previously only SongRow + NowPlaying MoreButton. (2) Cast-disconnect NPE
(DefaultMediaSourceFactory checkNotNull uri): CastPlayer timeline returns MediaItems WITHOUT
localConfiguration during session end → switchSessionPlayer now snapshots URI-bearing items
(lastLocalItems) before handing to cast, restores by mediaId on the way back, drops
URI-less leftovers, whole switch wrapped in try/catch. (3) Coil IllegalStateException on app
re-entry while service keeps process alive: App.kt setSingletonImageLoaderFactory guarded by
process-level imageLoaderFactoryInstalled flag.
PLAYLIST DOWNLOADS (Navic) DONE (not compiled): DownloadManager.downloadSong/execute/
downloadAudioFile now take (bitrate Int=0, format String?=null) → api.getStreamUrl(id,
bitrate, format) (lib already supported transcode params). PlaylistDownloadManager
(domain/manager, createdAtStart): @Serializable PlaylistDownloadPolicy{playlistId, name,
mode permanent|rolling, rollingLimit, maxBitRate(0=orig), format(""=orig), managed[]} stored
as JSON in Settings KEY playlistDownloadPolicies; syncPlaylist = dbRepository.
syncPlaylistSongs → playlistDao.getPlaylistById → ordered by crossRef.position → target
(all|first N) → evict managed-not-in-target (skips ids managed by other policies; never
touches manual downloads) → download missing at policy quality → managed updated. Schedule:
30s after start then every 6h + on setPolicy. UI: PlaylistDownloadDialog (Off/Permanent/
Rolling segmented + keep-N + quality Original/320/192/128 + format Original/opus/mp3 +
delete-on-disable switch); CollectionSheet +onAutoDownload param ("Auto-download…" row);
wired in collection/components/TopBar.kt only when collection is DomainPlaylist.
CAVEATS: transcoded files keep original filename/ext (plays fine, sniffed); quality change
doesn't re-download existing files; refresh only while app process alive (no WorkManager).
FEISHIN PLAYLIST DOWNLOADS DONE (typecheck web+node ✅): export-to-folder design (desktop
semantic). Main: src/main/features/core/downloads/index.ts — ipc downloads-select-directory
(dialog), downloads-sync {dir, files[{filename,url}], removeOthers} (sequential fetch→
pipeline to .part→rename, progress events 'downloads-progress', cancel flag, manifest
.feishin-downloads.json tracks OUR files so mirror-removal never touches foreign files);
registered in core/index.ts. Preload downloads.ts bridge (window.api.downloads). Renderer:
download-playlist-modal.tsx — folder picker (remembered per playlist in localStorage),
format Original/opus/mp3 + bitrate Original/320/192/128 (transcoded via controller.
getStreamUrl{transcode,bitrate,format,skipAutoTranscode}), Mirror switch (desktop
counterpart of Navic rolling: re-run syncs smart playlist folder), live Progress + cancel;
filenames `NNN - artist - title.ext` sanitized; trigger = download ActionIcon in
playlist-detail-song-list-header (isElectron-gated) next to LibraryHeaderMenu.
MIRROR FIX (user field-tested: removing 1 track caused 15 removed/14 re-downloaded — index
prefix in filenames shifted, mirror treated renames as departures): manifest now stores
{id, filename} entries (back-compat reads old string lists as id:""); departure decided by
SONG ID; position-only changes RENAME the existing file (managedById lookup) instead of
delete+redownload; toast shows "N renumbered". Typecheck web+node ✅.
PHASE 3 DONE (typecheck ✅, runtime untested): items 9+10 implemented TOGETHER as the
**Cast bridge** in Feishin main (src/main/features/core/cast/index.ts + castv2-client.d.ts
shim; deps castv2-client@1.2 + bonjour-service@1.4, protobufjs build approved in
pnpm-workspace allowBuilds). Every mDNS-discovered chromecast (_googlecast._tcp, passive
continuous browse) gets a CastDeviceBridge = its own hub WS connection (hello id
"cast-<txt.id>", name "📺 <fn>", platform chromecast, caps [receiver]) → appears in ALL
device pickers (Feishin AND Navic), transfer/resume generic, HUB UNCHANGED. Bridge: lazy
castv2 Client+DefaultMediaReceiver on first do:load; handles load/play/pause/seek/jump/
queueChanged/setVolume/release; local auto-advance on IDLE FINISHED (ended:true at queue
end); 1Hz getStatus position reports; hub reconnect 5s; follows hub settings via new
hubEvents EventEmitter + getHubConfig() exports in hub/index.ts. PROTOCOL ADDITION: track
meta now carries streamUrl + mime — Feishin buildHubTracks (async, getStreamUrl
skipAutoTranscode+transcode:false = instant URL; container→mime, mp3→audio/mpeg) used by
publishQueue + routeLocal (both now fire-and-forget async); Navic publishes
api.getStreamUrl(id) + song.mimeType at both publish sites. CAVEATS: chromecast must reach
streamUrl (music.example.com public ✓; LAN-only servers would fail); Navic-downloaded local
file paths never published (Navic streamUrl always server URL ✓).
CAST BRIDGE FIELD-TEST ROUND: user log showed the smoking gun — castv2-client methods REQUIRE
callbacks; my callback-less setVolume/play/pause/seek/stop threw "callback is not a function"
INSIDE the cast socket message handler → corrupted packet stream ("invalid wire type" storm)
→ heartbeat Device timeout; MaxListenersExceeded = 1Hz getStatus piling on dead channel.
FIXED: noop callbacks on ALL castv2 calls, statusInFlight guard on ticker, media controller
setMaxListeners(50), status-transition logging (playerState/idleReason/t/dur) to diagnose
whether receiver actually plays the FLAC. Note: bridge hardening round earlier added
electron-log everywhere + 8s connect timeout + fresh-session retry on load failure. User
also reported wanting remote-bar volume separated from local Feishin volume — by design it
IS separate (act volume → active device only); awaiting user clarification on what they saw.
CAST ROOT CAUSE FOUND (user): Feishin's server is configured via TAILSCALE IP
(http://100.86.181.124:4533) → published streamUrl unreachable from the Nest (load ok →
IDLE, no error). FIX: hub settings gained publicServerUrl (Feishin settings.store +
hub-settings.tsx "Public server URL" field); use-hub rewriteToPublic() rewrites origin of
streamUrl AND imageUrl in buildHubTracks (keeps pathname+search). User must set it to
https://music.example.com and REPLAY queue to republish. Earlier rounds fixed: castv2 callback
crash (noop callbacks mandatory!), load timeout+retry+result logging, contentId logging,
mDNS/ws lifecycle logging, statusInFlight guard.
CAST CONFIRMED FLAWLESS by user (auth was token mismatch, resolved). FINAL NAVIC ROUND
(not compiled): (1) HOME ROWS DONE — LibraryScreen + 2 AlbumListViewModels (Frequent/Newest,
keys libraryFrequentAlbums/libraryNewestAlbums), Content.kt +2 horizontalSections (reuse
Recent row pattern, existing strings option_sort_frequent/newest, See-all → Screen.AlbumList).
(2) ANIMATED MINIMIZE FIXED — root cause: NowPlaying is a ModalBottomSheet OverlayScene;
backstack removal destroys sheet instantly. Added NowPlayingSheetController (object in
NowPlayingScene.kt, hideRequests StateFlow) — scene LaunchedEffect does sheetState.hide()
then onBack(); NowPlayingScreen auto-minimize calls requestHide() instead of remove().
(3) PERF FIX — my reporterLoop/RadioManager joined ALL queue ids per 200ms uiState emission;
now reference-cached queueSig (uiState.copy keeps same queue list instance). (4) SYNC FIX —
SessionManager lib client now installs HttpTimeout (connect 15s/request 120s/socket 60s;
OkHttp default 10s read caused SocketTimeoutException via CF tunnel); DbRepository
syncLibrarySongs per-album: retry once on transient failure then SKIP (previously any
non-serialization exception aborted entire sync → new albums never inserted).
HOME ROWS CRASH+LAG FIX: my two extra AlbumListViewModels each ran getAlbumsByQuery loading
the ENTIRE library as AlbumWithSongs (all songs via @Relation) — 3 parallel full-library
loads on landing = jank + the Room crash. Fixed: SortUtils.toSqlQuery(limit: Int? = null)
appends LIMIT; AlbumRepository.getAlbumsLimited(listType, limit) lightweight top-N on IO;
LibraryScreen now uses plain remember/UiState states + runCatching loads of top-20
Frequent/Newest in the existing LaunchedEffect (heavy VMs removed). NOTE told user: perf
comparison vs "regular navic build" is debug-vs-release — Compose debug builds are
dramatically choppier; test with assembleRelease.
ROUND (Opus): FIXED 3 of 4: (#4) Navic artist list now ALBUM artists only —
DbRepository.syncArtists filters fetchAllArtists().filter{albumCount>0} (search3 returns
all 7k incl track artists). (#3) cast pause progress reset — onCastStatus + ticker now
ignore currentTime<=0 (transient rebuffer 0 was zeroing lastPositionMs before a pause
reported it). (#2 BIG) Feishin side-queue + lyrics now follow REMOTE playback: new
use-remote-aware.ts (useRemoteAwarePlayerSong synthesizes QueueSong from remoteNowPlaying +
currentServerId; useRemoteAwareTimestamp interpolates remote pos; useRemoteAwareStatus;
useRemoteSeek→act seek); RemotePlayQueue component swapped into PlayQueue when
useHubIsRemoteActive (tap row→act jump); lyrics.tsx uses remote-aware song; synchronized-
lyrics.tsx uses remote-aware status/timestamp/seek. Feishin typecheck PASSES (web+node).
INSTALL INCIDENT: my `pnpm run typecheck` triggered a deps re-check→reinstall that hit a
registry flake + pnpm11 blockExoticSubdeps gate; my `--force` recovery DROPPED transitive
electron-builder from the lockfile (minimumReleaseAge gate). Recovered: added
blockExoticSubdeps:false to pnpm-workspace.yaml + declared "electron-builder":"26.15.0"
(excluded version) in devDependencies (was undeclared/transitive — latent feishin-dev bug).
Install now clean, postinstall Done. To typecheck WITHOUT triggering deps re-check, run tsc
directly: node node_modules/.pnpm/typescript@5.9.3/node_modules/typescript/lib/tsc.js.
ROUND (remote UX): (#5) Navic album artists FIXED PROPERLY — albumCount filter failed
(search3 reports albumCount for track artists too); now SessionManager.fetchAlbumArtists()
= raw getArtists.view lenient parse (index[].artist[]), DbRepository.syncArtists uses it.
(#4) cast transfer-back reset — capturePosition now also guards currentTime>0 (was the last
0-source). (#3) Feishin add-to-queue while remote did nothing → player-context
addToQueueByData/ByFetch now route to enqueueToRemote when isRemoteSessionActive (new
src/renderer/features/hub/utils/remote-queue.ts: buildHubTracksForSongs w/ streamUrl+public
rewrite, addToQueueTypeToRemoteMode Play→now/next/end, enqueueToRemote sends act setQueue/
enqueue). (#1) remote-playerbar title→goToAlbum, artist→goToArtist (useRemoteSongNavigation
in use-remote-aware: getSongDetail resolves albumId/albumArtists[0].id → navigate). (#2)
RemotePlayQueue rows now have DropdownMenu (Go to album/artist, Play next, Add to queue via
resolve+enqueueToRemote) + clickable title/index→jump. Feishin typecheck web+node PASS.
Navic not compiled (no toolchain; follows patterns).
STILL OPEN: (#1-screenshot) Feishin track-list row overlap — virtualized-table row-height
vs content mismatch under display scaling; I did NOT touch the track table; likely upstream/
high-DPI; needs user's display-scale + Feishin zoom to diagnose. Also perf/lag: recommended
assembleRelease (debug Compose is choppy); made HubManager/RadioManager flows WhileSubscribed. Split: main = WS transport
(`src/main/features/core/hub/index.ts`, registered in core/index.ts; persists deviceId in
electron-store), preload bridge (`src/preload/hub.ts` + registered in preload/index.ts),
settings slice (HubSettingsSchema/useHubSettings in settings.store.ts, default url
ws://localhost:4790), renderer protocol hook (`src/renderer/features/hub/hooks/use-hub.tsx`,
mounted as <HubHook/> in audio-players.tsx). Maps do:load→getSongDetail per id→setQueue
(position in SECONDS), play/pause/jump/seek/setVolume/setRepeat/setShuffle/release; reports
position/index/playing ~1Hz only when active. Reuses Feishin's PlayerRepeat('none/all/one'
matches hub), PlayerShuffle(true→TRACK). Key APIs: usePlayerActions, usePlayerEvents
(onCurrentSongChange.index, onPlayerProgress.timestamp[sec], onPlayerStatus.status),
api.controller.getSongDetail. Still TODO Feishin: in-app settings UI panel (to toggle
enabled/url/token), device-picker + transfer-button UI, favorite/rating do-handlers.
Then Navic side.

Diagnosed aside: Navic showing only 450 artists is **not** a code limit — artist sync
does one unpaginated `getArtists()` with no `.take()`; 450 == what Navidrome's getArtists
returns (album-artists only, per Subsonic spec). Verify via `/rest/getArtists` curl.

UNIFIED-BAR ROUND (Feishin, typecheck web PASS, runtime untested by user): user hit a
startup bug — on `pnpm dev` Feishin auto-resumed LOCAL playback before the hub `welcome`
arrived, so routeLocalPlayToRemote bailed (activeId still null) and local ran away while the
bar showed remote mode (no reachable pause). Decided (user) to FULLY MERGE the remote views
into the normal player + remove BOTH AudioMuse visual indicators. Done: (1) AudioMuse badge
(play-queue-list-controls.tsx AudioMuseBadge) + seek-bar pink tint + useIsAudioMuseDriven/
AUDIOMUSE_* consts (playerbar-seek-slider.tsx) all REMOVED. (2) Startup race fixed in
use-hub.tsx: reconcileRemoteActive() pauses local if remote-active && playing && past
hubDrivenUntil, called from welcome + session handlers. (3) UNIFIED BAR via TRANSPORT
INTERCEPTION: remoteAct(action,extra) helper added to hub/utils/remote-queue.ts; PlayerContext
media* methods short-circuit to remoteAct when isRemoteSessionActive (next/pause/play/jump=
mediaPlayByIndex/previous/seek/playpause/volume/setVolume/inc-dec-volume via remoteActiveVolume()/
setRepeat/setShuffle/toggleRepeat=cycle/toggleShuffle) — so ALL existing controls drive the
session, no per-button edits. (4) DISPLAY made remote-aware: use-remote-aware.ts gained
useRemoteAwareShuffle/Repeat/Volume + favorite/rating now in synthesized song; swapped hooks in
left-controls (song+ItemImage src=imageUrl for remote cover), playerbar-slider (song+timestamp),
playerbar-seek-slider (timestamp), center-controls (status/shuffle/repeat/play-id), right-controls
(favorite/rating song + volume). (5) playerbar.tsx renders normal bar ALWAYS; remote-playerbar.tsx
DELETED. STILL PENDING the merge: RemotePlayQueue (play-queue.tsx still swaps to it when remote)
NOT yet folded into the virtualized queue table — deferred (table drag/activation semantics need
care + runtime validation first). KNOWN POLISH GAPS: favorite heart won't optimistically fill in
remote mode (reads hub-published state; mutation still applies); skip-fwd/back buttons not routed
to remote; mute icon uses local muted. Typecheck: run `.\node_modules\.bin\tsc.cmd --noEmit -p
tsconfig.web.json --composite false` (NOT `pnpm run typecheck` — triggers deps recheck).
NAVIC ARTISTS (#3) RESOLVED — was stale cache; the syncArtists()→fetchAlbumArtists()→getArtists.view
path was already correct, user just needed a full library refresh (login only reads the cached DAO).
Confirmed working by user.
CAST-BRIDGE RESTART RE-ADOPTION (Feishin main, node typecheck PASS, runtime untested): user
field-tested unified bar — found that with a Chromecast playing, restarting `pnpm dev` left the
bar showing the right song but PAUSED, the device "offline but playing" in the picker, and
transfer-back kept the cast playing with no auto-advance. ROOT CAUSE: on Feishin death the hub
keeps active_device_id = the cast bridge but sets is_playing=False (hub._disconnect; only a
`released` frame clears active); the physical Chromecast keeps streaming autonomously; the NEW
cast bridge reconnects but is DETACHED (castPlayer=null, tracks=[]) — it only attaches lazily on
the first do:load via ensureCast()→client.launch. So no live reports (bar paused), release's
castPlayer?.stop() is a no-op (keeps playing), and no IDLE/FINISHED status (no advance). FIX in
src/main/features/core/cast/index.ts: bridge now parses welcome/session frames; maybeAdopt(session)
— if session.activeDeviceId === our `cast-<id>` and we hold no castPlayer (once per connection via
`adopted` flag) — bootstraps tracks/index/positionMs from the session and calls adoptRunningSession():
client.connect→getSessions→find appId 'CC1AD845' (DefaultMediaReceiver)→client.join the RUNNING
session (not launch), attach status listener, getStatus to restore playing/position + re-sync index
by matching status.media.contentId to track.streamUrl, then report() (flips hub session back to
playing) + startTicker. castv2-client.d.ts shim extended with getSessions/join/CastSession + media.contentId.
Hub left unchanged (keeping active on disconnect is what enables re-adoption). NOTE: brief paused
flash on launch before adoption completes (settings push→mDNS→connect→join) is expected.
AUDIOMUSE CUSTOMIZATION (#4, Symfonium-style configurable recommendation algo, both players) =
NOT started; needs research into AudioMuse-AI real API beyond the getSimilarSongs2 shim.

FEISHIN QUEUE MERGE (done, typecheck web PASS): folded RemotePlayQueue into the normal PlayQueue.
play-queue.tsx now synthesizes the remote queue as QueueSong[] with sentinel _uniqueId `remote:<i>`
(from useHubStore remoteQueue) and feeds the SAME ItemTableList when useHubIsRemoteActive;
activeRowId=`remote:<index>`, enableDrag disabled while remote. player-context mediaPlay() routes a
`remote:`-prefixed id to remoteAct('jump',{index}) (double-click a remote row = hub jump). remote-
play-queue.tsx DELETED. KNOWN LIMITS (remote rows): context-menu go-to-album/artist + remove/move are
inert (synthesized song has no albumId/artistId; hub remove/move not wired); jump + add-to-queue/play-
next DO work (route via intercepted player context). useRemoteSongNavigation now unused (left in place).
NAVIC FULL ONE-PLAYER MERGE (done, NOT compiled — user builds; may need a fix round): mirrors the
Feishin unification. CENTRAL DISPLAY: MediaPlayer.kt (base MediaPlayerViewModel) now exposes
`localUiState` (raw local) + a blended `uiState` = combine(_uiState,_remoteState){remote?:local}
.stateIn(Eagerly); `setRemoteState(PlayerUiState?)` setter. HubManager.startRemoteMirror() (launched
from init) resolves the hub session (tracks→DomainSong via songDao, so coverArtId etc. work) into a
PlayerUiState and pushes it via setRemoteState while isRemoteActive (rebuild only when track-set/index
sig changes; 250ms ticker interpolates progress); pushes null when local. So the ENTIRE player UI
(MiniPlayer, NowPlaying, ArtworkPager, InfoRow, DurationsRow, lyrics, BlendBackground) mirrors the
remote session with NO per-leaf edits. HubManager internals (reporterLoop, handleDo seek/release)
switched to localUiState to avoid feeding remote state back. Added HubManager.actToggleShuffle/
actToggleRepeat. TRANSPORT routed to hub when isRemoteActive in the interactive components only:
MiniPlayer (swipe/playpause/next/seek), ButtonsRow (shuffle/prev/playpause/next/repeat), ProgressBar
(seek), ArtworkPager (page-settle→actJump), QueueScreen (row tap→actJump). RootBottomBar always shows
MiniPlayer (RemotePlayerBar swap removed); NowPlayingScreen auto-minimize REMOVED. RemotePlayerBar.kt +
RemoteControlSheet.kt DELETED (DevicePickerSheet kept; transfer-back = tap MiniPlayer→NowPlaying→Radio
action→DevicePicker). NO iOS edits (base change is commonMain; platform impls still write _uiState).
KNOWN LIMITS (Navic remote): queue remove/move/clear act on the LOCAL queue only (hub edit not wired);
favorite/rate/add-to-playlist apply locally to the same Navidrome (fine). NEXT: recommendation features
(#4, Symfonium-style configurable AudioMuse) — still not started.

FEISHIN AUTO DJ ON REMOTE (fixed, typecheck web PASS): Auto DJ never added songs while playing on a
remote device because use-auto-dj.ts subscribed ONLY to the local player store (usePlayerStoreBase),
which is frozen during remote playback — the add path (addToQueueByData) was already remote-aware, but
the trigger never fired. Fix: local callback now early-returns when isRemoteSessionActive(); added a
SECOND subscription to useHubStore (no subscribeWithSelector middleware, so manual `${nowId}:${remaining}`
sig dedupe) that, while remote-active and remaining<timing, resolves the remote now-playing id →full Song
via api.controller.getSongDetail (for genres/albumArtists/albumId), runs runAutoDjSongs/runAutoDjAlbumIds
deduped against the REMOTE queue ids, and enqueues via player.addToQueueByData/addToQueueByFetch(Play.LAST)
→ routes to hub enqueue('end'). remoteRunningRef guards re-entry. Albums-mode album dedupe is best-effort
(hub queue carries album names, not ids → empty queueAlbumIdSet). Self-limiting: after enqueue the hub
broadcasts a longer queue → remaining rises above timing.

STARTUP RUNAWAY-AUDIO REFIX (use-hub.tsx, typecheck web PASS): the reconcileRemoteActive() one-shot
pause (run on welcome/session) was insufficient — Feishin's local auto-resume starts the audio engine
ASYNC around/after `welcome`, so a single mediaPause loses the race (or local hadn't started playing yet
at welcome). Symptom recurred: on startup local audio played while the bar showed remote, and the bar's
pause drives the REMOTE session (transport interception), so the audible LOCAL engine couldn't be paused
without transferring to Feishin. Fix: (1) reconcileRemoteActive now re-asserts the pause after 150ms;
(2) added a continuous WATCHDOG in onPlayerProgress — while isRemoteActiveNow() && playing.current &&
past hubDrivenUntil, mediaPause() and skip report. onPlayerProgress only fires while local audio actually
advances, so it keeps re-pausing any runaway until it truly stops, then self-stops. No hijack on startup
(routeLocalPlayToRemote only fires on status CHANGE; startup autoplay before welcome already passed, so
the remote queue stays intact — confirmed by user's "queue topped up"). Normal local playback unaffected
(watchdog only acts when ANOTHER device is active).

NAVIC MERGE FIELD-TEST 1 (infinite-skip fix, NOT compiled — user builds): user reported after the
one-player merge: (a) Navic shows "nothing playing" while the hub knows Feishin is active; (b) transfer
Feishin→Navic landed on a different song; (c) transfer back → INFINITE SONG SKIPS on Feishin (log: react-
player rapidly loading every queue track, stream URLs on the TAILSCALE ip 100.86.181.124:4533). ROOT of
(c)+(b): removing the NowPlaying auto-minimize means NowPlayingScreen (+ ArtworkPager) stays mounted on
Navic as a REMOTE CONTROLLER; ArtworkPager's snapshotFlow{settledPage}→actJump(page) couldn't tell a
PROGRAMMATIC scroll (mirror syncing the pager as the remote advances) from a user swipe, so with any index-
space drift it looped: settle→actJump→Feishin skips→reports→mirror moves pager→settle→… FIX (ArtworkPager.kt):
gate the jump on a real user drag — track pagerState.interactionSource for DragInteraction.Start; settledPage
handler only fires actJump/playAt when wasUserSwipe (also fixes latent local risk). ROOT of (a) [STILL OPEN]:
the mirror + loadRemoteQueue resolve the hub queue via songDao.getSongsByIds and DROP tracks not in Navic's
local DB → empty/short queue (nothing playing) + shifted index space (wrong-song transfers when songs are
missing). If all songs are synced locally it aligns; otherwise need a metadata-fallback DomainSong built from
RemoteTrack (title/artist/album/durationMs/imageUrl; coverArtId=id) to keep display + index alignment without
the local DB. DomainSong has many required fields (DomainExplicitStatus enum, DomainContributor list) — synth
helper is non-trivial; deferred pending user retest of the skip fix (the storm likely caused (b), maybe masked (a)).

NAVIC FIELD-TEST 1 RESULT: infinite-skip FIXED (ArtworkPager user-drag gate worked); "nothing playing"
was a non-issue (songs were synced). NEW ISSUE: Android system media controls (notification/lockscreen)
only drove the LOCAL player, not the remote session.
ANDROID MEDIA CONTROLS → REMOTE (built, NOT compiled — user builds; EXPECT a compile pass, SimpleBasePlayer
API specifics are easy to get slightly wrong): the system controls talk to the media3 MediaSession whose
player is the local ExoPlayer; the blended uiState is UI-only and invisible to the session. For play/pause
to even reflect correctly (not just send 'play'), the session player must REPORT remote state — a thin
ForwardingPlayer is insufficient. SOLUTION: new RemoteSessionPlayer.kt (androidMain) = a SimpleBasePlayer
facade over HubManager.remoteSession: getState() builds playlist (MediaItemData from RemoteTrack title/
artist/album/imageUrl/durationUs) + currentMediaItemIndex + playWhenReady=isPlaying + extrapolating
contentPosition; handleSetPlayWhenReady→actPlay/actPause; handleSeek→actNext/actPrevious/actJump/actSeek;
invalidateState() on remoteSession changes + 1Hz tick. Only transport commands advertised (no media-item
editing). HubManager gained actPlay()/actPause(). PlaybackService: injects HubManager, creates remotePlayer,
collects isRemoteActive → swaps mediaSession.player to remotePlayer (and exoPlayer.pause()) when remote,
back to exoPlayer when local; releases it in onDestroy. HubManager.routeLocalPlayIfRemote() now early-returns
when isRemoteActive (the facade mirrors remote into the local controller's state; re-routing that mirror
would echo the queue / pause the remote). ARCHITECTURAL CONFLICT handled: the AndroidMediaPlayerViewModel
uses a MediaController on the same session, so when remotePlayer is swapped in, the VM's controller-listener
sees remote state — harmless because reports are gated by isActiveDevice(), publish by active==me, and route
is now gated off. KNOWN LIMITATIONS (told user): (1) starting BRAND-NEW local playback on Navic while another
device is active is blocked (facade rejects setMediaItems) — must transfer to Navic first; (2) VM's
onMediaItemTransition skip-on-unavailable could misfire on the facade if offline (online = fine); (3) transfer-
BACK relies on the isRemoteActive→false swap landing before do:load's setMediaItems — needs runtime check.

FEISHIN HIGH-DPI ROW OVERLAP (the long-open #1-screenshot issue) — FIXED (CSS, untested visually):
user on Windows 200% scaling, 2880x1920 (logical 1440x960) — side queue + track table rows overlapped
(stacked title/artist + wrapped genre spilling into rows below; worst far down the list and in the narrow
side queue). ROOT: the stacked title/artist columns' `.text-container` used `grid-template-rows: 1fr 1fr`;
grid tracks default to min-height:auto, so when line-heights exceed the row's share (compact 40px rows give
~20px/line; narrow columns make the artist wrap) the grid grows past --row-height and overflows into the
next virtualized row (rows are absolutely positioned at fixed offsets, so excess content overlaps rather
than pushing down). FIX in title-combined-column.module.css + title-artist-column.module.css: tracks →
`minmax(0, 1fr) minmax(0, 1fr)` + `min-height: 0` + `overflow: hidden` on .text-container, so the two lines
always clip to the row height. NOTE: did NOT add overflow:hidden to the generic `.container` (its drag-over
indicator uses a left/right:-9999px ::after that clipping would break); generic genre/album cells line-clamp
(2 default / 1 compact / 3 large) and appear to fit. IF overlap persists after this, suspect virtualization
row-position drift (sub-pixel row heights accumulating at DPR=2) rather than content overflow.

FIELD-TEST 2: media controls WORK now. Two follow-ups:
(a) ROW OVERLAP still present — my first CSS fix (minmax + overflow) was INEFFECTIVE because .text-container
had no definite height: parent .title-combined uses align-items:center so the block sized to CONTENT, making
minmax(0,1fr) resolve against auto height (no clamp). FIXED PROPERLY: added `height: 100%; align-self: stretch`
to .text-container in title-combined-column.module.css + title-artist-column.module.css (so the 1fr tracks +
overflow:hidden clamp to the row). NOTE: CSS-module HMR can be flaky — user should hard-reload / restart pnpm dev.
(b) STARTING NAVIC UNPAUSED FEISHIN (Feishin is the active device): only a `play`/`playpause`/`setQueue(play)`
act reaches the hub→Feishin do:play. Navic restores PAUSED so its report/publish/routeLocalPlayIfRemote
shouldn't fire → suspected the RemoteSessionPlayer facade: on swap-in, media3/system controller re-syncs and
calls setPlayWhenReady(true) → actPlay() → hub → Feishin plays. DEFENSIVE FIX: RemoteSessionPlayer.onActivated()
sets a 1500ms grace; handleSetPlayWhenReady/handleSeek no-op during grace; PlaybackService calls rp.onActivated()
right before `session.player = rp`. If the unpause persists, get Navic logcat filtered to HubManager to see the
exact act Navic emits on startup (could instead be routeLocalPlayIfRemote during the isRemoteActive stateIn lag
— its gate reads isRemoteActive.value which lags; if so, change that gate to compute active!=me synchronously).

FIELD-TEST 3: row-overlap + Navic-startup-unpause BOTH fixed. NEW: while casting from Feishin, the cast
progress bar eventually disconnected + an uncaught main-process exception. ROOT: the Chromecast TCP socket
died mid-playback (coincident with hub WS closing code 1011); castv2 Client.send reads the now-null socket
and throws SYNCHRONOUSLY inside the 1Hz status ticker's setInterval callback (not wrapped) → uncaught
main-process exception; castPlayer stayed non-null so the ticker kept firing into the dead client and live
reporting froze. FIX (cast/index.ts): wrapped getStatus in try/catch in startTicker AND capturePosition;
on ticker failure → teardownCast() + tryReadoptAfterDrop() (reuses adoptRunningSession to re-join the still-
playing cast and resume live reporting; guarded by reconnectingCast flag; captures wasPlaying BEFORE
teardown since teardownCast clears this.playing; if cast truly gone, getSessions finds no DefaultMediaReceiver
and it quietly no-ops). node typecheck PASS. (handleDo transport calls already covered by handleDo's outer
try/catch; load path already retries-with-fresh-session.) The hub WS 1011 itself is separate (hub-side close;
bridge reconnects in 5s) — not addressed.

FIELD-TEST 4: cast bridge fix works. REMOTE-BAR LINKS FIX (typecheck web PASS): in the unified playerbar
the remote now-playing album link went to home + artists weren't clickable, because the synthesized remote
QueueSong (useRemoteAwarePlayerSong) had no albumId and empty-id artists (left-controls links use
currentSong.albumId / artists[].id). FIX: useRemoteAwarePlayerSong now resolves the full Song once per remote
track id via api.controller.getSongDetail (module-level remoteDetailCache + remoteDetailInflight dedupe across
the ~4 components that call the hook), stored in a useState; merges albumId/artists/albumArtists into the
synthesized song (hooks moved before the early-return to respect rules-of-hooks). 
BUILD INSTRUCTIONS (told user): Feishin = electron-vite + electron-builder. Windows installer:
`pnpm run package:win` (runs `pnpm run build` = electron-vite build + remote vite build, then electron-builder
--win) → output in dist/ (NSIS .exe installer + win-unpacked/). Unpacked-only (no installer): `pnpm run
package:dev` (electron-builder --dir) → dist/win-unpacked/Feishin.exe. electron-builder 26.15.0 is declared
(latent feishin-dev bug we fixed earlier); blockExoticSubdeps:false in pnpm-workspace.yaml.

FEISHIN INSTALLER/WHITE-SCREEN: abandoned the NSIS installer (Defender quarantines the unsigned 223MB exe
during NSIS temp-extraction even with install-folder exclusions). The PORTABLE dist/win-unpacked/Feishin.exe
WORKS fine — that's the path forward. The earlier "white screen" was specific to the installed copy / electron
path, NOT the build (build is good; renderer crashed pre-JS only in that broken install). Diagnosed via remote
debugging (--remote-debugging-port=9222 + chrome://inspect; CDP showed empty-url crashed renderer). Left
PERMANENT error-only renderer logging in src/main/index.ts (did-fail-load / render-process-gone / preload-error
+ loadFile .catch) so future blank-window issues leave a trace; removed the noisy TEMP console-message/startup
logs. node typecheck PASS.
TRANSFER INDEX-MISALIGNMENT FIX (Navic, NOT compiled — user builds): user reported transfer Feishin→Navic
showed song 1 on the hub/display but Navic played song 2 (off-by-one). ROOT: HubManager.handleDo 'load'/'queueChanged'
used resolveSongs(ids) = songDao.getSongsByIds → DROPS songs missing from Navic's local DB, then applied the
hub `index` to the SHORTENED list → if a song before the index isn't synced locally, every later index shifts
(hub song0 missing → resolved[0]=hub song1 → index 0 plays song 2). FIX: new resolveQueue(tracks: List<JsonObject>)
resolves 1:1 preserving hub order AND length — missing songs become placeholder DomainSongs synthesized from the
hub track metadata (remoteTrackToDomainSong; coverArtId=id + getStreamUrl(id) so they still PLAY + show art via
the same Navidrome). load + queueChanged now use resolveQueue. Imports added: DomainExplicitStatus, Duration.
Companion.milliseconds. NOTE: the remote-display mirror (startRemoteMirror) still uses resolveSongs(ids) — same
drop-shift could affect remote now-playing display if a song is un-synced; harden with a RemoteTrack→placeholder
path if it recurs.

AUDIOMUSE TIER-1 WORK STARTED (see [[audiomuse-api]] + DESIGN-adaptive-audiomuse.md). Confirmed
user's Navidrome = 0.62.0, implements getSimilarSongs2 + getSonicSimilarTracks + findSonicPath
natively (sonic ones need the AudioMuse plugin loaded; probe via getOpenSubsonicExtensions →
"sonicSimilarity"). INCREMENT 1 = NAVIC AUTOPLAY (Off/Similar), NOT COMPILED (user builds in
Android Studio): new AutoplayMode enum (Off/Similar/Fingerprint/Adaptive — only Off/Similar
functional/shown; F+A reserved for Tier 2); PreferenceManager.autoplayMode; MediaPlayer.kt base
open fun appendToQueue(songs) + MediaPlayer.android.kt override (controller.addMediaItems + uiState
update, mirrors addToQueue); RadioManager gained ctor deps (preferenceManager, hubManager) + init
observeAutoplay() — collects mediaPlayer.localUiState, when mode==Similar && !paused && repeatMode==0
&& !hubManager.isRemoteActive && remaining<3 (sig=currentId:remaining dedupe) calls topUpSimilar()
= fetchSimilarSongIds(seed,30) - existing queue ids → songDao.getSongsByIds → toDomainModel →
take 15 → appendToQueue (uses EXISTING getSimilarSongs2, no new endpoints); ManagerModule
RadioManager now single(createdAtStart=true) with 5 get()s (Koin resolves by type). UI: Autoplay
SettingSelectionRow added to PlaybackScreen.kt (items Off/Similar, hardcoded labels). NOTE: `.collect{}`
used without explicit import, mirroring sibling MediaPlayer.kt (resolves in this module). Remote-mode
autoplay (top up the REMOTE session like Feishin) deliberately deferred — gated OFF when remote.
INCREMENT 2 = NAVIC ENGINE + SONG JOURNEY (NOT compiled): SessionManager gained 3 endpoints
(fetchOpenSubsonicExtensions, fetchSonicSimilarTrackIds id/count, findSonicPathIds startId/endId/count)
+ DTOs (SubsonicExtensionsEnvelope, SubsonicSonicMatchEnvelope w/ SonicMatchDto{entry:RawSongId,
similarity}), all in the existing self-contained salted-token style. RadioManager: + capability
probe `sonicSimilarityAvailable` StateFlow (observeCapabilities collects sessionManager.isLoggedIn,
probes getOpenSubsonicExtensions for "sonicSimilarity"); + private fetchSimilar(seedId,count) that
PREFERS getSonicSimilarTracks when available (guaranteed AudioMuse) else falls back to
getSimilarSongs2 — now used by BOTH startRadio and autoplay topUpSimilar; + startJourney(fromId,toId)
= findSonicPathIds → resolve via songDao → loadRemoteQueue (reuses mixSig so AudioMuse indicator
lights). SONG JOURNEY UI: SongSheet +onStartJourney param + "Journey to this song" row (reuses
Outlined.Radio icon — no path icon in the valkyrie set); wired in SongRow only so far — gated on
sonicAvailable && a different currentSong (start=now-playing, end=tapped song). Other SongSheet call
sites (Item.kt, SearchScreen, SongRowDropdown, NowPlaying MoreButton) don't pass onStartJourney yet
(nullable param → no row shown). NOTE: getSonicSimilarTracks(artistId) for artist-radio seeds returns
empty → fetchSimilar falls back to getSimilarSongs2 (no regression). INCREMENT 3 = FINISH NAVIC SURFACES (NOT compiled): (a) Song Journey now in ALL song long-press
sheets — wired onStartJourney in SongRow (done earlier), SongRowDropdown, SearchScreen, Item.kt.
Other 3 sites use an import-free `.value` gate: `player.uiState.value.currentSong?.takeIf {
radioManager.sonicSimilarityAvailable.value && it.id != song.id }?.let { now -> { radioManager.
startJourney(now.id, song.id) } }`. (Item.kt had no player → added playerItem via FQN
org.koin.compose.koinInject<paige.navic.shared.MediaPlayerViewModel>(). NowPlaying MoreButton
deliberately NOT wired — it's the now-playing song, gate is always null.) (b) ARTIST RADIO added:
ArtistSheet +onStartRadio param + "Start radio" row (Outlined.Radio), wired in 3 call sites
(ArtistListScreen, artist/components/DetailTopBar [state.artist.id], starred/components/Content) as
`radioManager.startRadio(artist.id)` — no gating (always works; getSonicSimilarTracks(artistId) empty
→ fetchSimilar falls back to getSimilarSongs2 = artist radio). Each needed +import
paige.navic.domain.manager.RadioManager + a koinInject<RadioManager>() in the right composable scope.
ALBUM RADIO DEFERRED: AudioMuse plugin does song+artist similarity, not album seeds; getSimilarSongs2
on an album id is unreliable → would risk empty results (CollectionSheet untouched).
TIER 1 CONFIRMED WORKING BY USER (autoplay Off/Similar, Song Journey everywhere, Artist radio, Route
icon). KNOWN COSMETIC ISSUE (fix later): the navi-connect device-picker button in NowPlaying
(SheetActionButton, Icons.Outlined.Radio) uses the SAME icon as "Start radio" — needs a distinct icon.
JOURNEY ICON DONE: ic_route.svg added to valkyrieResources/outlined → Icons.Outlined.Route, used in
SongSheet Journey row (Start radio keeps Radio).
FEISHIN PARITY INCREMENT 1 = CAPABILITY PROBE + PREFER-SONIC (typecheck web PASSES). Done the
IDIOMATIC Feishin way (not a custom probe): Feishin already maps OpenSubsonic extensions → ServerFeatures
in SubsonicController.getServerInfo. Added: (1) SubsonicExtensions.SONIC_SIMILARITY='sonicSimilarity'
(subsonic-types.ts) + ServerFeature.SONIC_SIMILARITY='sonicSimilarity' (features-types.ts); (2)
SubsonicController.getServerInfo sets features.sonicSimilarity=[1] when the ext is advertised — and
navidrome-controller.getServerInfo MERGES ...subsonicArgs.features so Navidrome servers get the flag
too (the user's server is Navidrome → uses navidrome-controller!); (3) typed contract endpoints
getSonicSimilarTracks + findSonicPath added to subsonic-api.ts with schemas sonicSimilarTracksParameters/
sonicPathParameters/sonicMatches ({sonicMatch:[{entry:song,similarity}]}) in subsonic-types.ts ssType;
(4) PREFER-SONIC: BOTH SubsonicController.getSimilarSongs AND navidrome-controller.getSimilarSongs now,
when hasFeature(server, SONIC_SIMILARITY), call getSonicSimilarTracks first (try/catch fallback to
getSimilarSongs). This makes track radio (play-track-radio-action) + Auto DJ (use-auto-dj.ts →
controller.getSimilarSongs) AudioMuse-powered. Album/artist radio use getSimilarSongs/getSimilarSongs2
directly (unchanged, like Navic). NOTE: feature flag populates when getServerInfo refreshes (server
add/auth) — user may need to reconnect. FEISHIN PARITY INCREMENT 2 = SONG JOURNEY (typecheck web + node PASS) — FEISHIN TIER 1 NOW COMPLETE.
Added getSonicPath through the typed controller: domain-types ControllerEndpoint + InternalControllerEndpoint
gained `getSonicPath?` (OPTIONAL — so jellyfin omits; GeneralController's Required<> makes the public
wrapper required/callable) + SonicPathArgs/SonicPathQuery{count?,endSongId,startSongId}; controller.ts
public getSonicPath wrapper (dispatch via apiController); SubsonicController.getSonicPath impl
(ssApiClient.findSonicPath → ssNormalize.song over res.body.sonicMatch); navidrome delegates
`getSonicPath: SubsonicController.getSonicPath`. UI: new play-track-journey-action.tsx (single
ContextMenu.Item, leftIcon "arrowLeftRight", i18n player.sonicJourney defaultValue "Journey to this
song") — gated: hasFeature(SONIC_SIMILARITY) && a current playing song (usePlayerStore getCurrentSong)
that's != the target; on select fetches getSonicPath(start=currentSong.id, end=song.id) →
player.addToQueueByData(Play.NOW). Wired into song-context-menu.tsx after PlayTrackRadioAction.
KNOWN LIMITS: uses LOCAL current song (not remote-aware blended like Navic) — minor edge case when
playing remotely; only on the song context menu (not album/artist). NEXT: Tier-2 (AudioMuse core API:
adaptive Mood Flow, Sonic Fingerprint autoplay, chat/mood search) per DESIGN-adaptive-audiomuse.md.
Typecheck: .\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.web.json --composite false (+ tsconfig.node.json).

TIER 2 STARTED — NAVIC FIRST (chosen because: no CORS like Feishin's renderer, native Ktor; and the
AutoplayMode.Fingerprint enum slot already existed). Verified contract: AudioMuse core
`GET /api/sonic_fingerprint/generate?n=&navidrome_user=&navidrome_password=` w/ header
`Authorization: Bearer <API_TOKEN>` → returns top-level JSON array `[{item_id, title, ...}]` (item_id =
Navidrome song id); core sets NO CORS + enforces Bearer via before_request guard. TIER 2 INCREMENT 1 =
NAVIC SONIC FINGERPRINT AUTOPLAY (NOT compiled): (1) PreferenceManager audioMuseUrl/audioMuseToken prefs;
(2) NEW AudioMuseManager.kt (domain/manager, singleOf DI) — Ktor client, Bearer auth, tight timeouts
(8s connect/30s), FAIL-SOFT (isConfigured + every call returns [] on missing-config/error/timeout);
fetchSonicFingerprintIds(count) GETs the endpoint passing navidrome user/pass from settings, parses
List<FingerprintItem{@SerialName item_id}>; (3) RadioManager gained audioMuseManager ctor dep (now 6;
DI updated) — observeAutoplay now allows Similar+Fingerprint; topUpSimilar renamed topUp(mode,seed,queue):
Fingerprint→audioMuseManager.fetchSonicFingerprintIds, else→fetchSimilar; sig now includes mode;
(4) PlaybackScreen shows Fingerprint in the selector ONLY when audioMuseManager.isConfigured; (5)
NaviConnectScreen got an "AudioMuse-AI (Tier 2)" section (URL + API token fields + Save AudioMuse).
CAVEAT: fingerprint is habit-based (not seed) so long sessions may deplete new picks (acceptable).
NAVIC SONIC FINGERPRINT CONFIRMED WORKING BY USER.
TIER 2 INCREMENT 2 = NAVIC ADAPTIVE "MOOD FLOW" (Yandex-style, NOT compiled). Verified contract: POST
/api/alchemy {items:[{op:ADD|SUBTRACT,id,type:song}], n, temperature?, subtract_distance?} (Bearer) →
{results:[{item_id,...}], filtered_out, centroid_2d}. Built: (1) AudioMuseManager.fetchAlchemyMixIds(
addIds,subtractIds,count) — POST alchemy (ktor post/setBody/contentType json), needs ≥1 ADD, fail-soft
→[]; + DTOs AlchemyRequest/AlchemyItem/AlchemyResponse{results:List<FingerprintItem>}. (2) RadioManager
Mood Flow: in-memory session state moodAddIds/moodSubtractIds (ArrayDeque, recency cap MOOD_MAX=12) +
moodPrevSongId/moodPrevProgress; captureMoodSignal(state) runs every emission when mode==Adaptive —
on track transition, prev song's last progress ≥0.85 → ADD (play-through), ≤0.20 → SUBTRACT (skip),
middle neutral; recordMood moves id to recent end, dedupes across sets; resetMoodFlow on Off. topUp
Adaptive branch → fetchAlchemyMixIds(add (cold-start=current song if empty), subtract); sig includes
mode. (3) AutoplayMode.Adaptive label → "Mood Flow". (4) PlaybackScreen shows Fingerprint+Adaptive when
audioMuseManager.isConfigured. KNOWN LIMITS (v1): APPEND-based not discard-tail-splice (adapts at
top-up time, not instant pivot — design's aggressive re-splice is a follow-up); LIKES not yet captured
as ADD (only skip/play-through); character presets (Echo/Steady/Transition → temperature/subtract_distance)
not wired (uses alchemy defaults); mood state in-memory (resets on restart/Off); no visualizer yet.
NAVIC EXTENDED-PLAYER AUTOPLAY SELECTOR (NOT compiled, user request): new
NowPlayingAutoplaySelector.kt (nowPlaying/components/controls) — a rounded (RoundedCornerShape 20dp)
frosted "button" (translucent surfaceVariant@0.4 + subtle border over the dynamic blurred
BlendBackground = expressive frosting; true backdrop blur would need Haze which Navic doesn't use) with
PlaylistPlay icon + "Autoplay · <mode.label>" + chevron. Modes Off/Similar [+Fingerprint/Adaptive when
audioMuseManager.isConfigured]; local mutableStateOf mirrors preferenceManager.autoplayMode. Per user
follow-ups, the picker is NOT a Material DropdownMenu but a custom Popup (androidx.compose.ui.window.Popup
+ PopupPositionProvider centering it under the button) styled IDENTICALLY to the button (shared frost,
RoundedCornerShape 20dp, hairline border); items are fillMaxWidth + TextAlign.Center, selected mode
tinted primary. REFINEMENTS: frost bumped to surfaceVariant@0.82 ("a lot more frost", both button +
popup share it); EXPRESSIVE open/close = AnimatedVisibility(MutableTransitionState) scaleIn(spring
DampingRatioMediumBouncy/StiffnessMediumLow)+fadeIn / scaleOut(spring StiffnessMedium)+fadeOut,
transformOrigin top-center (grows down out of the button); Popup kept mounted while currentState||
targetState so EXIT animates. TOGGLE/REOPEN FIX: tapping the open button fired the popup's outside-tap
dismiss then the click reopened it → added justDismissed flag (set in onDismissRequest, cleared after
250ms via LaunchedEffect+delay); button onClick `if(!justDismissed) expanded=!expanded`. STILL not true
backdrop blur (would need Haze lib, which Navic doesn't bundle) — offered. Wired into ControlsRow.kt between NowPlayingInfoRow and
NowPlayingProgressBar (Spacers 12dp, align CenterHorizontally) — shows in portrait AND landscape.
HAZE DEFERRED to the visualizer/adaptive-mood-background phase (user decision) — not added yet.
TIER 2 INCREMENT 3 = NAVIC MOOD FLOW CHARACTER PRESETS (NOT compiled). AudioMuse alchemy defaults:
temperature=1.0, subtract_distance=0.2 angular. Built: (1) MoodCharacter enum (domain/models/settings)
{EchoMatch 0.5/null, SteadyVibes 0.6/0.35, TransitionMaestro 1.6/null} (temperature, subtractDistance;
null=server default); Symfonium-style names; only affects Tier-2 Adaptive (Tier-1 endpoints take no
tuning). (2) PreferenceManager.moodCharacter (default SteadyVibes). (3) AudioMuseManager.fetchAlchemyMixIds
gained temperature/subtractDistance params; AlchemyRequest gained nullable temperature +
@SerialName("subtract_distance") subtractDistance — OMITTED when null (encodeDefaults=false) so server
defaults apply. (4) RadioManager Adaptive top-up passes preferenceManager.moodCharacter's temp/subtractDist.
(5) PlaybackScreen "Mood Flow character" SettingSelectionRow (MoodCharacter.entries) shown when
audioMuseManager.isConfigured.
TIER 2 INCREMENT 4 = NAVIC ADAPTIVE VISUALIZER v1 (NOT compiled, DEP-FREE — Haze split out to next step
to isolate dependency-version risk vs CMP 1.11.0). (1) NEW AdaptiveMoodBackground.kt (nowPlaying/
components/controls) — fluid aurora: 3 drifting radial-gradient blobs (AudioMuse palette periwinkle/pink/
orange over navy) animated via rememberInfiniteTransition (3 animateFloat 9/11/15s Reverse) on a Canvas
with Modifier.blur(90.dp); private DrawScope.blob() extension (local fun can't see DrawScope receiver).
(2) RadioManager now exposes reactive autoplayMode StateFlow + setAutoplayMode(mode) (sets pref + flow);
observeAutoplay reads _autoplayMode.value. (3) NowPlayingAutoplaySelector + PlaybackScreen autoplay row
now READ via collectAsState(radioManager.autoplayMode) + WRITE via radioManager.setAutoplayMode (so the
bg reacts live). (4) NowPlayingScreen: collects autoplayMode; shows AdaptiveMoodBackground when
==Adaptive, else BlendBackground (gated with `&& autoplayMode != Adaptive`). v1 LIMITS: palette/motion are
fixed (AudioMuse brand + gentle drift), NOT yet mood/energy-reactive (later: map alchemy centroid_2d →
palette, energy → motion). NOTE: deep-indented multiline Edits kept failing on tab/space matching — use
SUBSTRING old_strings without leading whitespace (Kotlin is whitespace-insensitive so resulting indent
imperfections are harmless).
HAZE DECISION: Haze stable (1.7.x) only supports CMP ≤1.9.3; CMP 1.11.0 needs Haze 2.0.0-alpha (refactored
API, pre-release) — too risky. USER CHOSE "build & check without Haze first": the autoplay chip is already
translucent (0.82) over the 90dp-blurred AdaptiveMoodBackground = frosted-glass already in Mood Flow. No
Haze code added. Revisit Haze 2.x only if the look falls short / when it stabilizes.
TIER 2 INCREMENT 5 = FEISHIN AUDIOMUSE FOUNDATION (typecheck web+node PASS). Core API has NO CORS +
Feishin renderer webSecurity gated by ignore_cors → routed via MAIN PROCESS. (1) settings.store: new
AudioMuseSettingsSchema {url, token} registered in ValidationSettingsStateSchema + initialState +
useAudioMuseSettings() hook. (2) main/features/core/audiomuse/index.ts: ipcMain.handle
'audiomuse-fingerprint' (GET sonic_fingerprint, Bearer, optional nd_user/pass query) + 'audiomuse-alchemy'
(POST alchemy {items ADD/SUBTRACT, n, temperature?, subtract_distance?}, Bearer) — both use global fetch
(main has it; downloads feature precedent), return item_id[] , fail-soft []; renderer passes ALL config
per-call (nothing persisted in main). Registered import './audiomuse' in core/index.ts. (3) preload/
audiomuse.ts bridge (fingerprint/alchemy → ipcRenderer.invoke) added to preload/index.ts api object →
window.api.audioMuse (auto-typed via PreloadApi). NO renderer consumer/UI yet (infra only). CAVEAT:
fingerprint needs Navidrome creds — Feishin may only store the salted credential not raw password, so
rely on AudioMuse core having NAVIDROME_USER/PASSWORD configured (document); pass username when available.
TIER 2 INCREMENT 6 = FEISHIN AUTOPLAY-SOURCE DROPDOWN (typecheck web+node PASS) — Feishin Tier-2 feature
parity done. Per user: keep Auto DJ, fold modes into ONE dropdown. (1) settings.store AutoDJSettingsSchema
+autoplaySource z.enum(['autoDj','fingerprint','moodFlow']).default('autoDj') + initialState. (2) NEW
renderer helper features/player/auto-dj/audio-muse-source.ts: audioMuseConfigured() + fetchFingerprintIds
/fetchAlchemyIds calling window.api.audioMuse (isElectron-guarded, fail-soft []). (3) use-auto-dj.ts:
appendAudioMuse(seedId, existingIds) helper inside the effect; BOTH local + remote handlers branch — if
source!='autoDj' → appendAudioMuse (fingerprint or alchemy[current song]) → addToQueueByFetch(SONG,LAST)
→ return, else existing autoDj. Added audioMuse + settings.autoplaySource to deps. (4) right-controls.tsx
AutoDJButton: enabled Switch REPLACED by a Select [Off / Auto DJ / Sonic Fingerprint / Mood Flow] (last
two only when audioMuseConfigured); Off→{enabled:false}, else→{enabled:true,autoplaySource}; mode+strategy
controls wrapped in {showAutoDjControls && <>...</>}; itemCount/timing always; added AudioMuse URL+token
TextInputs (isElectron only) → setSettings({audioMuse:{...}}) deep-merges. Removed now-unused Switch+Paper
imports. V1 LIMITS: Feishin Mood Flow is CURRENT-SONG-SEEDED (no skip/play-through signal capture yet —
needs web-player progress plumbing; Navic has full signals); Tier-2 DESKTOP-ONLY (main process for CORS,
web falls back to Auto DJ); fingerprint passes username only (relies on AudioMuse core NAVIDROME_PASSWORD).
TIER 2 INCREMENT 7 = NAVIC MOOD-REACTIVE PALETTE + HAZE TOGGLE (NOT compiled). (A) PALETTE (dep-free):
AlchemyResponse +@SerialName("centroid_2d") centroid2d:List<Float>?; AudioMuseManager exposes
lastMoodCentroid StateFlow (set from alchemy resp). AdaptiveMoodBackground reads it → baseHue =
atan2(c[1],c[0])/PI*180+360 %360 (scale-independent; default 300f when null) → animateFloatAsState(tween
4000) morph → Color.hsv palette (base + hue/hue+45/hue+315 blobs). Motion still fixed (energy-reactive
motion deferred). (B) HAZE as APPEARANCE TOGGLE: PreferenceManager.expressiveBlur (default false);
AppearanceScreen SettingSwitchRow "Expressive blur". Added Haze dep — libs.versions.toml haze="2.0.0-alpha03"
(⚠️ GUESSED VERSION — 2.0.x is the CMP-1.11 line; bump to latest 2.0.x on Maven Central if Gradle sync
fails) + haze + haze-blur libraries; build.gradle commonMain implementation(libs.haze)+(libs.haze.blur).
Wiring (Haze 2.0 API from repo README — hazeSource/hazeEffect{blurEffect{blurRadius}}, blurEffect from
dev.chrisbanes.haze.blur): NowPlayingScreen makes hazeState=remember{HazeState()}, moodHaze=(expressiveBlur
&& Adaptive)?hazeState:null; AdaptiveMoodBackground gets .hazeSource(hazeState) when expressiveBlur;
moodHaze threaded NowPlayingScreen→NowPlayingControlsRow(hazeState param)→NowPlayingAutoplaySelector
(hazeState param). Chip button: when hazeState!=null → Modifier.hazeEffect{blurEffect{blurRadius=20.dp}}
+ background(frost@0.5 tint for contrast), else background(frost). Popup left on translucent frost (Popup
is a separate window, Haze can't cross it). ⚠️ RISKS to verify at build: Haze alpha VERSION; HazeState()
ctor (2.0 may want rememberHazeState()); blurEffect DSL/import path. Mood-palette part is dep-free + should
compile. NEXT TIER-2: (A) Feishin Mood Flow skip/play-through signal capture (parity); (B) chat/mood search;
(C) mood-palette + Haze parity in Feishin; (D) energy-reactive motion.

AUTOPLAY FIELD-TEST 1 (user confirmed autoqueue works): two fixes (NOT compiled). (1) QueueScreen.kt
no longer closes on song tap — removed backStack.remove(Screen.Queue) in the row onClick (+ pruned
unused backStack val / LocalNavStack + Screen imports) so newly auto-queued songs stay visible; the
list follows currentIndex via the existing LaunchedEffect. (2) COIL CRASH-ON-UPDATE FIXED PROPERLY
(supersedes the old imageLoaderFactoryInstalled flag, which was insufficient): real exception was
"IllegalStateException: The singleton image loader has already been created" at AppKt.App — on an
in-place update the restarted foreground playback service (DownloadManager.kt:297 SingletonImageLoader.get
for art) created Coil's default singleton BEFORE the Activity's App() ran setSingletonImageLoaderFactory.
Fix: androidApp Application now implements coil3.SingletonImageLoader.Factory (newImageLoader =
initializeSingletonImageLoader) so Coil builds OUR loader lazily on the first .get() from anyone (race
gone). App.kt's composable setSingletonImageLoaderFactory call is GATED by a new `internal expect val
installComposeSingletonImageLoader` (actual=false androidMain ImageLoaderInstall.android.kt — Application
supplies the factory; actual=true iosMain ImageLoaderInstall.ios.kt — no Application factory). NOTE: a
first attempt wrapped that call in try/catch and FAILED to compile ("Try catch is not supported around
composable function invocations" — setSingletonImageLoaderFactory is @Composable); expect/actual gate
replaced it. androidApp already has libs.bundles.coil + projects.composeApp so the imports resolve.

Related: the user's existing Telegram bot is [[listenbrainz-bot]] (could become an
optional extra controller later).
