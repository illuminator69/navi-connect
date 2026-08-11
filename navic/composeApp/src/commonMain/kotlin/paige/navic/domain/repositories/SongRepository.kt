package paige.navic.domain.repositories

import kotlinx.collections.immutable.ImmutableList
import kotlinx.collections.immutable.toImmutableList
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.IO
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import paige.navic.domain.manager.SyncManager
import paige.navic.data.database.dao.DownloadDao
import paige.navic.data.database.dao.SongDao
import paige.navic.data.database.entities.SyncActionType
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.data.database.mappers.toEntity
import paige.navic.domain.models.DomainSong
import paige.navic.domain.models.DomainSongListType
import paige.navic.ui.core.UiState
import paige.navic.util.core.toSongSqlQuery
import kotlin.time.Clock

class SongRepository(
	private val songDao: SongDao,
	private val downloadDao: DownloadDao,
	private val dbRepository: DbRepository,
	private val syncManager: SyncManager
) {
	suspend fun getAllSongs(): List<DomainSong> {
		return songDao.getAllSongs().map { it.toDomainModel() }
	}

	private suspend fun getLocalData(
		listType: DomainSongListType,
		reversed: Boolean,
		artistId: String? = null
	): ImmutableList<DomainSong> {
		val songs = if (listType == DomainSongListType.Downloaded) {
			getDownloadedSongs(artistId)
		} else {
			songDao
				.getSongsByQuery(listType.toSongSqlQuery(artistId))
				.map { it.toDomainModel() }
		}

		return if (reversed) {
			songs.asReversed().toImmutableList()
		} else {
			songs.toImmutableList()
		}
	}

	/**
	 * Downloads are stored in a different database file than songs, so this filter can't be a JOIN:
	 * the downloaded ids get bound into the song query instead. SQLite caps how many variables one
	 * statement may bind, so the ids are chunked — which means each chunk sorts independently and
	 * the result has to be re-sorted. That sort is over downloaded songs only, not the library.
	 */
	private suspend fun getDownloadedSongs(artistId: String?): List<DomainSong> {
		val downloadedSongIds = downloadDao.getSongIdsByStatus()
		if (downloadedSongIds.isEmpty()) return emptyList()

		return downloadedSongIds
			.chunked(SQLITE_BIND_CHUNK)
			.flatMap { chunk ->
				songDao.getSongsByQuery(
					DomainSongListType.Downloaded.toSongSqlQuery(artistId, chunk)
				)
			}
			.map { it.toDomainModel() }
			.sortedBy { it.title.lowercase() }
	}

	private suspend fun refreshLocalData(
		listType: DomainSongListType,
		reversed: Boolean,
		artistId: String? = null
	): ImmutableList<DomainSong> {
		dbRepository.syncLibrarySongs().getOrThrow()
		return getLocalData(listType, reversed, artistId)
	}

	fun getSongsFlow(
		fullRefresh: Boolean,
		listType: DomainSongListType,
		reversed: Boolean,
		artistId: String? = null
	): Flow<UiState<ImmutableList<DomainSong>>> = flow {
		val localData = getLocalData(listType, reversed, artistId)
		if (fullRefresh) {
			emit(UiState.Loading(data = localData))
			try {
				emit(UiState.Success(data = refreshLocalData(listType, reversed, artistId)))
			} catch (error: Exception) {
				emit(UiState.Error(error = error, data = localData))
			}
		} else {
			emit(UiState.Success(data = localData))
		}
	}.flowOn(Dispatchers.IO)

	suspend fun isSongStarred(song: DomainSong) = songDao.isSongStarred(song.id)
	suspend fun getSongRating(song: DomainSong) = songDao.getSongRating(song.id) ?: 0
	suspend fun starSong(song: DomainSong) {
		val starredEntity = song.toEntity().copy(
			starredAt = Clock.System.now()
		)
		songDao.insertSong(starredEntity)
		syncManager.enqueueAction(SyncActionType.STAR, song.id)
	}

	suspend fun unstarSong(song: DomainSong) {
		val unstarredEntity = song.toEntity().copy(
			starredAt = null
		)
		songDao.insertSong(unstarredEntity)
		syncManager.enqueueAction(SyncActionType.UNSTAR, song.id)
	}

	suspend fun rateSong(song: DomainSong, rating: Int) {
		val ratedEntity = song.toEntity().copy(
			userRating = rating
		)
		songDao.insertSong(ratedEntity)
		when (rating) {
			0 -> syncManager.enqueueAction(SyncActionType.STAR_0, song.id)
			1 -> syncManager.enqueueAction(SyncActionType.STAR_1, song.id)
			2 -> syncManager.enqueueAction(SyncActionType.STAR_2, song.id)
			3 -> syncManager.enqueueAction(SyncActionType.STAR_3, song.id)
			4 -> syncManager.enqueueAction(SyncActionType.STAR_4, song.id)
			5 -> syncManager.enqueueAction(SyncActionType.STAR_5, song.id)
		}
	}

	private companion object {
		/** Kept well under SQLite's bound-variable ceiling, leaving room for the artist filter. */
		const val SQLITE_BIND_CHUNK = 900
	}
}
