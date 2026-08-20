package paige.navic.ui.screens.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.background
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.LocalMinimumInteractiveComponentSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_allow_mp3_retry
import navic.composeapp.generated.resources.action_cancel_all
import navic.composeapp.generated.resources.action_cancel_download
import navic.composeapp.generated.resources.action_clear_failed
import navic.composeapp.generated.resources.action_delete_download
import navic.composeapp.generated.resources.action_dismiss
import navic.composeapp.generated.resources.action_repair_downloads
import navic.composeapp.generated.resources.action_retry
import navic.composeapp.generated.resources.action_retry_all
import navic.composeapp.generated.resources.lbbot_fill_cancelled
import navic.composeapp.generated.resources.lbbot_fill_downloading
import navic.composeapp.generated.resources.lbbot_fill_failed
import navic.composeapp.generated.resources.lbbot_fill_gave_up
import navic.composeapp.generated.resources.lbbot_fill_needs_match
import navic.composeapp.generated.resources.lbbot_fill_needs_pick
import navic.composeapp.generated.resources.lbbot_fill_placed
import navic.composeapp.generated.resources.lbbot_fill_placing
import navic.composeapp.generated.resources.lbbot_fill_queued
import navic.composeapp.generated.resources.lbbot_fill_searching
import navic.composeapp.generated.resources.lbbot_fill_verified
import navic.composeapp.generated.resources.section_lbbot_fills
import navic.composeapp.generated.resources.banner_downloads_waiting
import navic.composeapp.generated.resources.section_download_settings
import navic.composeapp.generated.resources.setting_download_charging_only
import navic.composeapp.generated.resources.setting_download_charging_only_desc
import navic.composeapp.generated.resources.setting_download_concurrency
import navic.composeapp.generated.resources.setting_download_concurrency_desc
import navic.composeapp.generated.resources.setting_download_wifi_only
import navic.composeapp.generated.resources.setting_download_wifi_only_desc
import navic.composeapp.generated.resources.download_source_album
import navic.composeapp.generated.resources.download_source_library
import navic.composeapp.generated.resources.download_source_manual
import navic.composeapp.generated.resources.download_source_playlist
import navic.composeapp.generated.resources.download_source_queue
import navic.composeapp.generated.resources.info_download_attempts
import navic.composeapp.generated.resources.info_no_downloads
import navic.composeapp.generated.resources.section_downloads_active
import navic.composeapp.generated.resources.section_downloads_completed
import navic.composeapp.generated.resources.section_downloads_failed
import navic.composeapp.generated.resources.section_downloads_queued
import navic.composeapp.generated.resources.title_download_center
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.viewmodel.koinViewModel
import androidx.compose.ui.graphics.vector.ImageVector
import paige.navic.data.database.entities.DownloadSource
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.LbFillEntry
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Delete
import paige.navic.icons.outlined.Queue
import paige.navic.ui.components.common.ContentUnavailable
import paige.navic.ui.components.common.Form
import paige.navic.ui.components.common.FormRow
import paige.navic.ui.components.common.FormTitle
import paige.navic.ui.components.common.RemoteCoverArt
import paige.navic.ui.components.layouts.NestedTopBar
import paige.navic.ui.screens.settings.viewmodels.DownloadCenterItem
import paige.navic.ui.screens.settings.viewmodels.DownloadCenterViewModel

/**
 * Everything the app is holding offline, split by what it's actually DOING: transferring now,
 * accepted but waiting on a permit, failed (with the reason, and a retry that reuses the original
 * quality/format), and completed.
 */
