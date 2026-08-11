package paige.navic.ui.components.sheets

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_allow_mp3
import navic.composeapp.generated.resources.action_auto_pick
import navic.composeapp.generated.resources.action_cancel
import navic.composeapp.generated.resources.action_download_from_source
import navic.composeapp.generated.resources.action_find_sources
import navic.composeapp.generated.resources.action_recheck_album
import navic.composeapp.generated.resources.action_show_files
import navic.composeapp.generated.resources.info_gap_edition
import navic.composeapp.generated.resources.info_gap_failed
import navic.composeapp.generated.resources.info_gap_progress
import navic.composeapp.generated.resources.info_source_failover
import navic.composeapp.generated.resources.info_source_files_unexpanded
import navic.composeapp.generated.resources.info_fill_searching
import navic.composeapp.generated.resources.info_fill_verified
import navic.composeapp.generated.resources.info_gap_needs_lbbot
import navic.composeapp.generated.resources.info_gap_pick_source
import navic.composeapp.generated.resources.info_mp3_would_help
import navic.composeapp.generated.resources.info_no_sources
import navic.composeapp.generated.resources.info_source_coverage
import navic.composeapp.generated.resources.info_source_matches_album
import navic.composeapp.generated.resources.info_source_recommended
import navic.composeapp.generated.resources.info_source_wrong_album
import navic.composeapp.generated.resources.info_tracks_present
import navic.composeapp.generated.resources.title_fill_gaps
import navic.composeapp.generated.resources.title_sources
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.LbGap
import paige.navic.domain.manager.LbGapSource
import paige.navic.domain.manager.LbSourceFiles
import paige.navic.ui.components.common.CoverArt

/**
 * Consecutive failures a *polled* read may suffer before the sheet says anything.
 *
 * Only ever applied to reads nobody asked for. A user-pressed action reports its first failure
 * immediately — the point of the tolerance is to stop lb-bot's one-request-at-a-time lock painting
 * errors under a sheet that is working, not to hide real refusals.
 */
private const val TRANSIENT_FAILURE_LIMIT = 2

private const val TRANSIENT_RETRY_DELAY_MS = 1_500L

/**
 * How long a fetch holds the button down before giving it back on its own.
 *
 * A timer, not just evidence: lb-bot flipping to `downloading` is the expected release, but a
 * fetch that quietly didn't take would otherwise leave the sheet permanently disabled.
 */
private const val COMMIT_RELEASE_MS = 30_000L

