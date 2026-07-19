package paige.navic.di

import androidx.room3.Room
import androidx.sqlite.driver.bundled.BundledSQLiteDriver
import org.koin.android.ext.koin.androidApplication
import org.koin.core.module.dsl.singleOf
import org.koin.dsl.module
import paige.navic.data.database.CacheDatabase
import paige.navic.data.database.DownloadDatabase
import paige.navic.data.database.MIGRATION_CACHE_15_16
import paige.navic.data.database.MIGRATION_CACHE_16_17
import paige.navic.data.database.MIGRATION_CACHE_17_18
import paige.navic.data.database.MIGRATION_DOWNLOAD_3_4
import paige.navic.domain.manager.AndroidCastManager
import paige.navic.domain.manager.CastManager
import paige.navic.domain.manager.ConnectivityManager
import paige.navic.domain.manager.LogManager
import paige.navic.domain.manager.ShareManager
import paige.navic.domain.manager.StorageManager
import paige.navic.domain.repositories.PlayerStateRepository
import paige.navic.shared.AndroidMediaPlayerViewModel
import paige.navic.shared.MediaPlayerViewModel

actual val platformModule = module {
	single<CacheDatabase> {
		val dbPath = androidApplication()
			.getDatabasePath("cache.db")
			.absolutePath
		Room
			.databaseBuilder<CacheDatabase>(get(), dbPath)
			.setDriver(BundledSQLiteDriver())
			// Without this, bumping the cache version for the DownloadEntity change would drop the
			// user's entire cached library and force a full re-sync.
			.addMigrations(MIGRATION_CACHE_15_16, MIGRATION_CACHE_16_17, MIGRATION_CACHE_17_18)
			.fallbackToDestructiveMigration(true)
			.build()
	}

	single<DownloadDatabase> {
		val dbPath = androidApplication()
			.getDatabasePath("downloads.db")
			.absolutePath
		Room
			.databaseBuilder<DownloadDatabase>(get(), dbPath)
			.setDriver(BundledSQLiteDriver())
			// Unlike the cache DB, this one must NOT be dropped on upgrade: its rows are the only
			// record of which audio files exist on disk. Dropping it would orphan every downloaded
			// file — invisible to the app, still occupying the user's storage. The destructive
			// fallback stays only as a last resort for versions with no migration path.
			.addMigrations(MIGRATION_DOWNLOAD_3_4)
			.fallbackToDestructiveMigration(true)
			.build()
	}

	single<PlayerStateRepository> {
		val context = androidApplication()
		val producePath = {
			context.filesDir.resolve(PlayerStateRepository.DATASTORE_FILE_NAME).absolutePath
		}
		PlayerStateRepository(PlayerStateRepository.getInstance(producePath))
	}

	single<MediaPlayerViewModel> {
		AndroidMediaPlayerViewModel(
			application = androidApplication(),
			stateRepository = get(),
			albumDao = get(),
			downloadManager = get(),
			connectivityManager = get(),
			sessionManager = get(),
			preferenceManager = get(),
			savedQueueRepository = get()
		)
	}

	singleOf(::ShareManager)
	singleOf(::StorageManager)
	singleOf(::ConnectivityManager)
	singleOf(::LogManager)
	single<CastManager> { AndroidCastManager(androidApplication()) }
}
