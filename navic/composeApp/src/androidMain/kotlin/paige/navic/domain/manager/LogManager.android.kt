package paige.navic.domain.manager

import androidx.compose.runtime.mutableStateListOf
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import paige.navic.domain.parser.LogLine
import paige.navic.domain.parser.LogLineParser
import java.io.InputStreamReader

private const val LOG_SIZE = 500

/** How long lines accumulate before being handed to Compose, in ms. */
private const val FLUSH_INTERVAL_MS = 100L

/**
 * Buffer between the reader and the UI. Bounded and dropping: a burst of logging must cost us the
 * oldest lines, never unbounded memory, and never backpressure onto the logcat reader.
 */
private const val CHANNEL_CAPACITY = 2048

actual class LogManager {
	private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
	private var readerJob: Job? = null
	private var flushJob: Job? = null

	/**
	 * Kept for [stopStreaming]: `readLine()` is a blocking call that coroutine cancellation cannot
	 * interrupt, so the only way out of it is to destroy the process underneath it.
	 */
	@Volatile
	private var process: Process? = null

	private val _logs = mutableStateListOf<LogLine>()
	actual val logs: List<LogLine> = _logs

	actual fun startStreaming() {
		stopStreaming()
		_logs.clear()

		val incoming = Channel<LogLine>(
			capacity = CHANNEL_CAPACITY,
			onBufferOverflow = BufferOverflow.DROP_OLDEST
		)

		readerJob = scope.launch {
			val proc = Runtime.getRuntime().exec(arrayOf("logcat", "--format=tag"))
			process = proc
			try {
				InputStreamReader(proc.inputStream).buffered().use { reader ->
					while (isActive) {
						val nextLine = reader.readLine() ?: break
						incoming.trySend(LogLineParser.parseString(nextLine))
					}
				}
			} finally {
				// Without this a stopped-and-restarted log screen leaves an orphaned logcat
				// process reading forever.
				runCatching { proc.destroy() }
				process = null
				incoming.close()
			}
		}

		// The list is Compose state, so it may only be touched from the main thread — mutating it
		// off-thread races the draw phase and crashes inside ThreadedRenderer, which is what a
		// chatty tag used to do within seconds of opening this screen. Batching on a timer also
		// keeps a 30-lines-per-second tag to one recomposition per interval instead of thirty.
		flushJob = scope.launch(Dispatchers.Main) {
			val batch = ArrayList<LogLine>(64)
			while (isActive) {
				delay(FLUSH_INTERVAL_MS)
				batch.clear()
				while (true) {
					val line = incoming.tryReceive().getOrNull() ?: break
					batch.add(line)
				}
				if (batch.isEmpty()) continue
				_logs.addAll(batch)
				val excess = _logs.size - LOG_SIZE
				// One bulk trim: removeAt(0) per line is quadratic and fires a snapshot write each
				// time, which is its own source of frame drops.
				if (excess > 0) _logs.subList(0, excess).clear()
			}
		}
	}

	actual fun stopStreaming() {
		readerJob?.cancel()
		readerJob = null
		flushJob?.cancel()
		flushJob = null
		runCatching { process?.destroy() }
		process = null
	}

	actual fun clearLogs() {
		scope.launch { runCatching { Runtime.getRuntime().exec(arrayOf("logcat", "-c")) } }
		_logs.clear()
	}
}
