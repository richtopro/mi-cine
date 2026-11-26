plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val runNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "es.micine.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "es.micine.app"
        minSdk = 21
        targetSdk = 35
        versionCode = runNumber
        versionName = "1.0.$runNumber"
        ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") }
    }

    signingConfigs {
        create("micine") {
            storeFile = file("micine.keystore")
            storePassword = "micine123"
            keyAlias = "micine"
            keyPassword = "micine123"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("micine")
        }
        debug {
            signingConfig = signingConfigs.getByName("micine")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging { jniLibs { useLegacyPackaging = true } }
}

dependencies {
    implementation("org.videolan.android:libvlc-all:3.6.0")
}
