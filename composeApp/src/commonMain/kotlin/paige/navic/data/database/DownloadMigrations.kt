package paige.navic.data.database

import androidx.room3.migration.Migration
import androidx.sqlite.SQLiteConnection
import androidx.sqlite.execSQL

/**
 * v3 → v4: the download center's columns.
 *
 * This has to be a REAL migration. The databases are built with
 * `fallbackToDestructiveMigration(true)`, so simply bumping the version would drop the table —
 * and the audio files it points at would stay on disk as orphans, invisible to the app but still
 * eating the user's storage. Every added column is nullable or has a default, so existing rows
 * carry over as-is and keep working.
 *
 * `fileSize` backfills as 0 for pre-existing downloads rather than being measured here: this runs
 * on the DB thread during open, and stat-ing every file would block startup. [DownloadManager]
 * fills it in lazily instead.
 */
/**
 * Idempotent on purpose. A database can reach here ALREADY carrying these columns — a fresh
 * install of the build that shipped the expanded entity created the table complete, at the old
 * version number. Blindly re-adding a column there fails with "duplicate column name" and takes
 * the migration (and the app's launch) down with it, so each add tolerates already existing.
 */
private suspend fun SQLiteConnection.addDownloadCenterColumns() {
	listOf(
		"ALTER TABLE DownloadEntity ADD COLUMN maxBitRate INTEGER NOT NULL DEFAULT 0",
		"ALTER TABLE DownloadEntity ADD COLUMN format TEXT",
		"ALTER TABLE DownloadEntity ADD COLUMN fileSize INTEGER NOT NULL DEFAULT 0",
		"ALTER TABLE DownloadEntity ADD COLUMN error TEXT",
		"ALTER TABLE DownloadEntity ADD COLUMN retryCount INTEGER NOT NULL DEFAULT 0",
		"ALTER TABLE DownloadEntity ADD COLUMN sourcePolicy TEXT NOT NULL DEFAULT 'manual'",
		"ALTER TABLE DownloadEntity ADD COLUMN createdAt INTEGER NOT NULL DEFAULT 0",
		"ALTER TABLE DownloadEntity ADD COLUMN updatedAt INTEGER NOT NULL DEFAULT 0"
	).forEach { statement ->
		try {
			execSQL(statement)
		} catch (_: Throwable) {
			// Column already present — nothing to do.
		}
	}
}

val MIGRATION_DOWNLOAD_3_4 = object : Migration(3, 4) {
	override suspend fun migrate(connection: SQLiteConnection) {
		connection.addDownloadCenterColumns()
	}
}

/**
 * v15 → v16: the SAME columns, on the cache database's copy of the table.
 *
 * `DownloadEntity` is declared as an entity of BOTH [CacheDatabase] and [DownloadDatabase], so
 * expanding it changed the cache schema as well. Room noticed on launch — "changed schema but
 * forgot to update the version number" — because the version had stayed at 15.
 *
 * This migration exists so the fix doesn't cost the user their whole library: the cache DB also
 * falls back to a destructive rebuild, and bumping the version without it would drop every cached
 * album, song and playlist and force a full re-sync from the server.
 */
val MIGRATION_CACHE_15_16 = object : Migration(15, 16) {
	override suspend fun migrate(connection: SQLiteConnection) {
		connection.addDownloadCenterColumns()
	}
}

/**
 * v16 → v17: the `SavedQueueEntity` table backing automatic saved queues.
 *
 * A pure additive migration — a new, initially-empty table — so nothing existing is touched and the
 * user keeps their cached library instead of the destructive fallback wiping it. The column list
 * (identifiers, types, NOT NULL, primary key) must match Room's generated schema for
 * [paige.navic.data.database.entities.SavedQueueEntity] exactly, or Room's post-migration validation
 * fails on launch. `IF NOT EXISTS` tolerates a fresh install that already created the table at v17.
 */
val MIGRATION_CACHE_16_17 = object : Migration(16, 17) {
	override suspend fun migrate(connection: SQLiteConnection) {
		connection.execSQL(
			"CREATE TABLE IF NOT EXISTS `SavedQueueEntity` (" +
				"`id` TEXT NOT NULL, " +
				"`name` TEXT, " +
				"`sourceName` TEXT, " +
				"`queueJson` TEXT NOT NULL, " +
				"`currentIndex` INTEGER NOT NULL, " +
				"`currentSongId` TEXT, " +
				"`positionMs` INTEGER NOT NULL, " +
				"`shuffle` INTEGER NOT NULL, " +
				"`repeatMode` INTEGER NOT NULL, " +
				"`songCount` INTEGER NOT NULL, " +
				"`createdAt` INTEGER NOT NULL, " +
				"`updatedAt` INTEGER NOT NULL, " +
				"PRIMARY KEY(`id`))"
		)
	}
}

/**
 * v17 → v18: `SavedQueueEntity.sourceKind` (how each queue was created — album / playlist / radio /
 * Mood Flow / journey / manual, so the list can group generated sessions) and `coverArtId` (the
 * current track's cover, cached for the "Continue listening" row).
 *
 * Additive columns — `sourceKind` NOT NULL with a default so existing rows read `manual`, `coverArtId`
 * nullable. Each ALTER is wrapped in try/catch and tolerant of a fresh install that already created
 * the table complete at v18 — the same idempotency rationale as [addDownloadCenterColumns].
 */
val MIGRATION_CACHE_17_18 = object : Migration(17, 18) {
	override suspend fun migrate(connection: SQLiteConnection) {
		listOf(
			"ALTER TABLE SavedQueueEntity ADD COLUMN sourceKind TEXT NOT NULL DEFAULT 'manual'",
			"ALTER TABLE SavedQueueEntity ADD COLUMN coverArtId TEXT"
		).forEach { statement ->
			try {
				connection.execSQL(statement)
			} catch (_: Throwable) {
				// Column already present — nothing to do.
			}
		}
	}
}
