package paige.navic.ui.screens.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalMinimumInteractiveComponentSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.info_bitrate_default_zero
import navic.composeapp.generated.resources.info_in_use
import navic.composeapp.generated.resources.info_streaming_quality
import navic.composeapp.generated.resources.option_enable_custom_bitrates
import navic.composeapp.generated.resources.option_max_bitrate_cellular
import navic.composeapp.generated.resources.option_prefer_downloads_cellular
import navic.composeapp.generated.resources.option_max_bitrate_wifi
import navic.composeapp.generated.resources.subtitle_max_bitrates
import navic.composeapp.generated.resources.title_advanced
import navic.composeapp.generated.resources.title_cellular
import navic.composeapp.generated.resources.title_cellular_playback_source
import navic.composeapp.generated.resources.title_download_format
import navic.composeapp.generated.resources.title_download_quality
import navic.composeapp.generated.resources.title_streaming_quality
import navic.composeapp.generated.resources.title_wifi
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import paige.navic.LocalPlatformContext
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.models.settings.StreamingQuality
import paige.navic.domain.models.settings.description
import paige.navic.icons.Icons
import paige.navic.icons.outlined.Info
import paige.navic.ui.components.common.Form
import paige.navic.ui.components.common.FormRow
import paige.navic.ui.components.common.FormTitle
import paige.navic.ui.components.layouts.NestedTopBar

