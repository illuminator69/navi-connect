package paige.navic.ui.screens.savedqueues

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.rememberTextFieldState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.dropUnlessResumed
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_cancel
import navic.composeapp.generated.resources.action_delete_other_queues
import navic.composeapp.generated.resources.action_delete_queue
import navic.composeapp.generated.resources.action_ok
import navic.composeapp.generated.resources.action_rename
import navic.composeapp.generated.resources.action_restore_queue
import navic.composeapp.generated.resources.action_resume_queue
import navic.composeapp.generated.resources.action_save_as_playlist
import navic.composeapp.generated.resources.filter_all
import navic.composeapp.generated.resources.info_no_saved_queues
import navic.composeapp.generated.resources.message_delete_other_queues
import navic.composeapp.generated.resources.message_save_playlist_failed
import navic.composeapp.generated.resources.message_saved_as_playlist
import navic.composeapp.generated.resources.option_playlist_name
import navic.composeapp.generated.resources.option_queue_name
import navic.composeapp.generated.resources.queue_kind_album
import navic.composeapp.generated.resources.queue_kind_journey
import navic.composeapp.generated.resources.queue_kind_manual
import navic.composeapp.generated.resources.queue_kind_mood_flow
import navic.composeapp.generated.resources.queue_kind_playlist
import navic.composeapp.generated.resources.queue_kind_radio
import navic.composeapp.generated.resources.queue_song_count
import navic.composeapp.generated.resources.queue_unnamed
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
import paige.navic.ui.components.common.Dropdown
import paige.navic.ui.components.common.DropdownItem
import paige.navic.ui.components.common.Form
import paige.navic.ui.components.common.FormButton
import paige.navic.ui.components.common.FormRow
import paige.navic.ui.components.dialogs.FormDialog
import paige.navic.ui.components.layouts.NestedTopBar
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.savedqueues.viewmodels.SavedQueueMessage
import paige.navic.ui.screens.savedqueues.viewmodels.SavedQueuesViewModel

/**
 * The Symfonium-style "select media queue" list: every queue Navic has auto-captured (rolling cache
 * of the 20 most recent), most-recent first. A chip row filters by how the queue was made (album /
 * playlist / radio / Mood Flow / journey / manual). Tapping a row restores it PAUSED where it left
 * off; a per-row overflow resumes it playing, saves it as a Navidrome playlist, renames, or deletes;
 * the currently-playing queue is highlighted.
 */
@Composable
fun SavedQueuesScreen() {
	val viewModel = koinViewModel<SavedQueuesViewModel>()
	val mediaPlayer = koinInject<MediaPlayerViewModel>()
	val backStack = LocalNavStack.current

	val queues by viewModel.queues.collectAsStateWithLifecycle()
	val message by viewModel.message.collectAsStateWithLifecycle()
	val playerState by mediaPlayer.localUiState.collectAsState()
	val activeId = playerState.savedQueueId

	var renameTarget by remember { mutableStateOf<SavedQueueEntity?>(null) }
	var saveTarget by remember { mutableStateOf<SavedQueueEntity?>(null) }
	var deleteOthersOpen by remember { mutableStateOf(false) }
	// null = "All"; otherwise a SavedQueueSource kind.
	var kindFilter by remember { mutableStateOf<String?>(null) }

	val snackbarHostState = remember { SnackbarHostState() }
	val savedMsg = stringResource(Res.string.message_saved_as_playlist)
	val failedMsg = stringResource(Res.string.message_save_playlist_failed)
	LaunchedEffect(message) {
		when (message) {
			SavedQueueMessage.SavedAsPlaylist -> snackbarHostState.showSnackbar(savedMsg)
			SavedQueueMessage.Error -> snackbarHostState.showSnackbar(failedMsg)
			null -> {}
		}
		if (message != null) viewModel.clearMessage()
	}

	// Kinds actually present, in a stable canonical order, so the chip row only offers real options.
	val presentKinds = remember(queues) {
		SavedQueueSource.ALL.filter { kind -> queues.any { it.sourceKind == kind } }
	}
	val visibleQueues = remember(queues, kindFilter) {
		kindFilter?.let { k -> queues.filter { it.sourceKind == k } } ?: queues
	}

	Scaffold(
		topBar = { NestedTopBar({ Text(stringResource(Res.string.title_saved_queues)) }) },
		snackbarHost = { SnackbarHost(snackbarHostState) },
		contentWindowInsets = WindowInsets.statusBars
	) { innerPadding ->
		if (queues.isEmpty()) {
			ContentUnavailable(
				modifier = Modifier
					.padding(innerPadding)
					.fillMaxSize(),
				icon = Icons.Outlined.Queue,
				label = stringResource(Res.string.info_no_saved_queues)
			)
		} else {
			Column(
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
								mediaPlayer.swapToSavedQueue(queue.id, play = false)
								backStack.remove(Screen.SavedQueues)
							},
							onResume = dropUnlessResumed {
								mediaPlayer.swapToSavedQueue(queue.id, play = true)
								backStack.remove(Screen.SavedQueues)
							},
							onSaveAsPlaylist = { saveTarget = queue },
							onRename = { renameTarget = queue },
							onDelete = { viewModel.delete(queue.id) }
						)
					}
				}

				// Only meaningful with a live queue to keep and something else to drop.
				if (activeId != null && queues.size > 1) {
					FormButton(
						onClick = { deleteOthersOpen = true },
						color = MaterialTheme.colorScheme.errorContainer
					) {
						Text(stringResource(Res.string.action_delete_other_queues))
					}
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
				viewModel.rename(target.id, newName)
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

	if (deleteOthersOpen && activeId != null) {
		FormDialog(
			onDismissRequest = { deleteOthersOpen = false },
			icon = { Icon(Icons.Outlined.Delete, null) },
			title = { Text(stringResource(Res.string.title_delete_other_queues)) },
			buttons = {
				FormButton(
					onClick = {
						viewModel.deleteOthers(activeId)
						deleteOthersOpen = false
					},
					color = MaterialTheme.colorScheme.error
				) {
					Text(stringResource(Res.string.action_delete_other_queues))
				}
				FormButton(onClick = { deleteOthersOpen = false }) {
					Text(stringResource(Res.string.action_cancel))
				}
			},
			content = { Text(stringResource(Res.string.message_delete_other_queues)) }
		)
	}
}

