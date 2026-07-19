package paige.navic

// Android installs the Coil singleton factory via the Application
// (SingletonImageLoader.Factory), so the composable install in App() must not
// run — it would clash with the loader the Application already created.
internal actual val installComposeSingletonImageLoader: Boolean = false
