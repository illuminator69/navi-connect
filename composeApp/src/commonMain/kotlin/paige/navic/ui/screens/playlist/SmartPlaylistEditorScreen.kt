package paige.navic.ui.screens.playlist

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.kyant.capsule.ContinuousRoundedRectangle
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toImmutableList
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import org.koin.compose.koinInject
import paige.navic.LocalNavStack
import paige.navic.domain.manager.NativeApiManager
import paige.navic.domain.repositories.DbRepository
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Add
import paige.navic.icons.outlined.Close
import paige.navic.ui.components.common.Form
import paige.navic.ui.components.common.FormRow
import paige.navic.ui.components.common.FormTitle
import paige.navic.ui.components.layouts.NestedTopBar
import paige.navic.ui.navigation.Screen
import paige.navic.ui.screens.settings.components.SettingSelectionRow
import paige.navic.ui.screens.settings.components.SettingSwitchRow
import paige.navic.util.ui.rememberLibraryTabBackground

// ---------------------------------------------------------------------------
// Rule model: a curated subset of Navidrome's criteria fields/operators.
// ---------------------------------------------------------------------------

private enum class FieldType { BOOL, DATE, NUMBER, STRING }

private data class FieldDef(val key: String, val label: String, val type: FieldType)

private val FIELDS = listOf(
	FieldDef("title", "Title", FieldType.STRING),
	FieldDef("artist", "Artist", FieldType.STRING),
	FieldDef("album", "Album", FieldType.STRING),
	FieldDef("albumartist", "Album artist", FieldType.STRING),
	FieldDef("genre", "Genre", FieldType.STRING),
	FieldDef("comment", "Comment", FieldType.STRING),
	FieldDef("filetype", "File type", FieldType.STRING),
	FieldDef("year", "Year", FieldType.NUMBER),
	FieldDef("rating", "Rating (0-5)", FieldType.NUMBER),
	FieldDef("playcount", "Play count", FieldType.NUMBER),
	FieldDef("duration", "Duration (sec)", FieldType.NUMBER),
	FieldDef("bpm", "BPM", FieldType.NUMBER),
	FieldDef("bitrate", "Bitrate", FieldType.NUMBER),
	FieldDef("loved", "Favorite", FieldType.BOOL),
	FieldDef("dateadded", "Date added", FieldType.DATE),
	FieldDef("lastplayed", "Last played", FieldType.DATE)
)

private data class OperatorDef(val key: String, val label: String)

private fun operatorsFor(type: FieldType): List<OperatorDef> = when (type) {
	FieldType.STRING -> listOf(
		OperatorDef("contains", "contains"),
		OperatorDef("notContains", "doesn't contain"),
		OperatorDef("is", "is"),
		OperatorDef("isNot", "is not"),
		OperatorDef("startsWith", "starts with"),
		OperatorDef("endsWith", "ends with")
	)

	FieldType.NUMBER -> listOf(
		OperatorDef("is", "is"),
		OperatorDef("gt", "greater than"),
		OperatorDef("lt", "less than")
	)

	FieldType.BOOL -> listOf(OperatorDef("is", "is"))

	FieldType.DATE -> listOf(
		OperatorDef("inTheLast", "in the last (days)"),
		OperatorDef("notInTheLast", "not in the last (days)"),
		OperatorDef("before", "before (YYYY-MM-DD)"),
		OperatorDef("after", "after (YYYY-MM-DD)")
	)
}

private class RuleState {
	var field by mutableStateOf(FIELDS[0])
	var operator by mutableStateOf(operatorsFor(FIELDS[0].type)[0])
	var value by mutableStateOf("")
	var boolValue by mutableStateOf(true)
}

private val SORT_OPTIONS = listOf(
	"" to "Default",
	"title" to "Title",
	"artist" to "Artist",
	"album" to "Album",
	"year" to "Year",
	"dateadded" to "Date added",
	"lastplayed" to "Last played",
	"playcount" to "Play count",
	"rating" to "Rating",
	"random" to "Random"
)

