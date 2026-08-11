package paige.navic.androidApp

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import paige.navic.App
import paige.navic.ui.navigation.AppDeepLink

class MainActivity : ComponentActivity() {
	override fun onCreate(savedInstanceState: Bundle?) {
		super.onCreate(savedInstanceState)
		enableEdgeToEdge()
		handleDeepLink(intent)
		setContent { App() }
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
