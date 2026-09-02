plugins {
    id("com.android.application") version "9.2.1" apply false
    // Material 3 Expressive via Jetpack Compose; versions pinned to the
    // project-local offline mirror (.offline-m2) — do not bump without mirroring.
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.10" apply false
}
