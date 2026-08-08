package paige.navic.ui.components.sheets

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.koin.compose.koinInject
import paige.navic.domain.manager.CastManager
import paige.navic.domain.manager.HubDevice
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.util.ui.rememberNowPlayingCoverAmbient
import kotlin.math.roundToInt

/**
 * Spotify-style "Connect to a device" sheet: lists navi-connect devices and
 * transfers playback to the chosen one (the hub resumes it from the same spot,
 * preserving the play/pause state). Each row shows the device name + platform +
 * status so two clients are distinguishable; offline devices auto-hide and
 * manually-hidden ones are tucked behind a "Show offline & hidden" toggle.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DevicePickerSheet(onDismissRequest: () -> Unit) {
	val hubManager = koinInject<HubManager>()
	val castManager = koinInject<CastManager>()
	val preferenceManager = koinInject<PreferenceManager>()
	val connected by hubManager.connected.collectAsState()
	val connectionError by hubManager.connectionError.collectAsState()
	val devices by hubManager.devices.collectAsState()
	val myDeviceId by hubManager.myDeviceId.collectAsState()
	val activeDeviceId by hubManager.activeDeviceId.collectAsState()
	val castDevices by castManager.devices.collectAsState()
	val castConnectedName by castManager.connectedName.collectAsState()

	// Snapshot-backed pref → reading recomposes when the hidden set changes.
	val hiddenIds = remember(preferenceManager.hubHiddenDeviceIds) {
		preferenceManager.hubHiddenDeviceIds.split(",").filter { it.isNotBlank() }.toSet()
	}
	val toggleHidden: (String, Boolean) -> Unit = { id, hide ->
		val cur = preferenceManager.hubHiddenDeviceIds
			.split(",").filter { it.isNotBlank() }.toMutableSet()
		if (hide) cur.add(id) else cur.remove(id)
		preferenceManager.hubHiddenDeviceIds = cur.joinToString(",")
	}
	var showExtra by remember { mutableStateOf(false) }

	// Cast discovery only runs while the picker is open (active scanning is
	// battery hungry); the device list itself stays cached between openings,
	// so the sheet renders instantly and the scan refreshes it in place.
	DisposableEffect(Unit) {
		castManager.startDiscovery()
		onDispose { castManager.stopDiscovery() }
	}

	val visibleDevices = devices.filter { it.online && it.id !in hiddenIds }
	val extraDevices = devices.filter { !it.online || it.id in hiddenIds }

	val renderDevice: @Composable (HubDevice) -> Unit = { device ->
		val isHidden = device.id in hiddenIds
		Row(
			modifier = Modifier.fillMaxWidth(),
			horizontalArrangement = Arrangement.SpaceBetween,
			verticalAlignment = Alignment.CenterVertically
		) {
			Column(Modifier.weight(1f)) {
				Text(device.name, style = MaterialTheme.typography.bodyLarge)
				Text(
					buildString {
						append(
							when (device.platform) {
								"desktop" -> "Desktop"
								"android" -> "Android"
								"chromecast" -> "Cast"
								else -> device.platform.replaceFirstChar { it.uppercase() }
							}
						)
						append(" · ")
						append(
							when {
								device.isActive -> "playing"
								device.id == myDeviceId -> "this device"
								device.online -> "online"
								else -> "offline"
							}
						)
					},
					style = MaterialTheme.typography.bodySmall,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			}
			Row(
				horizontalArrangement = Arrangement.spacedBy(4.dp),
				verticalAlignment = Alignment.CenterVertically
			) {
				if (device.online && device.id != activeDeviceId) {
					Button(
						onClick = {
							hubManager.transfer(device.id)
							onDismissRequest()
						}
					) {
						Text(if (device.id == myDeviceId) "Play here" else "Transfer")
					}
				}
				TextButton(onClick = { toggleHidden(device.id, !isHidden) }) {
					Text(if (isHidden) "Unhide" else "Hide")
				}
			}
		}
	}

	ModalBottomSheet(
		onDismissRequest = onDismissRequest,
		sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
		// Cover-tinted surface + cover-themed content (see the shared wrapper).
		ambient = rememberNowPlayingCoverAmbient()
	) {
		Column(
			modifier = Modifier.padding(horizontal = 24.dp),
			verticalArrangement = Arrangement.spacedBy(12.dp)
		) {
			Text(
				"Connect to a device",
				style = MaterialTheme.typography.titleLarge,
				color = MaterialTheme.colorScheme.primary
			)
			if (!connected) {
				Text(
					connectionError ?: "Not connected to the hub",
					style = MaterialTheme.typography.bodyMedium,
					color = if (connectionError != null) MaterialTheme.colorScheme.error
						else MaterialTheme.colorScheme.onSurfaceVariant
				)
			} else if (visibleDevices.isEmpty() && extraDevices.isEmpty()) {
				Text(
					"No devices found",
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			}

			visibleDevices.forEach { renderDevice(it) }

			if (extraDevices.isNotEmpty()) {
				TextButton(onClick = { showExtra = !showExtra }) {
					Text((if (showExtra) "Hide" else "Show") + " offline & hidden (${extraDevices.size})")
				}
				if (showExtra) extraDevices.forEach { renderDevice(it) }
			}

			// Volume for the active remote device (mirrors Feishin's remote-bar
			// volume). The hub forwards `do setVolume` to that device ONLY, so it
			// never touches this device's local volume.
			val activeDevice = devices.firstOrNull { it.id == activeDeviceId }
			if (connected && activeDevice != null && activeDevice.id != myDeviceId) {
				var volume by remember(activeDevice.volume) {
					mutableStateOf(activeDevice.volume.toFloat())
				}
				Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
					Text(
						"Volume · ${activeDevice.name}",
						style = MaterialTheme.typography.titleMedium
					)
					Slider(
						value = volume,
						onValueChange = { volume = it },
						// Commit on release only — don't flood the socket while dragging.
						onValueChangeFinished = { hubManager.actSetVolume(volume.roundToInt()) },
						valueRange = 0f..100f
					)
				}
			}

			if (castDevices.isNotEmpty() || castConnectedName != null) {
				Text("Cast devices", style = MaterialTheme.typography.titleMedium)
				castDevices.forEach { cast ->
					val isCasting = castConnectedName == cast.name
					Row(
						modifier = Modifier.fillMaxWidth(),
						horizontalArrangement = Arrangement.SpaceBetween,
						verticalAlignment = Alignment.CenterVertically
					) {
						Column(Modifier.weight(1f)) {
							Text(cast.name, style = MaterialTheme.typography.bodyLarge)
							Text(
								if (isCasting) "casting from this device" else "Chromecast",
								style = MaterialTheme.typography.bodySmall,
								color = MaterialTheme.colorScheme.onSurfaceVariant
							)
						}
						Button(
							onClick = {
								if (isCasting) castManager.disconnect()
								else castManager.connect(cast.id)
								onDismissRequest()
							}
						) {
							Text(if (isCasting) "Disconnect" else "Cast")
						}
					}
				}
			}
			Spacer(Modifier.height(24.dp))
		}
	}
}
