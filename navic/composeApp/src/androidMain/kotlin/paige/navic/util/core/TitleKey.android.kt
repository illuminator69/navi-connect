package paige.navic.util.core

import java.text.Normalizer

actual fun toNfd(value: String): String = Normalizer.normalize(value, Normalizer.Form.NFD)
