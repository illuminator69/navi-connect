package paige.navic.ui.screens.savedqueues

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.rememberTextFieldState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.dropUnlessResumed
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_cancel
import navic.composeapp.generated.resources.action_clear_queue_history
import navic.composeapp.generated.resources.action_delete_other_queues
import navic.composeapp.generated.resources.action_delete_queue
import navic.composeapp.generated.resources.action_ok
import navic.composeapp.generated.resources.action_preview_queue
import navic.composeapp.generated.resources.action_rename
import navic.composeapp.generated.resources.action_restore_queue
import navic.composeapp.generated.resources.action_resume_queue
import navic.composeapp.generated.resources.action_save_as_playlist
import navic.composeapp.generated.resources.filter_all
import navic.composeapp.generated.resources.info_no_saved_queues
import navic.composeapp.generated.resources.info_no_saved_queues_hint
import navic.composeapp.generated.resources.message_clear_queue_history
import navic.composeapp.generated.resources.message_delete_other_queues
import navic.composeapp.generated.resources.message_restore_queue_failed
import navic.composeapp.generated.resources.message_save_playlist_failed
import navic.composeapp.generated.resources.message_saved_as_playlist
import navic.composeapp.generated.resources.option_playlist_name
import navic.composeapp.generated.resources.option_queue_name
import navic.composeapp.generated.resources.title_clear_queue_history
import navic.composeapp.generated.resources.title_delete_other_queues
import navic.composeapp.generated.resources.title_rename_queue
import navic.composeapp.generated.resources.title_save_as_playlist
import navic.composeapp.generated.resources.title_saved_queues
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinViewModel
import paige.navic.LocalNavStack
import paige.navic.data.database.entities.SavedQueueEntity
import paige.navic.domain.models.SavedQueueSource
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Badge
import paige.navic.icons.outlined.Delete
import paige.navic.icons.outlined.MoreVert
import paige.navic.icons.outlined.Queue
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.components.common.ContentUnavailable
import paige.navic.ui.components.common.CoverArt
import paige.navic.ui.components.common.Dropdown
import paige.navic.ui.components.common.DropdownItem
import paige.navic.ui.components.common.Form
import paige.navic.ui.components.common.FormButton
import paige.navic.ui.components.common.FormRow
import paige.navic.ui.components.dialogs.FormDialog
import paige.navic.ui.components.layouts.NestedTopBar
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.savedqueues.components.SavedQueuePreviewSheet
import paige.navic.ui.screens.savedqueues.viewmodels.SavedQueueMessage
import paige.navic.ui.screens.savedqueues.viewmodels.SavedQueuesViewModel

/**
 * The Symfonium-style "select media queue" list: every queue Navic has auto-captured (rolling cache
 * of the 20 most recent), most-recent first, with the one playing right now pinned to the top and
 * marked. A chip row filters by how the queue was made (album / playlist / radio / Mood Flow /
 * journey / manual). Tapping a row restores it PAUSED where it left off; a per-row overflow previews
 * its tracks, resumes it playing, saves it as a Navidrome playlist, renames, or deletes.
 *
 * The history itself is shared through the hub, so this screen and Feishin's are two views of one
 * list — which is why the presentation (art, three lines, "Now playing", preview, clear all) is kept
 * deliberately in step with it.
 */
