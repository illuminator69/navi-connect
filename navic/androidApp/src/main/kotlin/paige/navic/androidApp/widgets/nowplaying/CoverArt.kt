package paige.navic.androidApp.widgets.nowplaying

import android.graphics.Bitmap
import android.net.Uri
import android.util.LruCache

/**
 * The now-playing cover and the ambient wash derived from it, produced together.
 *
 * They are one object rather than two pieces of widget state on purpose. Derived separately, the
 * wash was computed from whichever bitmap happened to be current when the *url* changed — one
 * recomposition before the new bitmap arrived — so the widget rendered the new cover over the
 * previous song's wash and stayed a track behind for as long as it was open.
 */
internal data class CoverArt(
	val bitmap: Bitmap,
	val wash: Bitmap?
)

/**
 * Last couple of covers, kept for the life of the process.
 *
 * A widget update is a fresh composition, so without this every state change that does not touch
 * the artwork — pausing, most of all — dropped back to the placeholder and re-fetched, which read
 * as the widget blinking. Seeding the state from here means an unchanged cover is already there
 * on the first frame.
 */
internal object CoverArtCache {

	private val cache = LruCache<String, CoverArt>(2)

	fun get(key: String): CoverArt? = cache.get(key)

	fun put(key: String, art: CoverArt) {
		cache.put(key, art)
	}

	/**
	 * Cover identity within the art url — the `id` query parameter, so a re-salted Subsonic auth
	 * query still resolves to the same artwork.
	 */
	fun keyFor(artUrl: String?): String {
		if (artUrl.isNullOrBlank()) return ""
		return runCatching { Uri.parse(artUrl).getQueryParameter("id") }.getOrNull() ?: artUrl
	}
}