@Composable
fun DownloadCenterScreen() {
	val viewModel = koinViewModel<DownloadCenterViewModel>()
	val state by viewModel.state.collectAsStateWithLifecycle()
	val settings by viewModel.settings.collectAsStateWithLifecycle()
	val constrained by viewModel.constrained.collectAsStateWithLifecycle()
	val fills by viewModel.fills.collectAsStateWithLifecycle()

	Scaffold(
		topBar = {
			NestedTopBar(title = { Text(stringResource(Res.string.title_download_center)) })
		},
		contentWindowInsets = WindowInsets.statusBars
	) { innerPadding ->
		CompositionLocalProvider(LocalMinimumInteractiveComponentSize provides 0.dp) {
			Column(
				Modifier
					.padding(innerPadding)
					.fillMaxSize()
					.verticalScroll(rememberScrollState())
					.padding(top = 16.dp, end = 16.dp, start = 16.dp, bottom = 32.dp)
			) {
				// Download constraints. Kept at the top so the "why is my queue stuck?" answer is
				// right next to the (possibly stalled) sections below.
				FormTitle(stringResource(Res.string.section_download_settings))
				Form {
					ToggleRow(
						title = stringResource(Res.string.setting_download_wifi_only),
						subtitle = stringResource(Res.string.setting_download_wifi_only_desc),
						checked = settings.wifiOnly,
						onCheckedChange = { viewModel.setWifiOnly(it) }
					)
					ToggleRow(
						title = stringResource(Res.string.setting_download_charging_only),
						subtitle = stringResource(Res.string.setting_download_charging_only_desc),
						checked = settings.chargingOnly,
						onCheckedChange = { viewModel.setChargingOnly(it) }
					)
					ConcurrencyRow(
						value = settings.maxConcurrency,
						onChange = { viewModel.setMaxConcurrency(it) }
					)
				}

				if (constrained) {
					ConstrainedBanner()
				}

				// Above the offline sections, and separate from them: these are albums
				// lb-bot is acquiring from Soulseek, not files being cached for offline
				// playback. Empty whenever lb-bot is unconfigured, since nothing can then
				// ever have been started — which is also how the section stays hidden.
				if (fills.isNotEmpty()) {
					SectionHeader(
						title = stringResource(Res.string.section_lbbot_fills),
						count = fills.size
					)
					Form {
						fills.forEach { fill ->
							FillRow(
								fill = fill,
								onRetry = { viewModel.retryFill(fill.key) },
								onAllowMp3 = { viewModel.allowMp3AndRetry(fill) },
								onDismiss = { viewModel.dismissFill(fill.key) }
							)
						}
					}
				}

				val isEmpty = state.active.isEmpty() && state.queued.isEmpty() &&
					state.failed.isEmpty() && state.completed.isEmpty() && fills.isEmpty()

				if (isEmpty) {
					ContentUnavailable(
						// NOT the default fillMaxSize(): this sits inside a verticalScroll, where
						// the height constraint is infinite and filling it would blow up.
						modifier = Modifier
							.fillMaxWidth()
							.padding(vertical = 64.dp),
						icon = Icons.Outlined.Queue,
						label = stringResource(Res.string.info_no_downloads)
					)
				}

				if (state.active.isNotEmpty()) {
					SectionHeader(
						title = stringResource(Res.string.section_downloads_active),
						count = state.active.size,
						actionLabel = stringResource(Res.string.action_cancel_all),
						onAction = { viewModel.cancelAll() }
					)
					Form {
						state.active.forEach { item ->
							DownloadRow(
								item = item,
								// Progress is only meaningful for something actually transferring.
								progress = item.download.progress,
								trailingIcon = Icons.Outlined.Delete,
								trailingDescription = stringResource(Res.string.action_cancel_download),
								onTrailing = { viewModel.cancel(item) }
							)
						}
					}
				}

				if (state.queued.isNotEmpty()) {
					SectionHeader(
						title = stringResource(Res.string.section_downloads_queued),
						count = state.queued.size
					)
					Form {
						state.queued.forEach { item ->
							DownloadRow(
								item = item,
								trailingIcon = Icons.Outlined.Delete,
								trailingDescription = stringResource(Res.string.action_cancel_download),
								onTrailing = { viewModel.cancel(item) }
							)
						}
					}
				}

				if (state.failed.isNotEmpty()) {
					SectionHeader(
						title = stringResource(Res.string.section_downloads_failed),
						count = state.failed.size,
						actionLabel = stringResource(Res.string.action_retry_all),
						onAction = { viewModel.retryAllFailed() },
						secondaryActionLabel = stringResource(Res.string.action_clear_failed),
						onSecondaryAction = { viewModel.clearFailed() }
					)
					Form {
						state.failed.forEach { item ->
							DownloadRow(
								item = item,
								// The failure reason, not just a red icon — "Retry" is a coin flip
								// otherwise.
								supporting = item.download.error,
								supportingIsError = true,
								trailing = {
									TextButton(onClick = { viewModel.retry(item) }) {
										Text(stringResource(Res.string.action_retry))
									}
								}
							)
						}
					}
				}

				if (state.completed.isNotEmpty()) {
					SectionHeader(
						title = stringResource(Res.string.section_downloads_completed),
						count = state.completed.size,
						// Re-verifies files still exist and re-fetches any that vanished.
						actionLabel = stringResource(Res.string.action_repair_downloads),
						onAction = { viewModel.repairMissing() },
						trailingText = formatSize(state.totalSize)
					)
					Form {
						state.completed.forEach { item ->
							DownloadRow(
								item = item,
								// Size plus WHO asked for it, so a playlist-managed or whole-library
								// file is distinguishable from one the user downloaded by hand.
								supporting = "${formatSize(item.download.fileSize)} · " +
									sourceLabel(item.download.sourcePolicy),
								trailingIcon = Icons.Outlined.Delete,
								trailingDescription = stringResource(Res.string.action_delete_download),
								onTrailing = { viewModel.delete(item) }
							)
						}
					}
				}
			}
		}
	}
}