@Composable
fun SettingsStreamingQualityScreen() {
	val preferenceManager = koinInject<PreferenceManager>()
	val platformContext = LocalPlatformContext.current
	val connectivityManager = koinInject<ConnectivityManager>()
	val isOnline by connectivityManager.isOnline.collectAsStateWithLifecycle()
	val isCellular by connectivityManager.isCellular.collectAsStateWithLifecycle()

	var isAdvancedActive by remember { mutableStateOf(preferenceManager.isAdvancedTranscodingActive) }
	var downloadBitrate by remember { mutableStateOf(preferenceManager.downloadBitrate) }
	var downloadFormat by remember { mutableStateOf(preferenceManager.downloadFormat) }
	var preferDownloadsOnCellular by remember {
		mutableStateOf(preferenceManager.preferDownloadsOnCellular)
	}

	Scaffold(
		topBar = {
			NestedTopBar(
				{ Text(stringResource(Res.string.title_streaming_quality)) },
				hideBack = platformContext.sizeClass.widthSizeClass >= WindowWidthSizeClass.Medium
			)
		},
		contentWindowInsets = WindowInsets.statusBars
	) { innerPadding ->
		CompositionLocalProvider(
			LocalMinimumInteractiveComponentSize provides 0.dp
		) {
			Column(
				Modifier
					.padding(innerPadding)
					.verticalScroll(rememberScrollState())
					.padding(top = 16.dp, end = 16.dp, start = 16.dp, bottom = 32.dp)
			) {
				AnimatedVisibility(visible = !isAdvancedActive) {
					Column {
						FormTitle(buildString {
							append(stringResource(Res.string.title_wifi))
							if (isOnline && !isCellular) {
								append(' ' + stringResource(Res.string.info_in_use))
							}
						})
						Form(Modifier.selectableGroup()) {
							RadioButtons(
								value = preferenceManager.streamingQualityWifi,
								onChangeValue = { preferenceManager.streamingQualityWifi = it }
							)
						}

						FormTitle(buildString {
							append(stringResource(Res.string.title_cellular))
							if (isOnline && isCellular) {
								append(' ' + stringResource(Res.string.info_in_use))
							}
						})
						Form(Modifier.selectableGroup()) {
							RadioButtons(
								value = preferenceManager.streamingQualityCellular,
								onChangeValue = { preferenceManager.streamingQualityCellular = it }
							)
						}

					}
				}

				// Downloads get their OWN quality. A stream is thrown away; a download is kept, so
				// it shouldn't silently inherit the bitrate chosen to save mobile data. Bitrate and
				// container are separate so "original FLAC" and "320 kbps MP3" are both sayable —
				// the old single tier list stopped at 192 and always forced Opus. Outside the
				// advanced-mode toggle above: this is independent of the streaming bitrates.
				// Changing it affects NEW downloads; existing files keep what they were fetched at,
				// and a retry reuses that too.
				FormTitle(stringResource(Res.string.title_download_quality))
				Form(Modifier.selectableGroup()) {
					DownloadBitrateRadioButtons(
						value = downloadBitrate,
						onChangeValue = {
							downloadBitrate = it
							preferenceManager.downloadBitrate = it
						}
					)
				}

				FormTitle(stringResource(Res.string.title_download_format))
				Form(Modifier.selectableGroup()) {
					DownloadFormatRadioButtons(
						value = downloadFormat,
						enabled = downloadBitrate > 0,
						onChangeValue = {
							downloadFormat = it
							preferenceManager.downloadFormat = it
						}
					)
				}

				// Which copy wins on a metered link. Only matters once downloads are transcodes:
				// at Original bitrate the downloaded file IS the server file.
				FormTitle(stringResource(Res.string.title_cellular_playback_source))
				Form {
					val interactionSource = remember { MutableInteractionSource() }
					FormRow(
						modifier = Modifier.clickable(
							interactionSource = interactionSource,
							indication = null,
							onClick = {
								preferDownloadsOnCellular = !preferDownloadsOnCellular
								preferenceManager.preferDownloadsOnCellular = preferDownloadsOnCellular
							}
						),
						horizontalArrangement = Arrangement.SpaceBetween,
						contentPadding = PaddingValues(16.dp)
					) {
						Column(Modifier.weight(1f)) {
							Text(
								text = stringResource(Res.string.option_prefer_downloads_cellular),
								style = MaterialTheme.typography.bodyLarge
							)
							Text(
								text = if (preferDownloadsOnCellular) {
									"Plays your downloaded copy on mobile data — uses no data."
								} else {
									"Streams the server's original on mobile data — uses data."
								},
								style = MaterialTheme.typography.bodyMedium,
								color = MaterialTheme.colorScheme.onSurfaceVariant
							)
						}
						Switch(
							checked = preferDownloadsOnCellular,
							onCheckedChange = {
								preferDownloadsOnCellular = it
								preferenceManager.preferDownloadsOnCellular = it
							}
						)
					}
				}

				Spacer(Modifier.height(16.dp))
				FormTitle(stringResource(Res.string.title_advanced))

				Form {
					val interactionSource = remember { MutableInteractionSource() }
					FormRow(
						modifier = Modifier.clickable(
							interactionSource = interactionSource,
							indication = null,
							onClick = {
								isAdvancedActive = !isAdvancedActive
								preferenceManager.isAdvancedTranscodingActive = isAdvancedActive
							}
						),
						horizontalArrangement = Arrangement.SpaceBetween,
						contentPadding = PaddingValues(16.dp)
					) {
						Text(
							text = stringResource(Res.string.option_enable_custom_bitrates),
							style = MaterialTheme.typography.bodyLarge
						)
						Switch(
							checked = isAdvancedActive,
							onCheckedChange = {
								isAdvancedActive = it
								preferenceManager.isAdvancedTranscodingActive = it
							}
						)
					}

					AnimatedVisibility(visible = isAdvancedActive) {
						Column(Modifier.padding(16.dp)) {
							Text(
								text = stringResource(Res.string.subtitle_max_bitrates),
								style = MaterialTheme.typography.bodyMedium,
								color = MaterialTheme.colorScheme.onSurfaceVariant
							)

							Spacer(Modifier.height(16.dp))

							var wifiInput by remember {
								val current = preferenceManager.customMaxBitrateWifi
								mutableStateOf(if (current > 0) current.toString() else "")
							}

							OutlinedTextField(
								value = wifiInput,
								onValueChange = { newValue ->
									if (newValue.isEmpty() || newValue.all { it.isDigit() }) {
										wifiInput = newValue
										preferenceManager.customMaxBitrateWifi = newValue.toIntOrNull() ?: 0
									}
								},
								label = { Text(stringResource(Res.string.option_max_bitrate_wifi)) },
								placeholder = { Text("0") },
								supportingText = {
									Text(stringResource(Res.string.info_bitrate_default_zero))
								},
								keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
								modifier = Modifier.fillMaxWidth(),
								singleLine = true
							)

							Spacer(Modifier.height(16.dp))

							var cellularInput by remember {
								val current = preferenceManager.customMaxBitrateCellular
								mutableStateOf(if (current > 0) current.toString() else "")
							}

							OutlinedTextField(
								value = cellularInput,
								onValueChange = { newValue ->
									if (newValue.isEmpty() || newValue.all { it.isDigit() }) {
										cellularInput = newValue
										preferenceManager.customMaxBitrateCellular = newValue.toIntOrNull() ?: 0
									}
								},
								label = { Text(stringResource(Res.string.option_max_bitrate_cellular)) },
								placeholder = { Text("0") },
								supportingText = {
									Text(stringResource(Res.string.info_bitrate_default_zero))
								},
								keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
								modifier = Modifier.fillMaxWidth(),
								singleLine = true
							)

						}
					}
				}

				Spacer(Modifier.height(24.dp))
				Row(
					modifier = Modifier.padding(horizontal = 8.dp),
					horizontalArrangement = Arrangement.spacedBy(16.dp)
				) {
					Icon(
						Icons.Outlined.Info,
						contentDescription = null,
						tint = MaterialTheme.colorScheme.onSurfaceVariant
					)
					Text(
						stringResource(Res.string.info_streaming_quality),
						color = MaterialTheme.colorScheme.onSurfaceVariant,
						style = MaterialTheme.typography.bodyMedium
					)
				}
			}
		}
	}
}

