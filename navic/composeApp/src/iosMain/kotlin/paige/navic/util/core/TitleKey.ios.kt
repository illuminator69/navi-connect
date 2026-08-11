package paige.navic.util.core

import platform.Foundation.NSString
import platform.Foundation.decomposedStringWithCanonicalMapping

@Suppress("CAST_NEVER_SUCCEEDS")
actual fun toNfd(value: String): String =
	(value as NSString).decomposedStringWithCanonicalMapping
