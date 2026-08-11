package paige.navic.domain.manager.cast

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * JSON payloads carried inside [CastMessage.payloadUtf8], per namespace.
 *
 * Only the fields the bridge actually reads are modelled — receivers send a great deal more, and
 * [CastProtocol.json] ignores unknown keys so a firmware update can't break parsing.
 */

/** Every payload has a `type`; this is the cheap peek used to route before full deserialization. */
@Serializable
internal data class CastEnvelope(
	val type: String? = null,
	val requestId: Long? = null
)

// ------------------------------------------------------------------ receiver namespace

@Serializable
internal data class ReceiverStatusMessage(
	val requestId: Long? = null,
	val status: ReceiverStatus? = null
)

@Serializable
internal data class ReceiverStatus(
	val applications: List<ReceiverApplication> = emptyList(),
	val volume: ReceiverVolume? = null
)

@Serializable
internal data class ReceiverApplication(
	val appId: String? = null,
	val displayName: String? = null,
	/**
	 * The destination id for everything after launch. Media commands sent to `receiver-0`
	 * are silently ignored — this is the single most common way a hand-rolled castv2 client
	 * appears to connect fine and then do nothing.
	 */
	val transportId: String? = null,
	val sessionId: String? = null,
	val statusText: String? = null,
	val isIdleScreen: Boolean? = null,
	val namespaces: List<ReceiverNamespace> = emptyList()
)

@Serializable
internal data class ReceiverNamespace(val name: String? = null)

@Serializable
internal data class ReceiverVolume(
	val level: Double? = null,
	val muted: Boolean? = null
)

// --------------------------------------------------------------------- media namespace

@Serializable
internal data class MediaStatusMessage(
	val requestId: Long? = null,
	val status: List<MediaStatus> = emptyList()
)

@Serializable
internal data class MediaStatus(
	val mediaSessionId: Long? = null,
	val playerState: String? = null,
	val idleReason: String? = null,
	val currentTime: Double? = null,
	val media: MediaInformation? = null
) {
	val isPlaying: Boolean get() = playerState == "PLAYING"
	val isPaused: Boolean get() = playerState == "PAUSED"
	val finished: Boolean get() = playerState == "IDLE" && idleReason == "FINISHED"
	val errored: Boolean get() = playerState == "IDLE" && idleReason == "ERROR"

	/**
	 * Position in ms, or null when the device isn't reporting a usable one.
	 *
	 * A literal 0 is treated as "no reading". Cast devices transiently report 0 while
	 * (re)buffering, and in Feishin a 0 landing just before a pause is what intermittently
	 * reset the progress bar — and, on release, what reset transfers to the beginning of the
	 * track. Callers keep their last good value instead.
	 */
	val positionMs: Long? get() = currentTime?.takeIf { it > 0.0 }?.let { (it * 1000).toLong() }
}

@Serializable
internal data class MediaInformation(
	val contentId: String? = null,
	val contentType: String? = null,
	val streamType: String? = null,
	val duration: Double? = null,
	val metadata: MediaMetadata? = null
)

@Serializable
internal data class MediaMetadata(
	/** 3 = MusicTrackMediaMetadata, which is what gives a speaker its title/artist display. */
	val metadataType: Int = 3,
	val title: String? = null,
	val artist: String? = null,
	val albumName: String? = null,
	val images: List<MediaImage> = emptyList()
)

@Serializable
internal data class MediaImage(val url: String)

@Serializable
internal data class LoadRequest(
	val type: String = "LOAD",
	val requestId: Long,
	val sessionId: String? = null,
	val media: MediaInformation,
	val autoplay: Boolean,
	val currentTime: Double,
	@SerialName("customData") val customData: Map<String, String> = emptyMap()
)