/** Download bitrate. 0 = ask the server for the original file, so a FLAC stays a FLAC. */
private val DOWNLOAD_BITRATES = listOf(0, 320, 256, 192, 128)

/** Download container. "" = whatever the original is; otherwise an explicit transcode target. */
private val DOWNLOAD_FORMATS = listOf("" to "Original", "opus" to "Opus", "mp3" to "MP3")

@Composable
private fun DownloadBitrateRadioButtons(
	value: Int,
	onChangeValue: (Int) -> Unit
) {
	DOWNLOAD_BITRATES.forEach { bitrate ->
		val interactionSource = remember { MutableInteractionSource() }

		FormRow(
			modifier = Modifier.selectable(
				selected = value == bitrate,
				interactionSource = interactionSource,
				onClick = { onChangeValue(bitrate) },
				role = Role.RadioButton
			),
			horizontalArrangement = Arrangement.spacedBy(14.dp),
			interactionSource = interactionSource,
			contentPadding = PaddingValues(16.dp)
		) {
			RadioButton(selected = value == bitrate, onClick = null)

			Column(Modifier.weight(1f)) {
				Text(if (bitrate == 0) "Original" else "$bitrate kbps")

				if (bitrate == 0) {
					AnimatedVisibility(visible = value == bitrate) {
						Text(
							text = "The server's own file, untouched — FLAC stays FLAC.",
							style = MaterialTheme.typography.bodyMedium,
							color = MaterialTheme.colorScheme.onSurfaceVariant
						)
					}
				}
			}
		}
	}
}

@Composable
private fun DownloadFormatRadioButtons(
	value: String,
	enabled: Boolean,
	onChangeValue: (String) -> Unit
) {
	DOWNLOAD_FORMATS.forEach { (format, label) ->
		val interactionSource = remember { MutableInteractionSource() }
		val selected = value == format

		FormRow(
			modifier = Modifier.selectable(
				selected = selected,
				enabled = enabled,
				interactionSource = interactionSource,
				onClick = { onChangeValue(format) },
				role = Role.RadioButton
			),
			horizontalArrangement = Arrangement.spacedBy(14.dp),
			interactionSource = interactionSource,
			contentPadding = PaddingValues(16.dp)
		) {
			RadioButton(selected = selected, enabled = enabled, onClick = null)

			Column(Modifier.weight(1f)) {
				Text(
					label,
					// Greyed at Original bitrate: there is no transcode to pick a container for.
					color = if (enabled) Color.Unspecified
					else MaterialTheme.colorScheme.onSurfaceVariant
				)
			}
		}
	}
}

@Composable
private fun RadioButtons(
	value: StreamingQuality,
	onChangeValue: (StreamingQuality) -> Unit
) {
	StreamingQuality.entries.forEach { quality ->
		val interactionSource = remember { MutableInteractionSource() }

		FormRow(
			modifier = Modifier.selectable(
				selected = value == quality,
				interactionSource = interactionSource,
				onClick = { onChangeValue(quality) },
				role = Role.RadioButton
			),
			horizontalArrangement = Arrangement.spacedBy(14.dp),
			interactionSource = interactionSource,
			contentPadding = PaddingValues(16.dp)
		) {
			RadioButton(
				selected = value == quality,
				onClick = null
			)

			Column(Modifier.weight(1f)) {
				Text(stringResource(quality.displayName))

				quality.description()?.let { description ->
					AnimatedVisibility(
						visible = value == quality
					) {
						Text(
							text = description,
							style = MaterialTheme.typography.bodyMedium,
							color = MaterialTheme.colorScheme.onSurfaceVariant
						)
					}
				}
			}
		}
	}
}