/**
 * The gaps in an album the library already has: which tracks are absent, and what
 * to do about them.
 *
 * Distinct from [MissingAlbumSheet], which acquires a whole release. This drives
 * lb-bot's Fill-gaps pipeline against one review group — the `group_id` that came
 * on the `incomplete` discography row, which is live because lb-bot's discography
 * scan builds the review group as it goes.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GapFillSheet(
	groupId: String,
	albumTitle: String,
	coverArtId: String?,
	onDismissRequest: () -> Unit
) {
	val lbBot = koinInject<LbBotManager>()
	val scope = rememberCoroutineScope()

	val gaps by lbBot.gaps.collectAsState()
	var loading by remember(groupId) { mutableStateOf(true) }
	var working by remember(groupId) { mutableStateOf(false) }
	var error by remember(groupId) { mutableStateOf<LbBotManager.LbError?>(null) }
	val gap = gaps[groupId]

	// Held from a fetch POST returning until lb-bot's own status catches up. See [committed].
	var committed by remember(groupId) { mutableStateOf(false) }

	LaunchedEffect(groupId) {
		loading = true
		// A failure here is the one that used to be invisible: an older hub proxies
		// no gap routes, answers 404, and every button silently did nothing.
		//
		// "Busy" is the exception, and it is absorbed rather than shown: lb-bot serialises every
		// route behind one lock, so opening this sheet while a search runs times out at the hub
		// and used to paint an error line under a sheet that was working perfectly. Nothing was
		// pressed, so nothing is owed an answer yet — retry quietly, and only speak up if it keeps
		// failing.
		var attempt = 0
		while (true) {
			val result = lbBot.refreshGap(groupId)
			if (result !is LbBotManager.LbResult.Failed) break
			if (result.error !is LbBotManager.LbError.Busy || attempt >= TRANSIENT_FAILURE_LIMIT) {
				error = result.error
				break
			}
			attempt++
			delay(TRANSIENT_RETRY_DELAY_MS)
		}
		loading = false
		// Already in flight — started on this device before a restart, or on another
		// client entirely. Either way this device knows nothing about it until it
		// reads lb-bot, so pick the watch up rather than showing a frozen snapshot.
		if (lbBot.gaps.value[groupId]?.status == "downloading") lbBot.startGapWatch(groupId)
	}

	// Run one lb-bot write, surfacing whatever comes back.
	//
	// No tolerance here, deliberately: this only ever runs because the user pressed something, and
	// a button that silently does nothing is the exact failure the whole error model exists to
	// prevent. Absorbing failures is for polls nobody asked for.
	fun act(block: suspend () -> LbBotManager.LbResult<*>) {
		scope.launch {
			working = true
			error = null
			(block() as? LbBotManager.LbResult.Failed)?.let { error = it.error }
			working = false
		}
	}

	/** [act], plus hold the fetch button down until lb-bot admits it is transferring. */
	fun commit(block: suspend () -> LbBotManager.LbResult<*>) {
		scope.launch {
			working = true
			error = null
			val result = block()
			if (result is LbBotManager.LbResult.Failed) error = result.error else committed = true
			working = false
		}
	}

	ModalBottomSheet(onDismissRequest = onDismissRequest) {
		Column(Modifier.padding(horizontal = 20.dp).padding(bottom = 24.dp)) {
			Row(verticalAlignment = Alignment.CenterVertically) {
				CoverArt(coverArtId = coverArtId, modifier = Modifier.size(64.dp))
				Column(Modifier.padding(start = 12.dp)) {
					Text(
						albumTitle,
						style = MaterialTheme.typography.titleMedium,
						maxLines = 2,
						overflow = TextOverflow.Ellipsis
					)
					Text(
						if (gap != null && gap.total > 0)
							stringResource(Res.string.info_tracks_present, gap.present, gap.total)
						else stringResource(Res.string.title_fill_gaps),
						style = MaterialTheme.typography.bodySmall,
						color = MaterialTheme.colorScheme.onSurfaceVariant
					)
				}
			}

			Spacer(Modifier.height(16.dp))

			LbErrorLine(error)

			if (loading && gap == null) {
				Box(Modifier.fillMaxWidth().height(72.dp), Alignment.Center) {
					CircularProgressIndicator(Modifier.size(24.dp))
				}
			} else if (gap != null) {
				// slskd takes 30-90s to answer, and pressing again during that window
				// is not harmless: the second request blocks on lb-bot's review lock
				// long enough to time out at the hub, which arrives as a bare 502 for
				// what is really "busy". So the button says what it is doing instead.
				val searching =
					gap.sourceTask?.status.orEmpty() in LbBotManager.SEARCH_IN_FLIGHT

				// "Is a transfer actually happening", which `status == "downloading"` is a late
				// and incomplete proxy for. lb-bot enqueues on its own thread and flips that
				// status well after the first track is queued, so a fill that was genuinely
				// running showed nothing for its opening minutes — which reads as "my tap did
				// nothing" and invites a second fetch into the same album. `committed` closes the
				// remaining window between the POST returning and the first poll that reflects it.
				//
				// NOT `picked`: lb-bot's search sets that on every missing track before it starts
				// looking, so treating it as busy disables the picker in precisely the state where
				// the user is meant to choose a source. Only `queued` and `downloading` are real.
				val transferInFlight = committed ||
					gap.status == "downloading" ||
					gap.tracks.any { it.state == "queued" || it.state == "downloading" }

				// Released on a timer as well as on evidence, deliberately: a fetch that quietly
				// didn't take must give the button back rather than dying disabled.
				LaunchedEffect(committed, gap.status, transferInFlight) {
					if (!committed) return@LaunchedEffect
					if (gap.status == "downloading") {
						committed = false
						return@LaunchedEffect
					}
					delay(COMMIT_RELEASE_MS)
					committed = false
				}

				// Which release the slot count comes from. lb-bot measures the gap
				// against the canonical album's own MusicBrainz tag, so a library
				// tagged as a deluxe edition legitimately reports seventeen slots
				// while every pressing on offer has twelve — without saying so, the
				// count reads as a miscount and the short download reads as a bug.
				if (gap.canonicalMbid.isNotBlank() && gap.total > 0) {
					SheetCaption(stringResource(Res.string.info_gap_edition))
				}
				GapTracks(gap)

				Spacer(Modifier.height(12.dp))
				HorizontalDivider()
				Spacer(Modifier.height(8.dp))

					GapStatusLine(gap, transferInFlight)

				if (gap.mp3WouldHelp) {
					Text(
						stringResource(Res.string.info_mp3_would_help),
						style = MaterialTheme.typography.bodySmall,
						color = MaterialTheme.colorScheme.onSurfaceVariant
					)
					TextButton(onClick = { act { lbBot.allowMp3(groupId) } }) {
						Text(stringResource(Res.string.action_allow_mp3))
					}
				}

				Spacer(Modifier.height(8.dp))
				Row(
					Modifier.fillMaxWidth(),
					horizontalArrangement = Arrangement.spacedBy(8.dp)
				) {
					// "Find sources", not "download": a gap fill has the same
					// wrong-pressing hazard as a whole-album download and rather
					// less margin — the tracks land inside an album you already
					// have. Auto is still here, one row down, for when the ranking
					// is good enough; it just isn't the default any more.
					Button(
						onClick = { act { lbBot.gapSearch(groupId) } },
						enabled = !working && !searching && !transferInFlight,
						modifier = Modifier.weight(1f)
					) {
						if (working || searching) CircularProgressIndicator(Modifier.size(18.dp))
						else Text(stringResource(Res.string.action_find_sources))
					}
					if (transferInFlight) {
						OutlinedButton(onClick = { act { lbBot.gapCancel(groupId) } }) {
							Text(stringResource(Res.string.action_cancel))
						}
					} else {
						OutlinedButton(onClick = { act { lbBot.gapRescan(groupId) } }) {
							Text(stringResource(Res.string.action_recheck_album))
						}
					}
				}

				if (gap.sources.isNotEmpty()) {
					Spacer(Modifier.height(12.dp))
					SheetLabel(stringResource(Res.string.title_sources))
					SheetCaption(stringResource(Res.string.info_source_failover))
					GapSources(
						sources = gap.sources,
						enabled = !working && !transferInFlight,
						loadFiles = { index -> lbBot.gapSourceFiles(groupId, index) }
					) { source ->
						commit { lbBot.gapFetch(groupId, source.id) }
					}
					// A search that has ended and left nothing. Keyed off the task being
					// over rather than a literal "finished" — lb-bot ends tasks as
					// `complete` or `error`, so the old check never once matched.
				} else if ((gap.sourceTask != null && !searching) || gap.failReason.isNotBlank()) {
					Text(
						stringResource(Res.string.info_no_sources),
						style = MaterialTheme.typography.bodySmall,
						color = MaterialTheme.colorScheme.onSurfaceVariant
					)
				}

				// Auto, demoted: it walks the same ranked list this picker shows,
				// so it stays available for a user who trusts it, without being
				// the path of least resistance.
				if (!transferInFlight) {
					TextButton(
						onClick = { commit { lbBot.gapAuto(groupId) } },
						enabled = !working && !searching
					) {
						Text(stringResource(Res.string.action_auto_pick))
					}
				}
			}
		}
	}
}

