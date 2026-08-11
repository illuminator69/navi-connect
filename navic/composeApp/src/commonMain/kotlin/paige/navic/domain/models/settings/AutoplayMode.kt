package paige.navic.domain.models.settings

/**
 * navi-connect: continuous autoplay (queue-end top-up) strategy.
 *
 * - [Off]: never top up.
 * - [Similar]: top up from the current track's similar songs (Subsonic
 *   `getSimilarSongs2`; served as sonic similarity when the AudioMuse plugin is
 *   installed). Tier 1 — works with no extra configuration.
 * - [Fingerprint] / [Adaptive]: require the AudioMuse core API (Tier 2) and are
 *   not wired yet; defined here so the stored preference is forward-compatible
 *   and the selector can grow without a migration. See DESIGN-adaptive-audiomuse.md.
 *
 * Labels are hardcoded EN (consistent with the other navi-connect screens) to
 * avoid a string-resource regeneration step.
 */
enum class AutoplayMode(val label: String) {
	Off("Off"),
	Similar("Similar songs"),
	Fingerprint("Sonic Fingerprint"),
	Adaptive("Mood Flow")
}
