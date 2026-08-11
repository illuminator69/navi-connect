package paige.navic.domain.manager.cast

import kotlinx.serialization.json.Json
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream

/**
 * Google Cast v2 wire protocol — framing and namespaces.
 *
 * navi-connect's Chromecast support does NOT go through the Play Services Cast SDK. Feishin's
 * bridge talks castv2 directly (`castv2-client`), and Navic does the same for two reasons: the
 * MediaRouter/CastContext path finds no routes on the target device for reasons we could never
 * pin down, and speaking the protocol ourselves is the only way to get behavioural parity with
 * the bridge that already works — in particular joining a *running* receiver session, which the
 * SDK does not expose.
 *
 * The wire format is simple: a 4-byte big-endian length followed by a protobuf `CastMessage`.
 * That message has seven fields and we only ever send strings, so it is hand-encoded here rather
 * than pulling in protobuf-java (a runtime, its R8 rules, and a .proto build step for ~80 lines).
 */
internal object CastProtocol {
	const val NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
	const val NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
	const val NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
	const val NS_MEDIA = "urn:x-cast:com.google.cast.media"

	/** Every sender/receiver pair needs ids; these are the conventional ones. */
	const val SENDER_ID = "sender-0"
	const val RECEIVER_ID = "receiver-0"

	/** Default Media Receiver — plays plain media URLs, which is exactly what Navidrome serves. */
	const val DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"

	const val CAST_PORT = 8009

	/** Lenient: receivers add fields freely and a strict parse would fail on a firmware update. */
	val json = Json {
		ignoreUnknownKeys = true
		encodeDefaults = true
		explicitNulls = false
	}
}

/**
 * One castv2 frame. `payloadBinary` is deliberately absent: every namespace we speak is
 * `PAYLOAD_TYPE_STRING`, and accepting binary would only add a branch we can never exercise.
 */
internal data class CastMessage(
	val sourceId: String,
	val destinationId: String,
	val namespace: String,
	val payloadUtf8: String
)

// ---------------------------------------------------------------------------- protobuf

// CastMessage field numbers, from Google's cast_channel.proto. Field 1 (protocol_version) and
// field 5 (payload_type) are enums we always send as 0 — CASTV2_1_0 and STRING respectively —
// but they are `required` in the proto2 definition, so they must still be on the wire.
private const val FIELD_PROTOCOL_VERSION = 1
private const val FIELD_SOURCE_ID = 2
private const val FIELD_DESTINATION_ID = 3
private const val FIELD_NAMESPACE = 4
private const val FIELD_PAYLOAD_TYPE = 5
private const val FIELD_PAYLOAD_UTF8 = 6

private const val WIRE_VARINT = 0
private const val WIRE_LENGTH_DELIMITED = 2

/** A frame larger than this is not something a Chromecast sends; treat it as a desync. */
private const val MAX_FRAME_BYTES = 4 * 1024 * 1024

internal fun CastMessage.encode(): ByteArray {
	val out = java.io.ByteArrayOutputStream(payloadUtf8.length + 128)
	out.writeTag(FIELD_PROTOCOL_VERSION, WIRE_VARINT)
	out.writeVarint(0L)
	out.writeStringField(FIELD_SOURCE_ID, sourceId)
	out.writeStringField(FIELD_DESTINATION_ID, destinationId)
	out.writeStringField(FIELD_NAMESPACE, namespace)
	out.writeTag(FIELD_PAYLOAD_TYPE, WIRE_VARINT)
	out.writeVarint(0L)
	out.writeStringField(FIELD_PAYLOAD_UTF8, payloadUtf8)
	return out.toByteArray()
}

internal fun decodeCastMessage(bytes: ByteArray): CastMessage {
	var i = 0
	var source = ""
	var destination = ""
	var namespace = ""
	var payload = ""

	while (i < bytes.size) {
		val (tag, afterTag) = bytes.readVarint(i)
		i = afterTag
		val field = (tag ushr 3).toInt()
		when ((tag and 0x7L).toInt()) {
			WIRE_VARINT -> {
				val (_, next) = bytes.readVarint(i)
				i = next
			}

			WIRE_LENGTH_DELIMITED -> {
				val (len, afterLen) = bytes.readVarint(i)
				val start = afterLen
				val end = start + len.toInt()
				require(len >= 0 && end <= bytes.size) { "castv2: truncated field $field" }
				val value = String(bytes, start, end - start, Charsets.UTF_8)
				when (field) {
					FIELD_SOURCE_ID -> source = value
					FIELD_DESTINATION_ID -> destination = value
					FIELD_NAMESPACE -> namespace = value
					FIELD_PAYLOAD_UTF8 -> payload = value
				}
				i = end
			}

			// Nothing in CastMessage uses these, but skipping them correctly keeps a firmware
			// that adds a field from desynchronising the whole stream.
			1 -> i += 8
			5 -> i += 4
			else -> throw IllegalArgumentException("castv2: unknown wire type in field $field")
		}
	}
	return CastMessage(source, destination, namespace, payload)
}

private fun java.io.ByteArrayOutputStream.writeTag(field: Int, wireType: Int) {
	writeVarint((field.toLong() shl 3) or wireType.toLong())
}

private fun java.io.ByteArrayOutputStream.writeVarint(valueIn: Long) {
	var value = valueIn
	while (true) {
		val b = (value and 0x7F).toInt()
		value = value ushr 7
		if (value == 0L) {
			write(b)
			return
		}
		write(b or 0x80)
	}
}

private fun java.io.ByteArrayOutputStream.writeStringField(field: Int, value: String) {
	val encoded = value.toByteArray(Charsets.UTF_8)
	writeTag(field, WIRE_LENGTH_DELIMITED)
	writeVarint(encoded.size.toLong())
	write(encoded)
}

/** Returns the decoded varint and the index just past it. */
private fun ByteArray.readVarint(from: Int): Pair<Long, Int> {
	var result = 0L
	var shift = 0
	var i = from
	while (i < size) {
		val b = this[i].toInt() and 0xFF
		result = result or ((b and 0x7F).toLong() shl shift)
		i++
		if (b and 0x80 == 0) return result to i
		shift += 7
		require(shift < 64) { "castv2: varint overflow" }
	}
	throw IllegalArgumentException("castv2: truncated varint")
}

// ---------------------------------------------------------------------------- framing

internal fun OutputStream.writeFrame(message: CastMessage) {
	val body = message.encode()
	val header = ByteArray(4)
	header[0] = (body.size ushr 24).toByte()
	header[1] = (body.size ushr 16).toByte()
	header[2] = (body.size ushr 8).toByte()
	header[3] = body.size.toByte()
	// One write, not two: the header and body must not be split across a flush, or a receiver
	// reading eagerly can act on a length with no payload behind it yet.
	write(header + body)
	flush()
}

/** Blocking read of one frame. Throws [EOFException] when the peer closes. */
internal fun InputStream.readFrame(): CastMessage {
	val header = readExactly(4)
	val size = ((header[0].toInt() and 0xFF) shl 24) or
		((header[1].toInt() and 0xFF) shl 16) or
		((header[2].toInt() and 0xFF) shl 8) or
		(header[3].toInt() and 0xFF)
	require(size in 0..MAX_FRAME_BYTES) { "castv2: implausible frame size $size" }
	return decodeCastMessage(readExactly(size))
}

private fun InputStream.readExactly(n: Int): ByteArray {
	val buf = ByteArray(n)
	var read = 0
	while (read < n) {
		val r = read(buf, read, n - read)
		if (r < 0) throw EOFException("castv2: peer closed after $read/$n bytes")
		read += r
	}
	return buf
}
