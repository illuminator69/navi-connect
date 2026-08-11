package paige.navic.androidApp.widgets.quickpicks

import android.content.Context
import android.graphics.Bitmap
import coil3.imageLoader
import coil3.request.ImageRequest
import coil3.request.SuccessResult
import coil3.request.allowHardware
import coil3.toBitmap
import org.koin.mp.KoinPlatform
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.models.DomainAlbum
import paige.navic.domain.models.DomainAlbumListType
import paige.navic.domain.repositories.AlbumRepository
import paige.navic.util.core.Logger

private const val LOG_TAG = "QuickPicksWidget"

/** One shortcut tile: a cover, a one-line title, and the album it opens. */
internal data class QuickPickTile(
	val albumId: String,
	val title: String,
	val cover: Bitmap?
)

internal const val QUICK_PICK_TILE_COUNT = 5

/**
 * The tiles last built, for the cover they were built against.
 *
 * Same reason as `CoverArtCache`: a widget update composes from scratch, so an unpause — or any
 * other redraw that leaves the track alone — restarted the load and emptied the row for as long
 * as it took Room and Coil to answer. Holding one list means a redraw of the same track is
 * instant, while a genuine track change still re-rolls the random picks.
 */
internal object QuickPickTilesCache {

	private var key: String? = null
	private var tiles: List<QuickPickTile> = emptyList()

	fun get(forKey: String): List<QuickPickTile> = if (forKey == key) tiles else emptyList()

	fun put(forKey: String, value: List<QuickPickTile>) {
		key = forKey
		tiles = value
	}
}

/**
 * The five shortcut tiles: recently-added albums in slots 1/3/5, random ones in 2/4.
 *
 * Navidrome has no personalised recommendations, so there is deliberately nothing else here —
 * "new" and "surprise me" are the two things it can honestly answer. Both come from
 * [AlbumRepository.getAlbumsLimited], the same limited read the Library home rows use, so the
 * widget never opens a second query path into the library.
 */
internal suspend fun loadQuickPickTiles(context: Context): List<QuickPickTile> {
	val koin = KoinPlatform.getKoin()
	val albums = koin.getOrNull<AlbumRepository>() ?: return emptyList()
	val session = koin.getOrNull<SessionManager>() ?: return emptyList()

	// Deeper than the three slots need: the extras are what fills the row when the random read
	// comes back short (see below).
	val newest = read(albums, DomainAlbumListType.Newest, 12)
	val recent = newest.take(3)

	// Over-fetch so the random slots can skip anything already shown as recently added — with a
	// small library the two rows overlap often, and a widget showing the same album twice looks
	// broken rather than random.
	val random = read(albums, DomainAlbumListType.Random, 12)
		.filter { candidate -> recent.none { it.id == candidate.id } }
		.take(2)

	// A short random read must not leave holes in the row — the first build shipped three tiles
	// on a five-tile widget because the random query came back empty and nothing took its place.
	// Topping up from recently-added keeps the row full without inventing a third category.
	val tiles = (interleave(recent, random) + newest.drop(3))
		.distinctBy { it.id }
		.take(QUICK_PICK_TILE_COUNT)

	return tiles.map { album ->
		QuickPickTile(
			albumId = album.id,
			title = album.name,
			cover = fetchCover(context, session.getCoverArtUrl(album.coverArtId))
		)
	}
}

/**
 * One album list, or an empty one if the read fails.
 *
 * Logged rather than swallowed: a widget cannot report an error, and an empty list here is
 * indistinguishable from a library that genuinely has nothing to show.
 */
private suspend fun read(
	albums: AlbumRepository,
	listType: DomainAlbumListType,
	limit: Int
): List<DomainAlbum> = try {
	albums.getAlbumsLimited(listType, limit)
} catch (error: Exception) {
	Logger.e(LOG_TAG, "quick picks: ${listType.value} read failed", error)
	emptyList()
}

/** `[n0, r0, n1, r1, n2]`, collapsing gracefully when a library is too small to fill both. */
private fun interleave(
	newest: List<DomainAlbum>,
	random: List<DomainAlbum>
): List<DomainAlbum> {
	val ordered = mutableListOf<DomainAlbum>()
	var n = 0
	var r = 0
	while (ordered.size < QUICK_PICK_TILE_COUNT && (n < newest.size || r < random.size)) {
		if (n < newest.size) ordered.add(newest[n++])
		if (ordered.size < QUICK_PICK_TILE_COUNT && r < random.size) ordered.add(random[r++])
	}
	return ordered
}

private suspend fun fetchCover(context: Context, url: String): Bitmap? {
	val request = ImageRequest.Builder(context)
		.data(url)
		// Tiles are ~48dp squares; the now-playing cover's 700px would be wasted RemoteViews
		// payload five times over.
		.size(160)
		.allowHardware(false)
		.build()
	return (context.imageLoader.execute(request) as? SuccessResult)?.image?.toBitmap()
}
