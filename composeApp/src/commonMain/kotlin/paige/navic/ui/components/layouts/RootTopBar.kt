package paige.navic.ui.components.layouts

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.RowScope
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MediumFlexibleTopAppBar
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.TopAppBarScrollBehavior
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.lifecycle.compose.dropUnlessResumed
import navic.composeapp.generated.resources.Res
import navic.composeapp.generated.resources.action_log_out
import navic.composeapp.generated.resources.action_sleep_timer
import navic.composeapp.generated.resources.action_sleep_timer_enabled
import navic.composeapp.generated.resources.action_view_shares
import navic.composeapp.generated.resources.title_saved_queues
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinViewModel
import paige.navic.LocalPlatformContext
import paige.navic.LocalNavStack
import paige.navic.domain.models.settings.NavbarConfig
import paige.navic.domain.models.settings.NavbarTab
import paige.navic.ui.navigation.Screen
import paige.navic.icons.Icons
import paige.navic.icons.filled.Settings
import paige.navic.icons.outlined.AccountCircle
import paige.navic.icons.outlined.Bedtime
import paige.navic.icons.outlined.Logout
import paige.navic.icons.outlined.Queue
import paige.navic.icons.outlined.Search
import paige.navic.icons.outlined.Share
import paige.navic.domain.manager.SleepTimerManager
import paige.navic.ui.components.common.Dropdown
import paige.navic.ui.components.common.DropdownItem
import paige.navic.ui.components.common.blur.LocalExpressiveBlur
import paige.navic.ui.components.common.blur.expressiveBlurEffect
import paige.navic.ui.components.sheets.SleepTimerSheet
import paige.navic.ui.screens.login.viewmodels.LoginViewModel
import paige.navic.ui.screens.settings.viewmodels.NavtabsViewModel
import paige.navic.ui.theme.positive
import paige.navic.ui.core.UiState
import paige.navic.util.core.label

@OptIn(
	ExperimentalMaterial3Api::class,
	ExperimentalMaterial3ExpressiveApi::class
)
@Composable
fun RootTopBar(
	title: @Composable () -> Unit,
	scrollBehavior: TopAppBarScrollBehavior,
	actions: @Composable RowScope.() -> Unit = {},
) {
	val backStack = LocalNavStack.current
	val navViewModel = koinViewModel<NavtabsViewModel>()
	val viewModel = koinViewModel<LoginViewModel>()

	val navState by navViewModel.state.collectAsState()
	val config = (navState as? UiState.Success)?.data

	// Frost the scrolled content under the bar (same Haze layer as the mini-player /
	// nav) instead of snapping to a solid surface slab. No-op when Expressive blur is
	// off — the translucent scrolledContainerColor below then keeps the title readable.
	val expressiveBlur = LocalExpressiveBlur.current

	MediumFlexibleTopAppBar(
		modifier = Modifier.expressiveBlurEffect(expressiveBlur),
		title = {
			CompositionLocalProvider(
				LocalTextStyle provides when (LocalTextStyle.current) {
					MaterialTheme.typography.headlineMedium -> MaterialTheme.typography.headlineSmall
					else -> MaterialTheme.typography.titleLarge
				}
			) {
				title()
			}
		},
		actions = {
			actions()
			Actions(
				onLogOut = {
					viewModel.logout()
					backStack.clear()
					backStack.add(Screen.Login)
				},
				config = config,
			)
		},
		scrollBehavior = scrollBehavior,
		// Transparent at rest so the active theme/home-wash shows through. When scrolled,
		// the bar dematerialises rather than becoming a solid slab: a *translucent* surface
		// tint rides over the frosted backdrop (a lower alpha when blur is on, since the haze
		// already carries the contrast; higher when it's off so the title stays readable).
		// Title/icons follow LocalContentColor, so the bar adapts to any cover scheme.
		colors = TopAppBarDefaults.topAppBarColors(
			containerColor = Color.Transparent,
			scrolledContainerColor = MaterialTheme.colorScheme.surface.copy(
				alpha = if (expressiveBlur.enabled) 0.4f else 0.7f
			)
		),
	)
}

@Composable
private fun Actions(
	onLogOut: () -> Unit,
	config: NavbarConfig?,
) {
	val platformContext = LocalPlatformContext.current
	val backStack = LocalNavStack.current

	val isSearchEnabled = config?.tabs?.any {
		it.id == NavbarTab.Id.SEARCH && it.visible
	} == true

	if (!isSearchEnabled) {
		IconButton(
			onClick = dropUnlessResumed {
				platformContext.clickSound()
				backStack.add(Screen.Search(nested = true))
			}
		) {
			Icon(
				Icons.Outlined.Search,
				contentDescription = null
			)
		}
	}

	IconButton(onClick = dropUnlessResumed {
		platformContext.clickSound()
		backStack.add(Screen.SavedQueues)
	}) {
		Icon(
			Icons.Outlined.Queue,
			contentDescription = stringResource(Res.string.title_saved_queues)
		)
	}

	IconButton(onClick = dropUnlessResumed {
		platformContext.clickSound()
		backStack.add(Screen.Settings.Root)
	}) {
		Icon(
			Icons.Filled.Settings,
			contentDescription = null
		)
	}

	var expanded by remember { mutableStateOf(false) }
	var sleepTimerSheetOpen by remember { mutableStateOf(false) }
	val sleepTimerManager = koinInject<SleepTimerManager>()
	val sleepTimerLeft = sleepTimerManager.timeLeft

	Box {
		IconButton(onClick = {
			platformContext.clickSound()
			expanded = true
		}) {
			Icon(
				Icons.Outlined.AccountCircle,
				contentDescription = null
			)
		}
		Dropdown(
			expanded = expanded,
			onDismissRequest = { expanded = false }
		) {
			DropdownItem(
				text = { Text(stringResource(Res.string.action_view_shares)) },
				onClick = dropUnlessResumed {
					expanded = false
					backStack.add(Screen.ShareList)
				},
				leadingIcon = { Icon(Icons.Outlined.Share, null) }
			)

			if (sleepTimerLeft != null) {
				DropdownItem(
					text = { Text(stringResource(Res.string.action_sleep_timer_enabled, sleepTimerLeft.label()), color = MaterialTheme.colorScheme.positive) },
					onClick = {
						expanded = false
						sleepTimerSheetOpen = true
					},
					leadingIcon = { Icon(Icons.Outlined.Bedtime, null, tint = MaterialTheme.colorScheme.positive) }
				)
			} else {
				DropdownItem(
					text = { Text(stringResource(Res.string.action_sleep_timer)) },
					onClick = {
						expanded = false
						sleepTimerSheetOpen = true
					},
					leadingIcon = { Icon(Icons.Outlined.Bedtime, null) }
				)
			}

			DropdownItem(
				text = { Text(stringResource(Res.string.action_log_out)) },
				onClick = {
					expanded = false
					onLogOut()
				},
				leadingIcon = { Icon(Icons.Outlined.Logout, null) }
			)
		}
	}

	if (sleepTimerSheetOpen) {
		SleepTimerSheet(onDismissRequest = { sleepTimerSheetOpen = false })
	}
}
