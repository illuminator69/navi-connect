package paige.navic.ui.screens.artist.viewmodels

import androidx.compose.foundation.ScrollState
import androidx.compose.runtime.Immutable
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import paige.navic.data.database.dao.AlbumDao
import paige.navic.data.database.dao.ArtistDao
import paige.navic.data.database.entities.DownloadStatus
import paige.navic.data.database.mappers.toDomainModel
import paige.navic.domain.models.DomainAlbum
import paige.navic.domain.models.DomainArtist
import paige.navic.domain.models.DomainSong
import paige.navic.domain.repositories.AlbumRepository
import paige.navic.domain.repositories.ArtistRepository
import paige.navic.domain.repositories.DbRepository
import paige.navic.domain.repositories.SongRepository
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.domain.manager.LbBotManager
import paige.navic.domain.manager.LbRelease
import paige.navic.util.core.Logger
import paige.navic.util.core.albumTitleKey
import paige.navic.shared.MediaPlayerViewModel
import paige.navic.ui.core.UiState

@Immutable
data class ArtistState(
	val artist: DomainArtist,
	val albums: List<DomainAlbum>,
	val topSongs: List<DomainSong>,
	val similarArtists: List<DomainArtist> = emptyList()
)

/**
 * One release on the artist's discography shelf.
 *
 * Owned, partly-owned and not-owned releases are the same kind of thing here on
 * purpose — the shelf is the artist's discography, not a shopping list bolted onto
 * a library list. [album] is set when Navidrome has it, [release] when lb-bot's
 * index knows it, and at least one of the two always is.
 */
@Immutable
data class DiscographyEntry(
	val key: String,
	val title: String,
	val year: String,
	val album: DomainAlbum? = null,
	val release: LbRelease? = null
) {
	val owned: Boolean get() = album != null

	/**
	 * lb-bot says the library holds this, but Navidrome hasn't given us an album row
	 * yet — which is exactly what a just-completed fill looks like, because lb-bot
	 * flips its index row to `present` the moment the files are placed and Room only
	 * learns about it on the next library sync.
	 *
	 * This state exists because without it the tile *disappeared on success*: it was
	 * no longer `missing`, so it dropped out of the absent list, and there was no
	 * album to put in the owned list either.
	 */
	val pendingSync: Boolean get() = album == null && release?.isMissing == false

	/** Partly owned: lb-bot has a Fill-gaps group for the tracks that are absent. */
	val gapGroupId: String? get() = release?.takeIf { it.isIncomplete }?.groupId
	val presentTracks: Int? get() = release?.present
	val totalTracks: Int? get() = release?.total
	val missingTracks: Int
		get() = ((release?.total ?: 0) - (release?.present ?: 0)).coerceAtLeast(0)
}

@Immutable
data class DiscographySection(
	val type: String,
	val entries: List<DiscographyEntry>
)

@Immutable
data class DiscographyUi(
	/** The lb-bot layer answered. False means render nothing at all. */
	val available: Boolean = false,
	/** lb-bot has never scanned this artist — offer to, don't treat it as an error. */
	val indexed: Boolean = false,
	val indexing: Boolean = false,
	val sections: List<DiscographySection> = emptyList()
)

