package paige.navic.androidApp

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.LbFillEvent

/**
 * Tells the user when an lb-bot fill lands, while they are somewhere else.
 *
 * Android-only and deliberately outside `composeApp`: it needs a runtime permission and
 * the platform notification API, and commonMain still has to compile for iOS. The in-app
 * half — a snackbar, which is all that is needed while Navic is in front — lives in
 * `App.kt` and works whether or not this permission was ever granted.
 *
 * A fill takes minutes: search, transfer, tagging, placement, then Navidrome's own scan.
 * Nobody watches a phone for that long, so without this the only way to learn that an
 * album arrived was to go back and look.
 *
 * Built on the platform APIs rather than `NotificationCompat` so this module needs no
 * dependency it doesn't already have; the only version gate is the channel, which is
 * API 26 and up while Navic's minSdk is 24.
 */
class FillNotifier(private val context: Context) {

	private val manager: NotificationManager? =
		context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

	private fun ensureChannel() {
		if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
		val existing = manager?.getNotificationChannel(CHANNEL_ID)
		if (existing != null) return
		// DEFAULT, not HIGH: an album finishing is worth a notification, not a heads-up
		// banner over whatever the user is doing. Nothing here is time-critical — the
		// album is in the library either way.
		manager?.createNotificationChannel(
			NotificationChannel(CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_DEFAULT)
				.apply { description = CHANNEL_DESC }
		)
	}

	/**
	 * Post one fill's outcome. Silently does nothing when the permission was never
	 * granted — `notify` throws nothing on Android 13+, it is simply dropped, and the
	 * snackbar has already covered the in-app case.
	 */
	fun notify(event: LbFillEvent) {
		val name = event.album.ifBlank { event.artist }.ifBlank { return }
		// The same two outcomes the snackbar announces. `needs_pick` is the picker
		// waiting on the user, `cancelled` is something they just did, and `gave_up`
		// means we stopped tracking rather than that anything happened — none of the
		// three is news arriving from elsewhere, which is what a notification is for.
		val text = when (event.outcome) {
			LbBotManager.OUTCOME_DONE -> context.getString(R.string.notif_fill_landed, name)
			LbBotManager.OUTCOME_FAILED -> context.getString(R.string.notif_fill_lost, name)
			else -> return
		}
		ensureChannel()

		val open = PendingIntent.getActivity(
			context,
			0,
			Intent(context, MainActivity::class.java)
				.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
			PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
		)

		val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
			Notification.Builder(context, CHANNEL_ID)
		} else {
			@Suppress("DEPRECATION")
			Notification.Builder(context)
		}
		val notification = builder
			.setSmallIcon(android.R.drawable.stat_sys_download_done)
			.setContentTitle(context.getString(R.string.app_name))
			.setContentText(text)
			.setContentIntent(open)
			.setAutoCancel(true)
			.build()

		// Keyed by the fill, so a second outcome for the same album replaces the first
		// rather than stacking — a retry that succeeds should not leave its failure up.
		runCatching { manager?.notify(TAG, event.key.hashCode(), notification) }
	}

	private companion object {
		const val CHANNEL_ID = "lbbot_fills"
		const val CHANNEL_NAME = "Downloads from Soulseek"
		const val CHANNEL_DESC = "When an album lb-bot was fetching arrives, or fails"
		const val TAG = "lbbot_fill"
	}
}