@Composable
fun SavedQueuesScreen() {
	val viewModel = koinViewModel<SavedQueuesViewModel>()
	val mediaPlayer = koinInject<MediaPlayerViewModel>()
	val backStack = LocalNavStack.current

	val queues by viewModel.queues.collectAsStateWithLifecycle()
	val message by viewModel.message.collectAsStateWithLifecycle()
	val restoreFailed by mediaPlayer.restoreFailed.collectAsStateWithLifecycle()
	val actions = rememberSavedQueueActions(viewModel, mediaPlayer)
	val activeId = actions.activeId

	var renameTarget by remember { mutableStateOf<SavedQueueEntity?>(null) }
	var saveTarget by remember { mutableStateOf<SavedQueueEntity?>(null) }
	var previewTarget by remember { mutableStateOf<SavedQueueEntity?>(null) }
	var deleteOthersOpen by remember { mutableStateOf(false) }
	var clearAllOpen by remember { mutableStateOf(false) }
	// null = "All"; otherwise a SavedQueueSource kind.
	var kindFilter by remember { mutableStateOf<String?>(null) }

	val snackbarHostState = remember { SnackbarHostState() }
	val savedMsg = stringResource(Res.string.message_saved_as_playlist)
	val failedMsg = stringResource(Res.string.message_save_playlist_failed)
	val restoreFailedMsg = stringResource(Res.string.message_restore_queue_failed)
	LaunchedEffect(message) {
		when (message) {
			SavedQueueMessage.SavedAsPlaylist -> snackbarHostState.showSnackbar(savedMsg)
			SavedQueueMessage.Error -> snackbarHostState.showSnackbar(failedMsg)
			null -> {}
		}
		if (message != null) viewModel.clearMessage()
	}
	// A restore that fails used to be invisible — the screen closes itself on tap, so the user just
	// saw it go away with nothing playing.
	LaunchedEffect(restoreFailed) {
		if (restoreFailed) {
			snackbarHostState.showSnackbar(restoreFailedMsg)
			mediaPlayer.clearRestoreFailed()
		}
	}

	val rows = queues.orEmpty()
	// Kinds actually present, in a stable canonical order, so the chip row only offers real options.
	val presentKinds = remember(rows) {
		SavedQueueSource.ALL.filter { kind -> rows.any { it.sourceKind == kind } }
	}
	val visibleQueues = remember(rows, kindFilter, activeId) {
		val filtered = kindFilter?.let { k -> rows.filter { it.sourceKind == k } } ?: rows
		// Current queue pinned to the top (stable sort keeps the rest updatedAt-DESC).
		filtered.sortedByDescending { it.id == activeId }
	}

	Scaffold(
		topBar = { NestedTopBar({ Text(stringResource(Res.string.title_saved_queues)) }) },
		snackbarHost = { SnackbarHost(snackbarHostState) },
		contentWindowInsets = WindowInsets.statusBars
	) { innerPadding ->
		when {
			// Room hasn't emitted yet. Without this the empty state flashed on every entry.
			queues == null -> Box(
				Modifier
					.padding(innerPadding)
					.fillMaxSize(),
				contentAlignment = Alignment.Center
			) { CircularProgressIndicator() }

			rows.isEmpty() -> ContentUnavailable(
				modifier = Modifier
					.padding(innerPadding)
					.fillMaxSize(),
				icon = Icons.Outlined.Queue,
				label = stringResource(Res.string.info_no_saved_queues),
				description = stringResource(Res.string.info_no_saved_queues_hint)
			)

			else -> Column(
				Modifier
					.padding(innerPadding)
					.fillMaxSize()
					.verticalScroll(rememberScrollState())
					.padding(top = 8.dp, start = 16.dp, end = 16.dp, bottom = 32.dp)
			) {
				// Only worth a filter row when there's more than one kind to choose between.
				if (presentKinds.size > 1) {
					Row(
						Modifier
							.fillMaxWidth()
							.horizontalScroll(rememberScrollState())
							.padding(bottom = 8.dp),
						horizontalArrangement = Arrangement.spacedBy(8.dp)
					) {
						FilterChip(
							selected = kindFilter == null,
							onClick = { kindFilter = null },
							label = { Text(stringResource(Res.string.filter_all)) }
						)
						presentKinds.forEach { kind ->
							FilterChip(
								selected = kindFilter == kind,
								onClick = { kindFilter = kind },
								label = { Text(queueKindLabel(kind)) }
							)
						}
					}
				}

				Form {
					visibleQueues.forEach { queue ->
						SavedQueueRow(
							queue = queue,
							isActive = queue.id == activeId,
							onClick = dropUnlessResumed {
								actions.restore(queue)
								backStack.remove(Screen.SavedQueues)
							},
							onResume = dropUnlessResumed {
								actions.resume(queue)
								backStack.remove(Screen.SavedQueues)
							},
							onPreview = { previewTarget = queue },
							onSaveAsPlaylist = { saveTarget = queue },
							onRename = { renameTarget = queue },
							onDelete = { actions.delete(queue) }
						)
					}
				}

				// Both are destructive, but two full-width red slabs stacked at the bottom of the list
				// read as an alarm. Only the narrower-scoped action carries the error tint; "Clear
				// all" stays neutral until its confirm dialog, which is where the warning belongs.
				Spacer(Modifier.height(8.dp))
				if (activeId != null && rows.size > 1) {
					FormButton(
						onClick = { deleteOthersOpen = true },
						color = MaterialTheme.colorScheme.errorContainer
					) {
						Text(stringResource(Res.string.action_delete_other_queues))
					}
				}
				FormButton(onClick = { clearAllOpen = true }) {
					Text(
						stringResource(Res.string.action_clear_queue_history),
						color = MaterialTheme.colorScheme.error
					)
				}
			}
		}
	}

	renameTarget?.let { target ->
		QueueNameDialog(
			title = stringResource(Res.string.title_rename_queue),
			label = stringResource(Res.string.option_queue_name),
			initial = target.name ?: target.sourceName.orEmpty(),
			onConfirm = { newName ->
				actions.rename(target.id, newName)
				renameTarget = null
			},
			onDismissRequest = { renameTarget = null }
		)
	}

	saveTarget?.let { target ->
		QueueNameDialog(
			title = stringResource(Res.string.title_save_as_playlist),
			label = stringResource(Res.string.option_playlist_name),
			initial = target.name ?: target.sourceName.orEmpty(),
			onConfirm = { name ->
				viewModel.saveAsPlaylist(target.id, name)
				saveTarget = null
			},
			onDismissRequest = { saveTarget = null }
		)
	}

	previewTarget?.let { target ->
		SavedQueuePreviewSheet(
			queue = target,
			onResume = {
				previewTarget = null
				actions.resume(target)
				backStack.remove(Screen.SavedQueues)
			},
			onDismissRequest = { previewTarget = null }
		)
	}

	if (deleteOthersOpen && activeId != null) {
		ConfirmDialog(
			title = stringResource(Res.string.title_delete_other_queues),
			message = stringResource(Res.string.message_delete_other_queues),
			confirmLabel = stringResource(Res.string.action_delete_other_queues),
			onConfirm = {
				actions.deleteOthers(rows, activeId)
				deleteOthersOpen = false
			},
			onDismissRequest = { deleteOthersOpen = false }
		)
	}

	if (clearAllOpen) {
		ConfirmDialog(
			title = stringResource(Res.string.title_clear_queue_history),
			message = stringResource(Res.string.message_clear_queue_history),
			confirmLabel = stringResource(Res.string.action_clear_queue_history),
			onConfirm = {
				actions.clearAll(rows)
				clearAllOpen = false
			},
			onDismissRequest = { clearAllOpen = false }
		)
	}
}

