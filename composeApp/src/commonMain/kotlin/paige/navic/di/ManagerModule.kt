package paige.navic.di

import org.koin.core.module.dsl.singleOf
import org.koin.dsl.module
import paige.navic.domain.manager.AudioMuseManager
import paige.navic.domain.manager.DownloadManager
import paige.navic.domain.manager.HubManager
import paige.navic.domain.manager.NativeApiManager
import paige.navic.domain.manager.PlaylistDownloadManager
import paige.navic.domain.manager.PreferenceManager
import paige.navic.domain.manager.RadioManager
import paige.navic.domain.manager.SessionManager
import paige.navic.domain.manager.SleepTimerManager
import paige.navic.domain.manager.SyncManager
import paige.navic.util.ui.AmbientColorHolder

val managerModule = module {
	singleOf(::AmbientColorHolder)
	singleOf(::SleepTimerManager)
	single(createdAtStart = true) {
		SyncManager(get(), get(), get(), get(), get(), get()).apply {
			startPeriodicSync()
		}
	}
	singleOf(::DownloadManager)
	singleOf(::SessionManager)
	singleOf(::PreferenceManager)
	singleOf(::AudioMuseManager)
	single(createdAtStart = true) {
		HubManager(get(), get(), get(), get()).apply { start() }
	}
	// createdAtStart so the autoplay observer is running before the first radio use.
	single(createdAtStart = true) { RadioManager(get(), get(), get(), get(), get(), get()) }
	singleOf(::NativeApiManager)
	single(createdAtStart = true) { PlaylistDownloadManager(get(), get(), get(), get()) }
}
