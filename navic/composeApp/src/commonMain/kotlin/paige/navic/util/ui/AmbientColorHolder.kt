package paige.navic.util.ui

import androidx.compose.ui.graphics.Color

/**
 * App-scoped memory of the last resolved cover/photo ambient seed colour.
 *
 * A freshly-opened detail screen (album / artist) reads this as its INITIAL seed
 * so its ambient starts at the colour we're navigating FROM and eases to its own
 * (via the screen's `animateColorAsState`), instead of flashing the neutral
 * adaptive default while kmpalette extracts the new artwork's colour.
 *
 * Written by each detail screen whenever its resolved seed changes; read once on
 * the next screen's first composition. Main-thread only, so a plain var is fine.
 */
class AmbientColorHolder {
	var last: Color? = null
}
