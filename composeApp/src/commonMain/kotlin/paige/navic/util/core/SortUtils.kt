package paige.navic.util.core

import androidx.room3.RoomRawQuery
import androidx.sqlite.SQLiteStatement
import paige.navic.domain.models.DomainAlbumListType
import paige.navic.domain.models.DomainSongListType

/**
 * Sorting/filtering for the song list, expressed as SQL so the DAO returns the rows already in
 * order instead of the repository loading every song (plus every album and download) into memory
 * to sort them.
 *
 * [downloadedSongIds] only matters for [DomainSongListType.Downloaded]. Downloads live in a
 * *separate database file* from songs (`downloads.db` vs `cache.db` — the `DownloadEntity` declared
 * on `CacheDatabase` is never written to), so that one filter cannot be a JOIN and the ids have to
 * be fetched first and bound in.
 *
 * Ordering notes, to match the previous in-memory behaviour:
 * - `Rating` coalesces a missing rating to 0, as `userRating ?: 0` did.
 * - `Year`/`Newest` leave rows with no year/album last: SQLite sorts NULL lowest, so `DESC` puts
 *   them at the end, which is where Kotlin's descending sort put them too.
 * Ties additionally break on title so the order is stable between calls; the old in-memory sort
 * left ties in (arbitrary) table order.
 */
fun DomainSongListType.toSongSqlQuery(
	artistId: String? = null,
	downloadedSongIds: List<String> = emptyList()
): RoomRawQuery {
	val conditions = mutableListOf<String>()
	val args = mutableListOf<Any>()
	var join = ""
	val orderBy: String

	when (this) {
		DomainSongListType.FrequentlyPlayed ->
			orderBy = "SongEntity.playCount DESC, LOWER(SongEntity.title) ASC"
		DomainSongListType.Newest -> {
			join = " LEFT JOIN AlbumEntity ON SongEntity.belongsToAlbumId = AlbumEntity.albumId"
			orderBy = "AlbumEntity.createdAt DESC, LOWER(SongEntity.title) ASC"
		}
		DomainSongListType.Starred -> {
			conditions.add("SongEntity.starredAt IS NOT NULL")
			orderBy = "SongEntity.starredAt ASC, LOWER(SongEntity.title) ASC"
		}
		DomainSongListType.Random -> orderBy = "RANDOM()"
		DomainSongListType.Downloaded -> {
			if (downloadedSongIds.isEmpty()) {
				// Nothing is downloaded, so the list is empty. "WHERE 0" beats skipping the query
				// so callers still get an ordinary (empty) result.
				conditions.add("0")
			} else {
				val placeholders = downloadedSongIds.joinToString(",") { "?" }
				conditions.add("SongEntity.songId IN ($placeholders)")
				args.addAll(downloadedSongIds)
			}
			orderBy = "LOWER(SongEntity.title) ASC"
		}
		DomainSongListType.Rating ->
			orderBy = "COALESCE(SongEntity.userRating, 0) DESC, LOWER(SongEntity.title) ASC"
		DomainSongListType.Year ->
			orderBy = "SongEntity.year DESC, LOWER(SongEntity.title) ASC"
	}

	// Appended after the branch above so the bind order keeps matching the order the "?" appear in.
	if (artistId != null) {
		conditions.add("SongEntity.artistId = ?")
		args.add(artistId)
	}

	val whereClause = if (conditions.isEmpty()) "" else " WHERE ${conditions.joinToString(" AND ")}"
	val sql = "SELECT SongEntity.* FROM SongEntity$join$whereClause ORDER BY $orderBy"

	return RoomRawQuery(sql) { statement -> statement.bindAll(args) }
}

fun DomainAlbumListType.toSqlQuery(limit: Int? = null): RoomRawQuery {
	var where: String? = null
	var orderBy: String
	val args = mutableListOf<Any>()

	when (this) {
		DomainAlbumListType.AlphabeticalByArtist -> orderBy = "LOWER(artistName) ASC"
		DomainAlbumListType.AlphabeticalByName -> orderBy = "LOWER(name) ASC"
		DomainAlbumListType.Frequent -> {
			where = "playCount != 0"
			orderBy = "playCount DESC"
		}
		DomainAlbumListType.Highest -> orderBy = "userRating DESC"
		DomainAlbumListType.Newest -> orderBy = "createdAt DESC"
		DomainAlbumListType.Random -> orderBy = "RANDOM()"
		DomainAlbumListType.Downloaded,
		DomainAlbumListType.Recent -> orderBy = "lastPlayedAt DESC"
		DomainAlbumListType.Starred -> {
			where = "starredAt IS NOT NULL"
			orderBy = "starredAt ASC"
		}
		is DomainAlbumListType.ByGenre -> {
			where = "genre = ?"
			orderBy = "LOWER(name) ASC"
			args.add(genre)
		}
		is DomainAlbumListType.ByYear -> {
			if (fromYear != null && toYear != null) {
				where = "COALESCE(year, 0) BETWEEN ? AND ?"
				orderBy = "LOWER(name) ASC"
				args.add(fromYear)
				args.add(toYear)
			} else {
				orderBy = "year DESC"
			}
		}
	}

	val whereClause = where?.let { " WHERE $it" } ?: ""
	val limitClause = limit?.let { " LIMIT $it" } ?: ""
	val sql = "SELECT * FROM AlbumEntity$whereClause ORDER BY $orderBy$limitClause"

	return RoomRawQuery(sql) { statement -> statement.bindAll(args) }
}

/** Binds [args] positionally, so they must be in the same order as the "?" in the statement. */
private fun SQLiteStatement.bindAll(args: List<Any>) {
	args.forEachIndexed { index, arg ->
		val bindIndex = index + 1
		when (arg) {
			is String -> bindText(bindIndex, arg)
			is Int -> bindInt(bindIndex, arg)
			is Long -> bindLong(bindIndex, arg)
			is Float -> bindFloat(bindIndex, arg)
			is Double -> bindDouble(bindIndex, arg)
			is Boolean -> bindInt(bindIndex, if (arg) 1 else 0)
		}
	}
}
