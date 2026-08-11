package paige.navic.util.core

/**
 * Unicode NFD (canonical decomposition), so an accent can be stripped as a
 * combining mark. `java.text.Normalizer` is JVM-only and Kotlin has no common
 * equivalent, so each platform brings its own.
 */
expect fun toNfd(value: String): String

private val COMBINING_MARKS = '̀'..'ͯ'
private val BRACKET_SUFFIX = Regex("""[\[({].*?[])}]""")
private val NON_ALPHANUMERIC = Regex("[^a-z0-9]+")

/**
 * Album titles compared the way a person would: case, accents, punctuation and
 * parenthesised edition suffixes all discarded. "Kid A" and "Kid A (Remastered)"
 * are the same record for this purpose.
 *
 * Deliberately stops there. A dash suffix would be the obvious next rule, but
 * "Hail to the Thief - Live" is a different record from "Hail to the Thief", and
 * the cost of the two mistakes is not symmetric: leaving a duplicate on screen is
 * untidy, while collapsing two real releases hides an album the user then cannot
 * download at all.
 *
 * The NFD step is not decoration. lb-bot's titles come from MusicBrainz and
 * Navidrome's come from file tags, and the same visible string can be spelled
 * either composed or decomposed. Without decomposing first, the two sides key
 * differently and the album shows up twice — the exact duplicate this exists to
 * prevent.
 */
fun albumTitleKey(title: String): String =
	toNfd(title)
		.filterNot { it in COMBINING_MARKS }
		.lowercase()
		.replace(BRACKET_SUFFIX, "")
		.replace(NON_ALPHANUMERIC, "")
		.trim()