private fun buildRules(
	matchAll: Boolean,
	rules: List<RuleState>,
	sort: String,
	descending: Boolean,
	limit: Int?
): JsonObject = buildJsonObject {
	putJsonArray(if (matchAll) "all" else "any") {
		rules.forEach { rule ->
			addJsonObject {
				putJsonObject(rule.operator.key) {
					when (rule.field.type) {
						FieldType.BOOL -> put(rule.field.key, rule.boolValue)
						FieldType.NUMBER -> put(rule.field.key, rule.value.toLongOrNull() ?: 0L)
						FieldType.DATE ->
							if (rule.operator.key == "inTheLast" || rule.operator.key == "notInTheLast") {
								put(rule.field.key, rule.value.toLongOrNull() ?: 30L)
							} else {
								put(rule.field.key, rule.value)
							}

						FieldType.STRING -> put(rule.field.key, rule.value)
					}
				}
			}
		}
	}
	if (sort.isNotEmpty()) put("sort", sort)
	put("order", if (descending) "desc" else "asc")
	limit?.let { put("limit", it) }
}

// ---------------------------------------------------------------------------

/**
 * A borderless input that reads as one more form row: no outline, its own
 * [surfaceContainerHigh][MaterialTheme.colorScheme] fill, rounded to match [FormRow].
 */
@Composable
private fun FormTextField(
	value: String,
	onValueChange: (String) -> Unit,
	placeholder: String,
	modifier: Modifier = Modifier,
	keyboardType: KeyboardType = KeyboardType.Text
) {
	TextField(
		value = value,
		onValueChange = onValueChange,
		placeholder = { Text(placeholder) },
		singleLine = true,
		keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
		modifier = modifier.clip(ContinuousRoundedRectangle(5.dp)),
		colors = TextFieldDefaults.colors(
			focusedContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
			unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
			focusedIndicatorColor = Color.Transparent,
			unfocusedIndicatorColor = Color.Transparent,
			disabledIndicatorColor = Color.Transparent,
			errorIndicatorColor = Color.Transparent
		)
	)
}

/**
 * Editor for Navidrome SERVER-SIDE smart playlists: rule rows are converted to
 * the criteria JSON the native API accepts; the server keeps the playlist
 * auto-updated and every Subsonic client sees it as a normal playlist.
 */
