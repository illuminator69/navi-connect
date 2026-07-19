package paige.navic

// iOS has no Application-level SingletonImageLoader.Factory, so App() installs
// the singleton Coil factory itself.
internal actual val installComposeSingletonImageLoader: Boolean = true