@Composable
private fun SavedQueueRow(
	queue: SavedQueueEntity,
	isActive: Boolean,
	onClick: () -> Unit,
	onResume: () -> Unit,
	onPreview: () -> Unit,
	onSaveAsPlaylist: () -> Unit,
	onRename: () -> Unit,
	onDelete: () -> Unit
) {
	val displayName = savedQueueTitle(queue)
	val sourceLine = savedQueueSourceLine(queue)

	FormRow(
		onClick = onClick,
		color = if (isActive) MaterialTheme.colorScheme.surfaceContainerHighest else null
	) {
		// Art of the track this queue will resume on, matching the title — not a generic glyph, and
		// not a peer's cover URL (which points at ITS server with ITS auth).
		CoverArt(
			coverArtId = savedQueueCoverArtId(queue),
			contentDescription = null,
			modifier = Modifier.size(48.dp),
			shape = RoundedCornerShape(6.dp)
		)
		Spacer(Modifier.width(14.dp))
		Column(Modifier.weight(1f)) {
			Text(
				displayName,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis,
				fontWeight = if (isActive) FontWeight.SemiBold else null,
				color = if (isActive) MaterialTheme.colorScheme.primary
				else MaterialTheme.colorScheme.onSurface
			)
			// Names the source kind so generated sessions read differently from ordinary queues even
			// inside the "All" filter, and says outright which row is live — colour alone was easy to
			// miss, and Feishin labels it.
			Text(
				savedQueueSubtitle(queue, isActive),
				style = MaterialTheme.typography.bodyMedium,
				color = if (isActive) MaterialTheme.colorScheme.primary
				else MaterialTheme.colorScheme.onSurfaceVariant,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis
			)
			// Where it came from, only when the title isn't already saying it.
			sourceLine?.let { line ->
				Text(
					line,
					style = MaterialTheme.typography.bodySmall,
					color = MaterialTheme.colorScheme.onSurfaceVariant,
					maxLines = 1,
					overflow = TextOverflow.Ellipsis
				)
			}
		}

		var menuOpen by remember { mutableStateOf(false) }
		Box {
			IconButton(onClick = { menuOpen = true }) {
				Icon(Icons.Outlined.MoreVert, contentDescription = null)
			}
			Dropdown(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
				DropdownItem(
					text = { Text(stringResource(Res.string.action_restore_queue)) },
					onClick = {
						menuOpen = false
						onClick()
					},
					leadingIcon = { Icon(Icons.Outlined.Queue, null) }
				)
				DropdownItem(
					text = { Text(stringResource(Res.string.action_resume_queue)) },
					onClick = {
						menuOpen = false
						onResume()
					},
					leadingIcon = { Icon(Icons.Outlined.Queue, null) }
				)
				DropdownItem(
					text = { Text(stringResource(Res.string.action_preview_queue)) },
					onClick = {
						menuOpen = false
						onPreview()
					},
					leadingIcon = { Icon(Icons.Outlined.Queue, null) }
				)
				DropdownItem(
					text = { Text(stringResource(Res.string.action_save_as_playlist)) },
					onClick = {
						menuOpen = false
						onSaveAsPlaylist()
					},
					leadingIcon = { Icon(Icons.Outlined.Badge, null) }
				)
				DropdownItem(
					text = { Text(stringResource(Res.string.action_rename)) },
					onClick = {
						menuOpen = false
						onRename()
					},
					leadingIcon = { Icon(Icons.Outlined.Badge, null) }
				)
				DropdownItem(
					text = { Text(stringResource(Res.string.action_delete_queue)) },
					onClick = {
						menuOpen = false
						onDelete()
					},
					leadingIcon = { Icon(Icons.Outlined.Delete, null) }
				)
			}
		}
	}
}