@Composable
fun SmartPlaylistEditorScreen() {
	val nativeApi = koinInject<NativeApiManager>()
	val dbRepository = koinInject<DbRepository>()
	val backStack = LocalNavStack.current
	val scope = rememberCoroutineScope()

	var name by remember { mutableStateOf("") }
	var matchAll by remember { mutableStateOf(true) }
	val rules = remember { mutableStateListOf(RuleState()) }
	var sort by remember { mutableStateOf(SORT_OPTIONS[0]) }
	var descending by remember { mutableStateOf(false) }
	var limitText by remember { mutableStateOf("") }
	var isPublic by remember { mutableStateOf(false) }
	var saving by remember { mutableStateOf(false) }
	var error by remember { mutableStateOf<String?>(null) }

	Scaffold(topBar = { NestedTopBar({ Text("New smart playlist") }) }) { innerPadding ->
		Column(
			modifier = Modifier
				.padding(top = innerPadding.calculateTopPadding())
				.fillMaxSize()
				.background(rememberLibraryTabBackground())
				.verticalScroll(rememberScrollState())
				.padding(horizontal = 16.dp, vertical = 8.dp)
		) {
			// --- General ---------------------------------------------------
			FormTitle("General")
			Form {
				FormRow {
					FormTextField(
						value = name,
						onValueChange = { name = it },
						placeholder = "Playlist name",
						modifier = Modifier.fillMaxWidth()
					)
				}
				SettingSelectionRow(
					title = { Text("Match") },
					items = persistentListOf(true, false),
					label = { if (it) "All rules" else "Any rule" },
					selection = matchAll,
					onSelect = { matchAll = it }
				)
			}

			// --- Rules -----------------------------------------------------
			FormTitle("Rules")
			rules.forEachIndexed { index, rule ->
				Form {
					FormRow {
						Text(
							"Rule ${index + 1}",
							style = MaterialTheme.typography.titleSmall,
							modifier = Modifier.weight(1f)
						)
						if (rules.size > 1) {
							IconButton(onClick = { rules.removeAt(index) }) {
								Icon(
									Icons.Outlined.Close,
									contentDescription = "Remove rule",
									tint = MaterialTheme.colorScheme.error,
									modifier = Modifier.size(20.dp)
								)
							}
						}
					}
					SettingSelectionRow(
						title = { Text("Field") },
						items = FIELDS.toImmutableList(),
						label = { it.label },
						selection = rule.field,
						onSelect = { picked ->
							rule.field = picked
							rule.operator = operatorsFor(picked.type)[0]
							rule.value = ""
						}
					)
					SettingSelectionRow(
						title = { Text("Condition") },
						items = operatorsFor(rule.field.type).toImmutableList(),
						label = { it.label },
						selection = rule.operator,
						onSelect = { rule.operator = it }
					)
					if (rule.field.type == FieldType.BOOL) {
						SettingSwitchRow(
							title = { Text("Value") },
							subtitle = { Text(if (rule.boolValue) "true" else "false") },
							value = rule.boolValue,
							onSetValue = { rule.boolValue = it }
						)
					} else {
						FormRow {
							FormTextField(
								value = rule.value,
								onValueChange = { rule.value = it },
								placeholder = when (rule.field.type) {
									FieldType.NUMBER -> "Number"
									FieldType.DATE ->
										if (rule.operator.key == "inTheLast" ||
											rule.operator.key == "notInTheLast"
										) "Days" else "YYYY-MM-DD"

									else -> "Value"
								},
								keyboardType = if (rule.field.type == FieldType.NUMBER)
									KeyboardType.Number else KeyboardType.Text,
								modifier = Modifier.fillMaxWidth()
							)
						}
					}
				}
			}

			OutlinedButton(
				onClick = { rules.add(RuleState()) },
				modifier = Modifier.fillMaxWidth()
			) {
				Icon(Icons.Outlined.Add, contentDescription = null, modifier = Modifier.size(18.dp))
				Spacer(Modifier.width(8.dp))
				Text("Add rule")
			}

			Spacer(Modifier.height(20.dp))

			// --- Sorting ---------------------------------------------------
			FormTitle("Sorting")
			Form {
				SettingSelectionRow(
					title = { Text("Sort by") },
					items = SORT_OPTIONS.toImmutableList(),
					label = { it.second },
					selection = sort,
					onSelect = { sort = it }
				)
				SettingSelectionRow(
					title = { Text("Order") },
					items = persistentListOf(false, true),
					label = { if (it) "Descending" else "Ascending" },
					selection = descending,
					onSelect = { descending = it }
				)
				FormRow {
					FormTextField(
						value = limitText,
						onValueChange = { limitText = it },
						placeholder = "Limit (optional)",
						keyboardType = KeyboardType.Number,
						modifier = Modifier.fillMaxWidth()
					)
				}
			}

			// --- Visibility ------------------------------------------------
			FormTitle("Visibility")
			Form {
				SettingSwitchRow(
					title = { Text("Public") },
					subtitle = { Text("Visible to everyone on the server") },
					value = isPublic,
					onSetValue = { isPublic = it }
				)
			}

			error?.let {
				Text(
					it,
					color = MaterialTheme.colorScheme.error,
					modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp)
				)
			}

			Button(
				enabled = !saving && name.isNotBlank() &&
					rules.all { it.field.type == FieldType.BOOL || it.value.isNotBlank() },
				modifier = Modifier.fillMaxWidth().height(52.dp),
				onClick = {
					saving = true
					error = null
					scope.launch {
						val result = nativeApi.createSmartPlaylist(
							name = name.trim(),
							comment = "Created with Navic",
							isPublic = isPublic,
							rules = buildRules(
								matchAll = matchAll,
								rules = rules,
								sort = sort.first,
								descending = descending,
								limit = limitText.toIntOrNull()
							)
						)
						saving = false
						result
							.onSuccess {
								dbRepository.syncPlaylists()
								backStack.remove(Screen.SmartPlaylistEditor)
							}
							.onFailure { error = it.message ?: "Failed to create playlist" }
					}
				}
			) {
				if (saving) CircularProgressIndicator(Modifier.height(20.dp))
				else Text("Create smart playlist")
			}
			Spacer(Modifier.height(24.dp))
		}
	}
}
