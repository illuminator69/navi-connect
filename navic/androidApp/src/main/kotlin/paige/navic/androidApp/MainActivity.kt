package paige.navic.androidApp

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.launch
import org.koin.android.ext.android.inject
import paige.navic.App
import paige.navic.domain.manager.LbBotManager
import paige.navic.ui.navigation.AppDeepLink

class MainActivity : ComponentActivity() {

	private val lbBot: LbBotManager by inject()

	private val requestNotifications =
		registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

	override fun onCreate(savedInstanceState: Bundle?) {
		super.onCreate(savedInstanceState)
		enableEdgeToEdge()
		handleDeepLink(intent)
		observeFills()
		setContent { App() }
	}

	/**
	 * Post a notification when a fill settles, and ask for the permission to do so the
	 * first time one is started.
	 *
	 * Lazy on purpose: a permission prompt at launch, for a feature most sessions never
	 * touch, is the kind that gets denied reflexively — and lb-bot is off entirely unless
	 * a hub is configured. Asking at the moment the user starts a download makes the
	 * prompt legible, and a denial costs nothing: the snackbar in `App.kt` still fires,
	 * and the Download Center still lists every fill.
	 *
	 * `repeatOnLifecycle(STARTED)` rather than a process-wide collector because a
	 * notification is only useful for a fill that settles while the app is alive; the
	 * poll loop that produces these events is itself scoped to the process.
	 */
	private fun observeFills() {
		val notifier = FillNotifier(applicationContext)
		lifecycleScope.launch {
			repeatOnLifecycle(Lifecycle.State.STARTED) {
				launch {
					lbBot.fillEvents.collect { notifier.notify(it) }
				}
				launch {
					lbBot.ledger.collect { entries ->
						if (entries.any { it.isRunning }) askForNotifications()
					}
				}
			}
		}
	}

	private var askedForNotifications = false

	private fun askForNotifications() {
		if (askedForNotifications) return
		if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
		val granted = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
			PackageManager.PERMISSION_GRANTED
		// One ask per process. Android itself only shows the dialog twice ever, and after
		// that the call is a silent no — re-firing it on every fill would be pointless.
		askedForNotifications = true
		if (!granted) requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
	}

	/**
	 * Reached when a Quick Picks tile is tapped while the activity is already running — the tile
	 * intents carry `FLAG_ACTIVITY_SINGLE_TOP`, so the task is reused rather than recreated.
	 */
	override fun onNewIntent(intent: Intent) {
		super.onNewIntent(intent)
		setIntent(intent)
		handleDeepLink(intent)
	}

	private fun handleDeepLink(intent: Intent?) {
		val uri: Uri = intent?.data ?: return
		if (uri.scheme != DEEP_LINK_SCHEME) return
		when (uri.host) {
			DEEP_LINK_ALBUM -> uri.lastPathSegment
				?.takeIf { it.isNotBlank() }
				?.let(AppDeepLink::requestAlbum)
		}
	}

	private companion object {
		const val DEEP_LINK_SCHEME = "navic"
		const val DEEP_LINK_ALBUM = "album"
	}
}
