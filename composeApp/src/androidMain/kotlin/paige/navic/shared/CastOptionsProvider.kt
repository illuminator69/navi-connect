package paige.navic.shared

import android.content.Context
import com.google.android.gms.cast.CastMediaControlIntent
import com.google.android.gms.cast.framework.CastOptions
import com.google.android.gms.cast.framework.OptionsProvider
import com.google.android.gms.cast.framework.SessionProvider

/**
 * Cast framework bootstrap, referenced from the AndroidManifest meta-data.
 * Uses the Default Media Receiver, which plays plain media URLs — Navidrome
 * stream links (which are publicly reachable) work directly on the Chromecast.
 */
class CastOptionsProvider : OptionsProvider {
	override fun getCastOptions(context: Context): CastOptions =
		CastOptions.Builder()
			.setReceiverApplicationId(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID)
			.build()

	override fun getAdditionalSessionProviders(context: Context): MutableList<SessionProvider>? =
		null
}