@Composable
private fun ConfirmDialog(
	title: String,
	message: String,
	confirmLabel: String,
	onConfirm: () -> Unit,
	onDismissRequest: () -> Unit
) {
	FormDialog(
		onDismissRequest = onDismissRequest,
		icon = { Icon(Icons.Outlined.Delete, null) },
		title = { Text(title) },
		buttons = {
			FormButton(onClick = onConfirm, color = MaterialTheme.colorScheme.error) {
				Text(confirmLabel)
			}
			FormButton(onClick = onDismissRequest) {
				Text(stringResource(Res.string.action_cancel))
			}
		},
		content = { Text(message) }
	)
}

@Composable
private fun QueueNameDialog(
	title: String,
	label: String,
	initial: String,
	onConfirm: (String) -> Unit,
	onDismissRequest: () -> Unit
) {
	val nameState = rememberTextFieldState(initial)

	FormDialog(
		onDismissRequest = onDismissRequest,
		title = { Text(title) },
		buttons = {
			FormButton(
				onClick = { onConfirm(nameState.text.toString()) },
				color = MaterialTheme.colorScheme.primary
			) {
				Text(stringResource(Res.string.action_ok))
			}
			FormButton(onClick = onDismissRequest) {
				Text(stringResource(Res.string.action_cancel))
			}
		},
		content = {
			TextField(
				state = nameState,
				label = { Text(label) },
				lineLimits = TextFieldLineLimits.SingleLine
			)
		}
	)
}
