package paige.navic.data.database.mappers

import paige.navic.data.database.entities.ArtistEntity
import paige.navic.domain.manager.RawArtist
import paige.navic.domain.models.DomainArtist
import dev.zt64.subsonic.api.model.Artist as ApiArtist
import kotlin.time.Instant

/**
 * Maps the raw `search3` artist payload (see [SessionManager.fetchAllArtists])
 * into [ArtistEntity]. `roles`/`biography`/`similar` aren't returned by
 * search3 and are filled lazily elsewhere, matching the library mapper below.
 */
fun RawArtist.toEntity() = ArtistEntity(
	artistId = this.id,
	name = this.name,
	albumCount = this.albumCount,
	coverArtId = this.coverArt,
	artistImageUrl = this.artistImageUrl,
	starredAt = this.starred?.let { runCatching { Instant.parse(it) }.getOrNull() },
	userRating = this.userRating,
	sortName = this.sortName,
	musicBrainzId = this.musicBrainzId,
	lastFmUrl = null,
	roles = emptyList(),
	biography = null,
	similarArtistIds = emptyList()
)

fun ApiArtist.toEntity() = ArtistEntity(
	artistId = this.id,
	name = this.name,
	albumCount = this.albumCount,
	coverArtId = this.coverArtId,
	artistImageUrl = this.artistImageUrl,
	starredAt = this.starredAt,
	userRating = this.userRating,
	sortName = this.sortName,
	musicBrainzId = this.musicBrainzId,
	lastFmUrl = null,
	roles = this.roles,
	biography = null,
	similarArtistIds = emptyList()
)

fun ArtistEntity.toDomainModel() = DomainArtist(
	id = this.artistId,
	name = this.name,
	albumCount = this.albumCount,
	coverArtId = this.coverArtId,
	artistImageUrl = this.artistImageUrl,
	starredAt = this.starredAt,
	userRating = this.userRating,
	sortName = this.sortName,
	musicBrainzId = this.musicBrainzId,
	lastFmUrl = this.lastFmUrl,
	roles = this.roles,
	biography = this.biography,
	similarArtistIds = this.similarArtistIds
)

fun DomainArtist.toEntity() = ArtistEntity(
	artistId = this.id,
	name = this.name,
	albumCount = this.albumCount,
	coverArtId = this.coverArtId,
	artistImageUrl = this.artistImageUrl,
	starredAt = this.starredAt,
	userRating = this.userRating,
	sortName = this.sortName,
	musicBrainzId = this.musicBrainzId,
	lastFmUrl = this.lastFmUrl,
	roles = this.roles,
	biography = this.biography,
	similarArtistIds = this.similarArtistIds
)
