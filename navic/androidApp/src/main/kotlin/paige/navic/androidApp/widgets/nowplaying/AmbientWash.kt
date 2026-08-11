package paige.navic.androidApp.widgets.nowplaying

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import android.graphics.Rect
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The blurred, saturation-boosted cover wash `BlendBackground` draws on the Library hero and
 * Now Playing, baked into a [Bitmap] for the home screen.
 *
 * A widget is a `RemoteViews` tree, so there is no live `Modifier.blur()` to apply and no
 * `ColorFilter` on a Glance `Image` background — the whole effect has to exist in the pixels
 * before the bitmap crosses into the launcher's process. RenderScript is gone and
 * `RenderEffect` needs a `HardwareRenderer` + `ImageReader` surface to run off-screen, which is
 * far more machinery than a 32×32 blur warrants, so the blur here is a downscale + box blur:
 * the cover is crushed to [SAMPLE] px (where a box blur over ~1k pixels is free), blurred, then
 * re-expanded with bilinear filtering. Visually that is the same wide, structureless wash as
 * `Modifier.blur(80.dp)`, and it works on every API level the app supports.
 */
internal object AmbientWash {

	/** Side length the cover is crushed to before blurring. */
	private const val SAMPLE = 32

	/** Side length of the produced wash. Small — it is stretched over the whole widget anyway. */
	private const val OUTPUT = 320

	private const val BLUR_RADIUS = 3
	private const val BLUR_PASSES = 3

	/** Matches `BlendBackground`'s `setToSaturation(1.5f)`. */
	private const val SATURATION = 1.5f

	/** Matches `BlendBackground`'s `Color.Black.copy(alpha = 0.4f)` scrim. */
	private const val SCRIM_ALPHA = 102

	/** The wash for [source]. Results are held by [CoverArtCache], alongside the cover itself. */
	suspend fun of(source: Bitmap): Bitmap? = withContext(Dispatchers.Default) {
		runCatching { render(source) }.getOrNull()
	}

	private fun render(source: Bitmap): Bitmap {
		val small = Bitmap.createScaledBitmap(source, SAMPLE, SAMPLE, true)

		val pixels = IntArray(SAMPLE * SAMPLE)
		small.getPixels(pixels, 0, SAMPLE, 0, 0, SAMPLE, SAMPLE)
		repeat(BLUR_PASSES) { boxBlur(pixels, SAMPLE, SAMPLE, BLUR_RADIUS) }
		small.setPixels(pixels, 0, SAMPLE, 0, 0, SAMPLE, SAMPLE)

		val out = Bitmap.createBitmap(OUTPUT, OUTPUT, Bitmap.Config.ARGB_8888)
		val canvas = Canvas(out)
		val paint = Paint(Paint.FILTER_BITMAP_FLAG).apply {
			colorFilter = ColorMatrixColorFilter(
				ColorMatrix().apply { setSaturation(SATURATION) }
			)
		}
		canvas.drawBitmap(small, null, Rect(0, 0, OUTPUT, OUTPUT), paint)
		canvas.drawColor(Color.argb(SCRIM_ALPHA, 0, 0, 0))

		small.recycle()
		return out
	}

	/** One horizontal + one vertical box pass, in place. Alpha is left alone (covers are opaque). */
	private fun boxBlur(pixels: IntArray, width: Int, height: Int, radius: Int) {
		val scratch = IntArray(pixels.size)
		blurRows(pixels, scratch, width, height, radius)
		// Transposing would cost another buffer; blurring columns directly is the same work.
		blurColumns(scratch, pixels, width, height, radius)
	}

	private fun blurRows(src: IntArray, dst: IntArray, width: Int, height: Int, radius: Int) {
		for (y in 0 until height) {
			val row = y * width
			for (x in 0 until width) {
				var r = 0
				var g = 0
				var b = 0
				var n = 0
				for (dx in -radius..radius) {
					val sx = (x + dx).coerceIn(0, width - 1)
					val c = src[row + sx]
					r += (c shr 16) and 0xFF
					g += (c shr 8) and 0xFF
					b += c and 0xFF
					n++
				}
				dst[row + x] = 0xFF shl 24 or ((r / n) shl 16) or ((g / n) shl 8) or (b / n)
			}
		}
	}

	private fun blurColumns(src: IntArray, dst: IntArray, width: Int, height: Int, radius: Int) {
		for (x in 0 until width) {
			for (y in 0 until height) {
				var r = 0
				var g = 0
				var b = 0
				var n = 0
				for (dy in -radius..radius) {
					val sy = (y + dy).coerceIn(0, height - 1)
					val c = src[sy * width + x]
					r += (c shr 16) and 0xFF
					g += (c shr 8) and 0xFF
					b += c and 0xFF
					n++
				}
				dst[y * width + x] = 0xFF shl 24 or ((r / n) shl 16) or ((g / n) shl 8) or (b / n)
			}
		}
	}
}