@Composable
private fun ToggleRow(
	title: String,
	subtitle: String,
	checked: Boolean,
	onCheckedChange: (Boolean) -> Unit
) {
	FormRow(onClick = { onCheckedChange(!checked) }) {
		Column(Modifier.weight(1f)) {
			Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
			Text(
				subtitle,
				style = MaterialTheme.typography.bodyMedium,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
		}
		Switch(checked = checked, onCheckedChange = onCheckedChange)
	}
}

/** A simple −/N/+ stepper for the concurrency setting, clamped to 1..10. */
@Composable
private fun ConcurrencyRow(value: Int, onChange: (Int) -> Unit) {
	FormRow {
		Column(Modifier.weight(1f)) {
			Text(stringResource(Res.string.setting_download_concurrency))
			Text(
				stringResource(Res.string.setting_download_concurrency_desc),
				style = MaterialTheme.typography.bodyMedium,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
		}
		Row(verticalAlignment = Alignment.CenterVertically) {
			TextButton(enabled = value > 1, onClick = { onChange(value - 1) }) { Text("−") }
			Text(
				value.toString(),
				style = MaterialTheme.typography.titleMedium,
				modifier = Modifier.padding(horizontal = 4.dp)
			)
			TextButton(enabled = value < 10, onClick = { onChange(value + 1) }) { Text("+") }
		}
	}
}

@Composable
private fun ConstrainedBanner() {
	Row(
		modifier = Modifier
			.fillMaxWidth()
			.padding(vertical = 8.dp)
			.background(
				MaterialTheme.colorScheme.secondaryContainer,
				RoundedCornerShape(12.dp)
			)
			.padding(horizontal = 16.dp, vertical = 12.dp),
		verticalAlignment = Alignment.CenterVertically
	) {
		Text(
			stringResource(Res.string.banner_downloads_waiting),
			style = MaterialTheme.typography.bodyMedium,
			color = MaterialTheme.colorScheme.onSecondaryContainer
		)
	}
}

@Composable
private fun SectionHeader(
	title: String,
	count: Int,
	actionLabel: String? = null,
	onAction: (() -> Unit)? = null,
	secondaryActionLabel: String? = null,
	onSecondaryAction: (() -> Unit)? = null,
	trailingText: String? = null
) {
	Row(
		modifier = Modifier.fillMaxWidth(),
		horizontalArrangement = Arrangement.SpaceBetween,
		verticalAlignment = Alignment.CenterVertically
	) {
		FormTitle("$title ($count)")
		Row(verticalAlignment = Alignment.CenterVertically) {
			if (trailingText != null) {
				Text(
					trailingText,
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			}
			if (secondaryActionLabel != null && onSecondaryAction != null) {
				TextButton(onClick = onSecondaryAction) { Text(secondaryActionLabel) }
			}
			if (actionLabel != null && onAction != null) {
				TextButton(onClick = onAction) { Text(actionLabel) }
			}
		}
	}
}

@Composable
private fun DownloadRow(
	item: DownloadCenterItem,
	progress: Float? = null,
	supporting: String? = null,
	supportingIsError: Boolean = false,
	trailingIcon: ImageVector? = null,
	trailingDescription: String? = null,
	onTrailing: (() -> Unit)? = null,
	trailing: (@Composable () -> Unit)? = null
) {
	FormRow {
		Column(Modifier.weight(1f)) {
			Text(
				// A row whose song isn't in the local library yet still has to say SOMETHING —
				// falling back to the id beats rendering a blank line.
				text = item.song?.title ?: item.download.songId,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
			val subtitle = supporting ?: item.song?.artistName
			if (subtitle != null) {
				Text(
					text = subtitle,
					style = MaterialTheme.typography.bodyMedium,
					color = if (supportingIsError) MaterialTheme.colorScheme.error
					else MaterialTheme.colorScheme.onSurfaceVariant,
					maxLines = 2,
					overflow = TextOverflow.Ellipsis
				)
			}
			if (item.download.retryCount > 0) {
				Text(
					text = stringResource(
						Res.string.info_download_attempts,
						item.download.retryCount + 1
					),
					style = MaterialTheme.typography.bodySmall,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			}
			if (progress != null) {
				LinearProgressIndicator(
					progress = { progress },
					modifier = Modifier
						.fillMaxWidth()
						.padding(top = 8.dp)
				)
			}
		}

		if (trailing != null) {
			trailing()
		} else if (trailingIcon != null && onTrailing != null) {
			IconButton(onClick = onTrailing) {
				Icon(
					trailingIcon,
					contentDescription = trailingDescription,
					modifier = Modifier.size(20.dp)
				)
			}
		}
	}
}

/**
 * One album lb-bot is fetching, or has finished trying to fetch.
 *
 * Not [DownloadRow]: that one is built around a `DownloadCenterItem` — a song row with a
 * cached file behind it — and these have no song, no file and no local id. What they have
 * instead is a release-group, cover art from the Cover Art Archive (lb-bot's own cover
 * route serves Navidrome art keyed by a Navidrome album id, which a release the library
 * lacks does not have), and lb-bot's own sentence for why it failed.
 */
@Composable
private fun FillRow(
	fill: LbFillEntry,
	onRetry: () -> Unit,
	onAllowMp3: () -> Unit,
	onDismiss: () -> Unit
) {
	FormRow {
		RemoteCoverArt(
			url = fill.coverUrl,
			modifier = Modifier.size(44.dp).padding(end = 12.dp),
			contentDescription = null
		)
		Column(Modifier.weight(1f)) {
			Text(
				text = fill.album.ifBlank { fill.artist }.ifBlank { fill.key },
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
			val failed = fill.settled && !fill.succeeded
			Text(
				text = fillStateLabel(fill),
				style = MaterialTheme.typography.bodyMedium,
				color = if (failed) MaterialTheme.colorScheme.error
				else MaterialTheme.colorScheme.onSurfaceVariant,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
			// lb-bot's reason, verbatim. It is the only thing that distinguishes "no peer
			// had it" from "every source was rejected for format" — and the second of
			// those is what the Allow MP3 button below is for.
			if (failed && fill.reason.isNotBlank()) {
				Text(
					text = fill.reason,
					style = MaterialTheme.typography.bodySmall,
					color = MaterialTheme.colorScheme.onSurfaceVariant,
					maxLines = 3,
					overflow = TextOverflow.Ellipsis
				)
			}
			if (fill.isRunning) {
				// Determinate only once lb-bot is counting transfers; before that it is
				// searching, and a bar sitting at zero reads as a stall.
				if (fill.total > 0) {
					LinearProgressIndicator(
						progress = { fill.percent / 100f },
						modifier = Modifier.fillMaxWidth().padding(top = 8.dp)
					)
				} else {
					LinearProgressIndicator(
						modifier = Modifier.fillMaxWidth().padding(top = 8.dp)
					)
				}
			}
		}

		if (fill.settled) {
			Row(verticalAlignment = Alignment.CenterVertically) {
				if (!fill.succeeded) {
					if (fill.mp3WouldHelp && fill.groupId.isNotBlank()) {
						TextButton(onClick = onAllowMp3) {
							Text(stringResource(Res.string.action_allow_mp3_retry))
						}
					}
					TextButton(onClick = onRetry) {
						Text(stringResource(Res.string.action_retry))
					}
				}
				IconButton(onClick = onDismiss) {
					Icon(
						Icons.Outlined.Delete,
						contentDescription = stringResource(Res.string.action_dismiss),
						modifier = Modifier.size(20.dp)
					)
				}
			}
		}
	}
}

/**
 * What a fill is doing, in the user's terms rather than lb-bot's.
 *
 * The outcome is checked before the state because a settled row's last state is not the
 * whole story: a fill given up on still reads `unknown`, and a cancelled one keeps
 * whatever it was doing when it was cancelled.
 */
@Composable
private fun fillStateLabel(fill: LbFillEntry): String = when {
	fill.settled -> when (fill.outcome) {
		LbBotManager.OUTCOME_DONE -> stringResource(Res.string.lbbot_fill_verified)
		LbBotManager.OUTCOME_CANCELLED -> stringResource(Res.string.lbbot_fill_cancelled)
		LbBotManager.OUTCOME_NEEDS_PICK -> stringResource(Res.string.lbbot_fill_needs_pick)
		LbBotManager.OUTCOME_GAVE_UP -> stringResource(Res.string.lbbot_fill_gave_up)
		else -> if (fill.state == "needs_match")
			stringResource(Res.string.lbbot_fill_needs_match)
		else stringResource(Res.string.lbbot_fill_failed)
	}
	fill.state == "downloading" && fill.total > 0 ->
		stringResource(Res.string.lbbot_fill_downloading, fill.done, fill.total)
	fill.state == "queued" -> stringResource(Res.string.lbbot_fill_queued)
	fill.state == "placing" -> stringResource(Res.string.lbbot_fill_placing)
	fill.state == "placed" -> stringResource(Res.string.lbbot_fill_placed)
	// `searching` and the ambiguous `unknown` — which for a live row means lb-bot's
	// worker has not written its first ledger row yet — read the same to the user.
	else -> stringResource(Res.string.lbbot_fill_searching)
}

/** Human label for a download's [DownloadSource]; unknown values fall back to the raw string. */
@Composable
private fun sourceLabel(source: String): String = when (source) {
	DownloadSource.MANUAL -> stringResource(Res.string.download_source_manual)
	DownloadSource.ALBUM -> stringResource(Res.string.download_source_album)
	DownloadSource.PLAYLIST -> stringResource(Res.string.download_source_playlist)
	DownloadSource.LIBRARY -> stringResource(Res.string.download_source_library)
	DownloadSource.QUEUE -> stringResource(Res.string.download_source_queue)
	else -> source.replaceFirstChar { it.uppercase() }
}

/** Bytes as MB/GB — the same rounding the storage settings row uses. */
private fun formatSize(bytes: Long): String {
	val mb = bytes.toDouble() / (1024 * 1024)
	return if (mb > 1024) {
		"${((mb / 1024) * 100).toInt() / 100.0} GB"
	} else {
		"${mb.toInt()} MB"
	}
}
