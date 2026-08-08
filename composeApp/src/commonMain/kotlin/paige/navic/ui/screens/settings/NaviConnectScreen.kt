package paige.navic.ui.screens.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.koin.compose.koinInject
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.ui.components.layouts.NestedTopBar

/**
 * navi-connect settings: hub connection (enable/url/token/name) and the device
 * list with Spotify-style playback transfer. Strings are intentionally not
 * localised — this is a personal-fork feature.
 */
@Composable
fun NaviConnectScreen() {
	val preferenceManager = koinInject<PreferenceManager>()
	val hubManager = koinInject<HubManager>()

	val connected by hubManager.connected.collectAsState()
	val devices by hubManager.devices.collectAsState()
	val myDeviceId by hubManager.myDeviceId.collectAsState()
	val activeDeviceId by hubManager.activeDeviceId.collectAsState()
	val connectionError by hubManager.connectionError.collectAsState()

	// Surface a hub-side error (bad token, target offline, …) the moment it arrives, so a wrong
	// token reads as an actual error instead of "just never connects".
	val snackbarHostState = remember { SnackbarHostState() }
	LaunchedEffect(connectionError) {
		connectionError?.let { snackbarHostState.showSnackbar(it) }
	}

	var enabled by rememberSaveable { mutableStateOf(preferenceManager.hubEnabled) }
	var url by rememberSaveable { mutableStateOf(preferenceManager.hubUrl) }
	var token by rememberSaveable { mutableStateOf(preferenceManager.hubToken) }
	var deviceName by rememberSaveable { mutableStateOf(preferenceManager.hubDeviceName) }

	var audioMuseUrl by rememberSaveable { mutableStateOf(preferenceManager.audioMuseUrl) }
	var audioMuseToken by rememberSaveable { mutableStateOf(preferenceManager.audioMuseToken) }

	fun applyAndReconnect() {
		preferenceManager.hubEnabled = enabled
		preferenceManager.hubUrl = url.trim()
		preferenceManager.hubToken = token.trim()
		preferenceManager.hubDeviceName = deviceName.trim().ifBlank { "Navic" }
		hubManager.restart()
	}

	fun saveAudioMuse() {
		preferenceManager.audioMuseUrl = audioMuseUrl.trim()
		preferenceManager.audioMuseToken = audioMuseToken.trim()
	}

	Scaffold(
		topBar = { NestedTopBar({ Text("navi-connect") }) },
		snackbarHost = { SnackbarHost(snackbarHostState) }
	) { innerPadding ->
		Column(
			modifier = Modifier
				.padding(innerPadding)
				.verticalScroll(rememberScrollState())
				.padding(horizontal = 16.dp),
			verticalArrangement = Arrangement.spacedBy(12.dp)
		) {
			Row(
				modifier = Modifier.fillMaxWidth(),
				horizontalArrangement = Arrangement.SpaceBetween,
				verticalAlignment = Alignment.CenterVertically
			) {
				Column(Modifier.weight(1f)) {
					Text("Enable navi-connect", style = MaterialTheme.typography.titleMedium)
					Text(
						when {
							connected -> "Connected to hub"
							connectionError != null -> connectionError!!
							else -> "Not connected"
						},
						style = MaterialTheme.typography.bodySmall,
						color = if (!connected && connectionError != null) MaterialTheme.colorScheme.error
							else MaterialTheme.colorScheme.onSurfaceVariant
					)
				}
				Switch(
					checked = enabled,
					onCheckedChange = {
						enabled = it
						applyAndReconnect()
					}
				)
			}

			OutlinedTextField(
				value = url,
				onValueChange = { url = it },
				label = { Text("Hub URL") },
				placeholder = { Text("ws://192.168.1.10:4790") },
				singleLine = true,
				modifier = Modifier.fillMaxWidth()
			)
			OutlinedTextField(
				value = token,
				onValueChange = { token = it },
				label = { Text("Token") },
				singleLine = true,
				modifier = Modifier.fillMaxWidth()
			)
			OutlinedTextField(
				value = deviceName,
				onValueChange = { deviceName = it },
				label = { Text("Device name") },
				singleLine = true,
				modifier = Modifier.fillMaxWidth()
			)
			Button(onClick = { applyAndReconnect() }) {
				Text("Save & reconnect")
			}

			Text("AudioMuse-AI (Tier 2)", style = MaterialTheme.typography.titleMedium)
			Text(
				"Unlocks Sonic Fingerprint autoplay, Mood Flow and mood search. Normally " +
					"routed through the hub above, which holds the AudioMuse address and " +
					"token server-side — leave these blank. Fill them in only to reach " +
					"the core API directly, for a LAN setup with no hub.",
				style = MaterialTheme.typography.bodySmall,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
			OutlinedTextField(
				value = audioMuseUrl,
				onValueChange = { audioMuseUrl = it },
				label = { Text("AudioMuse URL (direct, optional)") },
				placeholder = { Text("http://192.168.1.10:8000") },
				singleLine = true,
				modifier = Modifier.fillMaxWidth()
			)
			OutlinedTextField(
				value = audioMuseToken,
				onValueChange = { audioMuseToken = it },
				label = { Text("AudioMuse API token (direct, optional)") },
				singleLine = true,
				modifier = Modifier.fillMaxWidth()
			)
			Button(onClick = { saveAudioMuse() }) {
				Text("Save AudioMuse")
			}

			Text("Devices", style = MaterialTheme.typography.titleMedium)
			if (devices.isEmpty()) {
				Text(
					"No devices found",
					style = MaterialTheme.typography.bodyMedium,
					color = MaterialTheme.colorScheme.onSurfaceVariant
				)
			}
			devices.forEach { device ->
				Row(
					modifier = Modifier.fillMaxWidth(),
					horizontalArrangement = Arrangement.SpaceBetween,
					verticalAlignment = Alignment.CenterVertically
				) {
					Column(Modifier.weight(1f)) {
						Text(device.name, style = MaterialTheme.typography.bodyLarge)
						Text(
							buildString {
								append(if (device.online) "online" else "offline")
								if (device.isActive) append(" · playing")
								if (device.id == myDeviceId) append(" · this device")
							},
							style = MaterialTheme.typography.bodySmall,
							color = MaterialTheme.colorScheme.onSurfaceVariant
						)
					}
					Button(
						enabled = device.online && device.id != activeDeviceId,
						onClick = { hubManager.transfer(device.id) }
					) {
						Text(if (device.id == myDeviceId) "Play here" else "Transfer")
					}
				}
			}
		}
	}
}