class ArtistDetailViewModel(
	private val artistId: String,
	private val repository: DbRepository,
	private val artistRepository: ArtistRepository,
	private val songRepository: SongRepository,
	private val albumRepository: AlbumRepository,
	private val artistDao: ArtistDao,
	private val albumDao: AlbumDao,
	private val downloadManager: DownloadManager,
	private val lbBotManager: LbBotManager,
	connectivityManager: ConnectivityManager
) : ViewModel() {
	private val _artistState = MutableStateFlow<UiState<ArtistState>>(UiState.Loading())
	val artistState = _artistState.asStateFlow()

	private val _starred = MutableStateFlow(false)
	val starred = _starred.asStateFlow()

	private val _selectedSong = MutableStateFlow<DomainSong?>(null)
	val selectedSong = _selectedSong.asStateFlow()

	private val _selectedSongIsStarred = MutableStateFlow(false)
	val selectedSongIsStarred = _selectedSongIsStarred.asStateFlow()

	private val _selectedSongRating = MutableStateFlow(0)
	val selectedSongRating = _selectedSongRating.asStateFlow()

	private val _selectedAlbum = MutableStateFlow<DomainAlbum?>(null)
	val selectedAlbum = _selectedAlbum.asStateFlow()

	private val _selectedAlbumIsStarred = MutableStateFlow(false)
	val selectedAlbumIsStarred = _selectedAlbumIsStarred.asStateFlow()

	private val _selectedAlbumRating = MutableStateFlow(0)
	val selectedAlbumRating = _selectedAlbumRating.asStateFlow()

	val isOnline = connectivityManager.isOnline

	val allDownloads = downloadManager.allDownloads
		.stateIn(
			scope = viewModelScope,
			started = SharingStarted.Lazily,
			initialValue = emptyList()
		)

	val scrollState = ScrollState(initial = 0)

	private val _discography = MutableStateFlow(DiscographyUi())
	val discography = _discography.asStateFlow()

	/** The tile whose sheet is open, if it's a release the library doesn't have. */
	private val _selectedRelease = MutableStateFlow<LbRelease?>(null)
	val selectedRelease = _selectedRelease.asStateFlow()

	/** The tile whose sheet is open, if it's a partly-owned album with a gap. */
	private val _selectedGap = MutableStateFlow<DiscographyEntry?>(null)
	val selectedGap = _selectedGap.asStateFlow()

	init {
		loadArtistData()
		// A fill landing — here or on another client — changes two answers at once:
		// Navidrome has an album it didn't, and lb-bot no longer counts that
		// release-group as missing. Showing one without the other is exactly the
		// double-listing the reconciliation below exists to prevent.
		viewModelScope.launch {
			lbBotManager.libraryRevision.drop(1).collect { loadDiscography() }
		}
	}

	private fun loadArtistData() {
		viewModelScope.launch {
			try {
				val artistEntity = artistDao.getArtistById(artistId)
					?: throw Exception("Artist not found in database")
				val domainArtist = artistEntity.toDomainModel()

				var albumsWithSongs =
					albumDao.getAlbumsByArtist(artistId).firstOrNull() ?: emptyList()

				if (albumsWithSongs.isEmpty()) {
					albumsWithSongs = albumDao.getAlbumsByArtistName(domainArtist.name).firstOrNull() ?: emptyList()
				}

				val domainAlbums = albumsWithSongs.map { it.toDomainModel() }

				val domainSongs = albumsWithSongs.flatMap { it.songs }
					.map { it.toDomainModel() }
					.sortedByDescending { it.playCount }
					.take(12)

				val initialSimilarArtists = domainArtist.similarArtistIds.mapNotNull { id ->
					artistDao.getArtistById(id)?.toDomainModel()
				}

				_starred.value = artistRepository.isArtistStarred(domainArtist)

				_artistState.value = UiState.Success(
					ArtistState(
						artist = domainArtist,
						albums = domainAlbums,
						topSongs = domainSongs,
						similarArtists = initialSimilarArtists
					)
				)

				// Fire-and-forget, and strictly after the page has its own data: the
				// discography shelf is an enhancement, and an artist page must render
				// identically whether or not lb-bot is there.
				loadDiscography()

				repository.fetchArtistMetadata(artistId)
					.onSuccess { updatedArtist ->
						val currentState = (_artistState.value as? UiState.Success)?.data
						if (currentState != null) {

							val updatedSimilarArtists =
								updatedArtist.similarArtistIds.mapNotNull { id ->
									artistDao.getArtistById(id)?.toDomainModel()
								}

							_artistState.value = UiState.Success(
								currentState.copy(
									artist = updatedArtist,
									similarArtists = updatedSimilarArtists
								)
							)
						}
					}
					.onFailure { error ->
						Logger.e("ArtistDetailViewModel", "Failed to fetch artist metadata", error)
					}
			} catch (e: Exception) {
				_artistState.value = UiState.Error(e)
			}
		}
	}

	// ------------------------------------------------------------------ //
	// lb-bot discography
	// ------------------------------------------------------------------ //

	/**
	 * Build the discography shelf: everything Navidrome has by this artist, plus
	 * everything lb-bot's MusicBrainz index says exists and the library doesn't.
	 *
	 * Navidrome comes first and is never dropped. lb-bot's list is its own view of
	 * the artist, and an album whose Navidrome record its matcher couldn't claim has
	 * no row at all — so building the shelf out of lb-bot's list would silently hide
	 * albums the user owns.
	 */
	fun loadDiscography() {
		val state = (_artistState.value as? UiState.Success)?.data ?: return
		viewModelScope.launch {
			if (!isOnline.value || !lbBotManager.probeAvailable()) {
				_discography.value = DiscographyUi(available = false)
				return@launch
			}
			val data = lbBotManager.discography(state.artist.id, state.artist.musicBrainzId)
			if (data == null) {
				_discography.value = DiscographyUi(available = false)
				return@launch
			}
			if (!data.indexed) {
				_discography.value = DiscographyUi(available = true, indexed = false)
				return@launch
			}
			_discography.value = DiscographyUi(
				available = true,
				indexed = true,
				sections = buildSections(state.albums, data.releases)
			)
		}
	}

	/** Start the (slow, rate-limited) MusicBrainz walk, then wait for the index to fill. */
	fun indexArtist() {
		val state = (_artistState.value as? UiState.Success)?.data ?: return
		val mbid = state.artist.musicBrainzId
		if (mbid.isNullOrBlank()) return
		viewModelScope.launch {
			_discography.value = _discography.value.copy(indexing = true)
			// Watch `scanned_at`, not `indexed`. For an already-indexed artist `indexed` is true on
			// the very first tick, so the spinner cleared while the walk was still running — in
			// exactly the case a rescan button exists for. A first scan reads 0 here, so the same
			// "it moved" rule covers both.
			val scannedAtBefore = lbBotManager.discography(state.artist.id, mbid)?.scannedAt ?: 0.0
			if (!lbBotManager.indexArtist(mbid, state.artist.name, state.artist.id)) {
				_discography.value = _discography.value.copy(indexing = false)
				return@launch
			}
			// A big discography takes 10-60s at MusicBrainz's one request a second.
			// Re-read the (instant) index a few times and let the shelf appear when it
			// appears — a miss just means "not yet". Never poll the returned task id:
			// that route deep-copies lb-bot's whole review state and is not whitelisted.
			repeat(INDEX_POLL_ATTEMPTS) {
				delay(INDEX_POLL_INTERVAL_MS)
				val data = lbBotManager.discography(state.artist.id, mbid)
				if (data?.indexed == true && data.scannedAt > scannedAtBefore) {
					_discography.value = DiscographyUi(
						available = true,
						indexed = true,
						sections = buildSections(state.albums, data.releases)
					)
					return@launch
				}
			}
			_discography.value = _discography.value.copy(indexing = false)
		}
	}

	/**
	 * Pure list-crunching, so it runs on [Dispatchers.Default].
	 *
	 * `viewModelScope` dispatches on `Main.immediate`, so without this the whole matching pass —
	 * two maps over every lb-bot release, a pass over every owned album, then a groupBy and a sort
	 * per section — ran on the UI thread, landing precisely as the discography painted. A prolific
	 * artist with a large lb-bot index is where that was felt.
	 */
	private suspend fun buildSections(
		albums: List<DomainAlbum>,
		releases: List<LbRelease>
	): List<DiscographySection> = withContext(Dispatchers.Default) {
		val claimedRelease = mutableMapOf<String, LbRelease>()
		val usedRgids = mutableSetOf<String>()

		// Navidrome album ids are authoritative — lb-bot wrote them itself when it
		// matched the release-group. Fall back to a normalised title only for the rows
		// it couldn't attach an id to.
		val byNdId = mutableMapOf<String, LbRelease>()
		val byTitle = mutableMapOf<String, LbRelease>()
		for (release in releases) {
			release.navidromeAlbumIds.forEach { id ->
				if (id.isNotBlank() && id !in byNdId) byNdId[id] = release
			}
			val key = albumTitleKey(release.title)
			if (key.isNotEmpty() && key !in byTitle) byTitle[key] = release
		}
		for (album in albums) {
			val match = byNdId[album.id] ?: byTitle[albumTitleKey(album.name)]
			if (match != null && usedRgids.add(match.rgid)) claimedRelease[album.id] = match
		}

		val owned = albums.map { album ->
			val release = claimedRelease[album.id]
			DiscographyEntry(
				key = album.id,
				title = album.name,
				year = release?.year ?: album.year?.toString().orEmpty(),
				album = album,
				release = release
			)
		}
		// Every lb-bot row that didn't attach to a Navidrome album, whatever its
		// status — NOT just the `missing` ones.
		//
		// Filtering to `missing` here is what made a filled album vanish. lb-bot
		// flips its index row to `present` as soon as the files are placed, so a
		// successful download turned the row non-missing while Room still had no
		// album for it, and the tile fell out of both lists. A `missing` row that
		// matched an owned album is still absorbed into it above rather than
		// dropped, which is what stops the same record rendering twice.
		val absent = releases
			.filter { it.rgid.isNotBlank() && it.rgid !in usedRgids }
			.map { release ->
				DiscographyEntry(
					key = release.rgid,
					title = release.title,
					year = release.year,
					release = release
				)
			}

		(owned + absent)
			.groupBy { sectionType(it) }
			.map { (type, entries) ->
				DiscographySection(
					type = type,
					entries = entries.sortedWith(
						compareByDescending<DiscographyEntry> { it.year }
							.thenBy { it.title.lowercase() }
					)
				)
			}
			.sortedBy { TYPE_ORDER.indexOf(it.type).takeIf { i -> i >= 0 } ?: TYPE_ORDER.size }
	}

	/**
	 * lb-bot's `effective_type`, raw and lowercase. The display name is the
	 * composable's job — turning "ep" into a heading here produced "Ep", and a
	 * `replaceFirstChar` can never know that this particular vocabulary contains an
	 * initialism. An album lb-bot doesn't know about is an album.
	 */
	private fun sectionType(entry: DiscographyEntry): String =
		entry.release?.effectiveType?.takeIf { it.isNotBlank() }
			?: entry.release?.primaryType?.takeIf { it.isNotBlank() }
			?: "album"

	fun selectRelease(release: LbRelease?) {
		_selectedRelease.value = release
	}

	fun selectGap(entry: DiscographyEntry?) {
		_selectedGap.value = entry
	}

	fun selectSong(song: DomainSong) {
		viewModelScope.launch {
			_selectedSong.value = song
			_selectedSongIsStarred.value = songRepository.isSongStarred(song)
			_selectedSongRating.value = songRepository.getSongRating(song)
		}
	}

	fun clearSelection() {
		_selectedSong.value = null
	}

	fun selectAlbum(album: DomainAlbum) {
		viewModelScope.launch {
			_selectedAlbum.value = album
			_selectedAlbumIsStarred.value = albumRepository.isAlbumStarred(album)
			_selectedAlbumRating.value = albumRepository.getAlbumRating(album)
		}
	}

	fun rateSelectedAlbum(rating: Int) {
		viewModelScope.launch {
			val selection = _selectedAlbum.value ?: return@launch
			runCatching {
				_selectedAlbumRating.value = rating
				albumRepository.rateAlbum(selection, rating)
			}
		}
	}

	fun clearAlbumSelection() {
		_selectedAlbum.value = null
	}

	fun starSelectedSong() {
		viewModelScope.launch {
			val selection = _selectedSong.value ?: return@launch
			runCatching {
				_selectedSongIsStarred.value = true
				songRepository.starSong(selection)
				loadArtistData()
			}
		}
	}

	fun unstarSelectedSong() {
		viewModelScope.launch {
			val selection = _selectedSong.value ?: return@launch
			runCatching {
				_selectedSongIsStarred.value = false
				songRepository.unstarSong(selection)
				loadArtistData()
			}
		}
	}

	fun rateSelectedSong(rating: Int) {
		viewModelScope.launch {
			val selection = _selectedSong.value ?: return@launch
			runCatching {
				_selectedSongRating.value = rating
				songRepository.rateSong(selection, rating)
			}
		}
	}

	fun starArtist(starred: Boolean) {
		val artist = (_artistState.value as? UiState.Success)?.data?.artist ?: return
		viewModelScope.launch {
			runCatching {
				if (starred) {
					artistRepository.starArtist(artist)
				} else {
					artistRepository.unstarArtist(artist)
				}
				_starred.value = starred
			}
		}
	}

	fun starAlbum(starred: Boolean) {
		viewModelScope.launch {
			val selection = _selectedAlbum.value ?: return@launch
			runCatching {
				if (starred) {
					albumRepository.starAlbum(selection)
				} else {
					albumRepository.unstarAlbum(selection)
				}
				_selectedAlbumIsStarred.value = starred
			}
		}
	}

	fun playArtistAlbums(player: MediaPlayerViewModel) {
		(_artistState.value as? UiState.Success)?.data?.let { state ->
			player.clearQueue()
			state.albums.forEach { album ->
				player.addToQueue(album)
			}
			player.togglePlay()
		}
	}

	fun downloadSong(song: DomainSong) {
		downloadManager.downloadSong(song)
	}

	fun cancelDownload(songId: String) {
		downloadManager.cancelDownload(songId)
	}

	fun deleteDownload(songId: String) {
		downloadManager.deleteDownload(songId)
	}

	@OptIn(ExperimentalCoroutinesApi::class)
	fun collectionDownloadStatus(): Flow<DownloadStatus> {
		return artistState.flatMapLatest { state ->
			if (state is UiState.Success) {
				val allArtistSongIds = state.data.albums.flatMap { album ->
					album.songs.map { it.id }
				}

				if (allArtistSongIds.isEmpty()) {
					flowOf(DownloadStatus.NOT_DOWNLOADED)
				} else {
					downloadManager.getCollectionDownloadStatus(allArtistSongIds)
				}
			} else {
				flowOf(DownloadStatus.NOT_DOWNLOADED)
			}
		}
	}

	private companion object {
		const val INDEX_POLL_ATTEMPTS = 20
		const val INDEX_POLL_INTERVAL_MS = 5_000L

		/** Shelf order, in lb-bot's own vocabulary (`_TYPE_DEFINING_SECONDARY` plus the
		 *  primary types it browses). Anything new lands at the end rather than
		 *  disappearing. */
		val TYPE_ORDER = listOf(
			"album", "ep", "single", "compilation", "soundtrack", "live", "remix", "demo"
		)
	}
}
