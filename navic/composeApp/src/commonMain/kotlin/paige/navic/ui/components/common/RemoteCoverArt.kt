package paige.navic.ui.components.common

import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.layout.ContentScale
import coil3.compose.SubcomposeAsyncImage
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import coil3.request.crossfade
import org.koin.compose.koinInject
import paige.navic.domain.manager.PreferenceManager
import coil3.compose.LocalPlatformContext as LocalCoilPlatformContext

/**
 * Cover art from a third-party host, for a release the library does not have.
 *
 * Separate from [CoverArt] on purpose, and it must stay separate. [CoverArt]
 * attaches `preferenceManager.customHeadersMap()` to every request, because every
 * image it loads comes from the user's own Navidrome. Those headers are where a
 * reverse-proxy auth header or a Cloudflare Access token lives, so pointing that
 * composable at coverartarchive.org would hand the user's credentials to someone
 * else's server. This one sends no headers at all.
 *
 * A miss is the normal case — plenty of obscure release-groups have no art in the
 * Archive — so both the empty and the failed states render the same plain
 * placeholder box the null branch of [CoverArt] does. A broken-image icon here
 * would be noise, not information.
 */
@Composable
fun RemoteCoverArt(
	modifier: Modifier = Modifier,
	url: String?,
	contentDescription: String? = null,
	onClick: (() -> Unit)? = null,
	onLongClick: (() -> Unit)? = null,
	square: Boolean = true,
	crossfadeMs: Int = 500,
	shape: Shape? = null
) {
	val preferenceManager = koinInject<PreferenceManager>()
	val resolvedShape = shape ?: preferenceManager.coverArtShape.shape
	val coilPlatformContext = LocalCoilPlatformContext.current

	val model = remember(url) {
		ImageRequest.Builder(coilPlatformContext)
			.data(url)
			.memoryCacheKey(url)
			.diskCacheKey(url)
			.diskCachePolicy(CachePolicy.ENABLED)
			.memoryCachePolicy(CachePolicy.ENABLED)
			.crossfade(crossfadeMs)
			.build()
	}

	val commonModifier = modifier
		.then(if (square) Modifier.aspectRatio(1f) else Modifier)
		.clip(resolvedShape)
		.background(MaterialTheme.colorScheme.surfaceContainer)
		.then(
			if (onClick != null)
				Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick)
			else Modifier
		)

	if (url.isNullOrBlank()) return Box(commonModifier)
	SubcomposeAsyncImage(
		model = model,
		contentDescription = contentDescription,
		modifier = commonModifier,
		contentScale = ContentScale.Crop,
		error = { Box(Modifier) }
	)
}
