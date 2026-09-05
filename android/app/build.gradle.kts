plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "dev.vma.sensor"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.vma.sensor"
        minSdk = 26
        targetSdk = 36
        versionCode = 3
        versionName = "0.2.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug") // personal-device APK
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-runtime:2.6.2")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.camera:camera-camera2:1.4.2")
    implementation("androidx.camera:camera-lifecycle:1.4.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.android.gms:play-services-location:21.3.0")

    // Material 3 Expressive stack — versions pinned to .offline-m2 exactly.
    implementation("androidx.compose.material3:material3:1.4.0")
    implementation("androidx.compose.ui:ui:1.10.0")
    implementation("androidx.compose.foundation:foundation:1.10.0")
    implementation("androidx.compose.runtime:runtime:1.10.0")
    implementation("androidx.compose.material:material-ripple:1.10.0")
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
}

configurations.all {
    resolutionStrategy {
        // The offline mirror has no emoji2-views-helper 1.4.0; appcompat's
        // alignment constraint elevates it and breaks --offline resolution.
        // 1.3.0 is the mirrored copy and is API-compatible for this app.
        force("androidx.emoji2:emoji2-views-helper:1.3.0")
    }
}
