package paige.navic.domain.models.settings

/**
 * Tuning preset for adaptive "Mood Flow" (AutoplayMode.Adaptive). Maps to
 * AudioMuse Song Alchemy parameters:
 *  - [temperature]: softmax sampling temperature — higher = more exploration/drift,
 *    lower = tighter/more deterministic (server default 1.0).
 *  - [subtractDistance]: exclusion radius around skipped tracks — higher pushes
 *    disliked vibes further away (server default 0.2 angular). null = server default.
 *
 * Symfonium-style names. Only affects Tier-2 Adaptive mode (the Tier-1 similar/
 * radio endpoints take no tuning).
 */
enum class MoodCharacter(
	val label: String,
	val temperature: Float,
	val subtractDistance: Float?
) {
	EchoMatch("Echo Match", 0.5f, null),                  // sticks close to the vibe
	SteadyVibes("Steady Vibes", 0.6f, 0.35f),             // stays in lane, resists skips
	TransitionMaestro("Transition Maestro", 1.6f, null)   // explores, drifts readily
}