/** Human label for a [SavedQueueSource] kind. Unknown/newer kinds fall back to the raw value. */
@Composable
private fun queueKindLabel(kind: String): String = when (kind) {
	SavedQueueSource.MANUAL -> stringResource(Res.string.queue_kind_manual)
	SavedQueueSource.ALBUM -> stringResource(Res.string.queue_kind_album)
	SavedQueueSource.PLAYLIST -> stringResource(Res.string.queue_kind_playlist)
	SavedQueueSource.RADIO -> stringResource(Res.string.queue_kind_radio)
	SavedQueueSource.MOOD_FLOW -> stringResource(Res.string.queue_kind_mood_flow)
	SavedQueueSource.JOURNEY -> stringResource(Res.string.queue_kind_journey)
	else -> kind
}

@Composable
private fun SavedQueueRow(
	queue: SavedQueueEntity,
	isActive: Boolean,
	onClick: () -> Unit,
	onResume: () -> Unit,
	onSaveAsPlaylist: () -> Unit,
	onRename: () -> Unit,
	onDelete: () -> Unit
) {
	val displayName = queue.name?.takeIf { it.isNotBlank() }
		?: queue.sourceName?.takeIf { it.isNotBlank() }
		?: stringResource(Res.string.queue_unnamed)

	FormRow(
		onClick = onClick,
		color = if (isActive) MaterialTheme.colorScheme.surfaceContainerHighest else null
	) {
		Icon(
			Icons.Outlined.Queue,
			contentDescription = null,
			modifier = Modifier.size(22.dp),
			tint = if (isActive) MaterialTheme.colorScheme.primary
			else MaterialTheme.colorScheme.onSurfaceVariant
		)
		Spacer(Modifier.width(14.dp))
		Column(Modifier.weight(1f)) {
			Text(
				displayName,
				maxLines = 1,
				overflow = TextOverflow.Ellipsis,
				color = if (isActive) MaterialTheme.colorScheme.primary
				else MaterialTheme.colorScheme.onSurface
			)
			// Subtitle names the source kind so generated sessions read differently from ordinary
			// queues even inside the "All" filter.
			Text(
				"${queueKindLabel(queue.sourceKind)} · " +
					stringResource(Res.string.queue_song_count, queue.songCount),
				style = MaterialTheme.typography.bodyMedium,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
		}

		var menuOpen by remember { mutableStateOf(false) }
		androidx.compose.foundation.layout.Box {
			androidx.compose.material3.IconButton(onClick = { menuOpen = true }) {
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