@Composable
private fun GapTracks(gap: LbGap) {
	LazyColumn(Modifier.heightIn(max = 240.dp)) {
		items(gap.tracks) { track ->
			Row(
				Modifier.fillMaxWidth().padding(vertical = 3.dp),
				verticalAlignment = Alignment.CenterVertically
			) {
				Text(
					"${track.position}",
					style = MaterialTheme.typography.bodySmall,
					color = MaterialTheme.colorScheme.onSurfaceVariant,
					modifier = Modifier.size(width = 28.dp, height = 18.dp)
				)
				Text(
					track.title,
					style = MaterialTheme.typography.bodyMedium,
					maxLines = 1,
					overflow = TextOverflow.Ellipsis,
					color = if (track.state == "present" || track.state == "done")
						MaterialTheme.colorScheme.onSurface
					else MaterialTheme.colorScheme.onSurfaceVariant,
					modifier = Modifier.weight(1f)
				)
				// Only annotate a track that is doing something. "present" is the
				// majority of the list and needs no word next to it.
				if (track.state !in setOf("present", "missing")) {
					Text(
						track.state.replace('_', ' '),
						style = MaterialTheme.typography.labelSmall,
						color = MaterialTheme.colorScheme.primary
					)
				}
			}
		}
	}
}

@Composable
private fun GapStatusLine(gap: LbGap, transferInFlight: Boolean) {
	// A running source search is the one thing that takes tens of seconds with
	// nothing else to show, so read the background task rather than looking inert.
	// That field is also why no client ever needs lb-bot's task API.
	val task = gap.sourceTask
	val text = when {
		task?.status == "running" ->
			task.current.ifBlank { stringResource(Res.string.info_fill_searching) }
		gap.status == "complete" -> stringResource(Res.string.info_fill_verified)
		// `picking` is what asking for sources sets, before anything has been
		// found — so with candidates on screen it means "your move", not "go and
		// use lb-bot's web UI". Only the sourceless case is a real hand-off:
		// that is lb-bot's match workspace, which Navic does not port.
		gap.status == "picking" -> stringResource(
			if (gap.sources.isNotEmpty()) Res.string.info_gap_pick_source
			else Res.string.info_gap_needs_lbbot
		)
		gap.status == "downloading" -> gap.album.ifBlank { "" }
		gap.failDetail.isNotBlank() -> gap.failDetail
		task?.error?.isNotBlank() == true -> task.error
		gap.noSourceReason.isNotBlank() -> gap.noSourceReason
		else -> ""
	}
	Column(Modifier.fillMaxWidth()) {
		if (text.isNotBlank()) Text(text, style = MaterialTheme.typography.bodyMedium)

		// A gap fill is per-track, so there is no single transfer to report on and
		// the sheet used to sit inert for minutes. The per-track states are already
		// in the poll; count them. `tracksWanted` is the tracks being filled, not
		// the album's length — progress against 17 when only 12 were queued would
		// stall at 12/17 and read as a hang.
		if (transferInFlight && gap.tracksWanted > 0) {
			Text(
				stringResource(
					Res.string.info_gap_progress,
					gap.tracksDone,
					gap.tracksWanted
				),
				style = MaterialTheme.typography.bodySmall,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
			Spacer(Modifier.height(6.dp))
			LinearProgressIndicator(
				progress = { gap.tracksDone.toFloat() / gap.tracksWanted },
				modifier = Modifier.fillMaxWidth()
			)
			if (gap.tracksFailed > 0) {
				Text(
					stringResource(Res.string.info_gap_failed, gap.tracksFailed),
					style = MaterialTheme.typography.labelSmall,
					color = MaterialTheme.colorScheme.error
				)
			}
		}
	}
}

@Composable
private fun GapSources(
	sources: List<LbGapSource>,
	enabled: Boolean,
	loadFiles: suspend (Int) -> LbBotManager.LbResult<LbSourceFiles>,
	onPick: (LbGapSource) -> Unit
) {
	val scope = rememberCoroutineScope()
	// The listings are stripped from the poll (they would be hundreds of KB every
	// five seconds), so each one is fetched the first time its source is opened
	// and kept for the life of the sheet.
	val loaded = remember { mutableStateMapOf<Int, LbSourceFiles>() }
	var expanded by remember { mutableStateOf(-1) }
	var loadingIndex by remember { mutableStateOf(-1) }

	LazyColumn(Modifier.heightIn(max = 300.dp)) {
		items(sources) { source ->
			Column(
				Modifier
					.fillMaxWidth()
					.padding(vertical = 6.dp)
			) {
				SourceHeadline(source.peer) {
					if (source.recommended) {
						Text(
							stringResource(Res.string.info_source_recommended),
							style = MaterialTheme.typography.labelSmall,
							color = MaterialTheme.colorScheme.primary
						)
					}
				}
				// Same wrong-album risk as a whole-album download, so the same
				// verdict: a peer's folder name is the only clue that the tracks
				// about to be dropped into your album came from a different record.
				Text(
					if (source.albumMatchOk) stringResource(Res.string.info_source_matches_album)
					else stringResource(Res.string.info_source_wrong_album),
					style = MaterialTheme.typography.labelMedium,
					fontWeight = FontWeight.Medium,
					color = if (source.albumMatchOk) MaterialTheme.colorScheme.primary
					else MaterialTheme.colorScheme.error
				)
				SourceMetaLine(
					listOf(
						source.format,
						source.bitrate,
						source.size,
						stringResource(
							Res.string.info_source_coverage,
							source.coverageDetail.haveTracks,
							source.coverageDetail.totalTracks
						)
					)
				)
				// Risk flags come straight from lb-bot's own ranking. A source that
				// scores well but is flagged is exactly the case a human should see.
				if (source.flags.isNotEmpty()) {
					Text(
						source.flags.joinToString(" • "),
						style = MaterialTheme.typography.labelSmall,
						color = MaterialTheme.colorScheme.error
					)
				}

				val listing = loaded[source.id]
				when {
					expanded == source.id && listing != null -> {
						if (!listing.expanded) {
							Text(
								stringResource(Res.string.info_source_files_unexpanded),
								style = MaterialTheme.typography.labelSmall,
								color = MaterialTheme.colorScheme.error
							)
						}
						SourceFileList(listing.files, listing.filesTruncated)
					}

					loadingIndex == source.id ->
						CircularProgressIndicator(Modifier.size(16.dp))

					else -> TextButton(onClick = {
						if (listing != null) {
							expanded = source.id
						} else {
							scope.launch {
								loadingIndex = source.id
								(loadFiles(source.id) as? LbBotManager.LbResult.Ok)?.let {
									loaded[source.id] = it.value
									expanded = source.id
								}
								loadingIndex = -1
							}
						}
					}) {
						Text(stringResource(Res.string.action_show_files, source.fileCount))
					}
				}

				TextButton(onClick = { onPick(source) }, enabled = enabled) {
					Text(stringResource(Res.string.action_download_from_source))
				}
			}
			HorizontalDivider()
		}
	}
}
