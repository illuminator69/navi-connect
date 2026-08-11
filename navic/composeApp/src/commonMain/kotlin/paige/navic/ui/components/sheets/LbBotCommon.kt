package paige.navic.ui.components.sheets

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.error_lbbot_busy
import navic.composeapp.generated.resources.error_lbbot_hub_outdated
import navic.composeapp.generated.resources.error_lbbot_not_configured
import navic.composeapp.generated.resources.error_lbbot_rejected
import navic.composeapp.generated.resources.error_lbbot_unreachable
import org.jetbrains.compose.resources.stringResource
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.LbSourceFile

/**
 * What went wrong, in words the user can act on.
 *
 * lb-bot writes its refusals as sentences for humans, so those are shown verbatim
 * rather than paraphrased — it knows why and we don't. The one case worth
 * translating is a 404, which does not mean "not found" here: it means the hub
 * proxies no such route, i.e. it is older than this app and needs restarting.
 * That failure used to be completely silent.
 */
@Composable
fun lbErrorText(error: LbBotManager.LbError): String = when (error) {
	is LbBotManager.LbError.NotConfigured -> stringResource(Res.string.error_lbbot_not_configured)
	is LbBotManager.LbError.RouteUnknown -> stringResource(Res.string.error_lbbot_hub_outdated)
	is LbBotManager.LbError.Busy -> stringResource(Res.string.error_lbbot_busy)
	is LbBotManager.LbError.Unreachable -> stringResource(Res.string.error_lbbot_unreachable)
	is LbBotManager.LbError.Rejected ->
		error.message.ifBlank { stringResource(Res.string.error_lbbot_rejected, error.status) }
}

@Composable
fun LbErrorLine(error: LbBotManager.LbError?, modifier: Modifier = Modifier) {
	if (error == null) return
	Text(
		lbErrorText(error),
		style = MaterialTheme.typography.bodySmall,
		color = MaterialTheme.colorScheme.error,
		modifier = modifier.fillMaxWidth().padding(vertical = 6.dp)
	)
}

@Composable
internal fun SheetLabel(text: String) {
	Text(
		text,
		style = MaterialTheme.typography.labelLarge,
		fontWeight = FontWeight.Medium,
		color = MaterialTheme.colorScheme.primary,
		modifier = Modifier.padding(top = 8.dp, bottom = 4.dp)
	)
}

/** Horizontal chip picker. Versions and editions are a short, side-by-side choice —
 *  a vertical scroller made two or three options look like a list to wade through. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ChipRow(labels: List<String>, selectedIndex: Int, onSelect: (Int) -> Unit) {
	LazyRow(
		modifier = Modifier.fillMaxWidth(),
		horizontalArrangement = Arrangement.spacedBy(8.dp)
	) {
		itemsIndexed(labels) { index, label ->
			FilterChip(
				selected = index == selectedIndex,
				onClick = { onSelect(index) },
				label = { Text(label, maxLines = 1, overflow = TextOverflow.Ellipsis) }
			)
		}
	}
}

/** Two lines of small print under a heading — used for the source-picker caveat. */
@Composable
internal fun SheetCaption(text: String) {
	Text(
		text,
		style = MaterialTheme.typography.bodySmall,
		color = MaterialTheme.colorScheme.onSurfaceVariant,
		modifier = Modifier.fillMaxWidth().padding(bottom = 4.dp)
	)
}

/** A source's evidence line, shared by the album picker and the gap picker. */
@Composable
internal fun SourceHeadline(peer: String, trailing: @Composable () -> Unit) {
	Row(Modifier.fillMaxWidth()) {
		Text(
			peer,
			style = MaterialTheme.typography.bodyMedium,
			fontWeight = FontWeight.Medium,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.weight(1f)
		)
		trailing()
	}
}

/**
 * What is actually in a peer's folder, and which slot each file would fill.
 *
 * This is the only thing that settles "seventeen tracks are missing but this
 * source has twelve" before committing — a coverage count says *how many* match,
 * this says *which*. A file with no `matchedTo` fills nothing: a folder made
 * entirely of those is the wrong album, however plausible its name.
 */
@Composable
internal fun SourceFileList(files: List<LbSourceFile>, truncated: Boolean) {
	if (files.isEmpty()) return
	Column(Modifier.fillMaxWidth().padding(top = 4.dp, start = 8.dp)) {
		files.forEach { file ->
			Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
				Text(
					file.matchedTo?.let { match ->
						listOfNotNull(
							match.position.takeIf { it.isNotBlank() },
							match.title.takeIf { it.isNotBlank() }
						).joinToString(". ")
					} ?: file.filename,
					style = MaterialTheme.typography.bodySmall,
					maxLines = 1,
					overflow = TextOverflow.Ellipsis,
					color = if (file.matchedTo != null) MaterialTheme.colorScheme.onSurface
					else MaterialTheme.colorScheme.onSurfaceVariant,
					modifier = Modifier.weight(1f)
				)
				Text(
					listOfNotNull(
						file.ext.takeIf { it.isNotBlank() },
						file.bitrate.takeIf { it > 0 }?.let { "$it kbps" }
					).joinToString(" "),
					style = MaterialTheme.typography.labelSmall,
					color = if (file.accepted) MaterialTheme.colorScheme.onSurfaceVariant
					else MaterialTheme.colorScheme.error
				)
			}
		}
		if (truncated) {
			Text(
				"…",
				style = MaterialTheme.typography.labelSmall,
				color = MaterialTheme.colorScheme.onSurfaceVariant
			)
		}
	}
}

@Composable
internal fun SourceMetaLine(parts: List<String?>) {
	val text = parts.filterNot { it.isNullOrBlank() }.joinToString(" • ")
	if (text.isBlank()) return
	Column {
		Text(
			text,
			style = MaterialTheme.typography.bodySmall,
			color = MaterialTheme.colorScheme.onSurfaceVariant
		)
	}
}
