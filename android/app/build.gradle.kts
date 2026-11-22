plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// GitHub Actions passes -PappVersion=1.4.0 -PappVersionCode=<run number>
val appVersion = (project.findProperty("appVersion") as String?) ?: "dev"
val appVersionCode = (project.findProperty("appVersionCode") as String?)?.toIntOrNull() ?: 1
// Release signing key (from GitHub secrets). Without it the APK is signed with a throwaway key,
// which installs fine but can't update an earlier install.
val keystoreFile = System.getenv("KEYSTORE_FILE")?.let { file(it) }?.takeIf { it.exists() }

android {
    namespace = "com.jashton.comicscraper"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jashton.comicscraper"
        minSdk = 24
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersion
        ndk {
            abiFilters += listOf("arm64-v8a")          // every phone from the last ~8 years
        }
    }

    signingConfigs {
        create("release") {
            if (keystoreFile != null) {
                storeFile = keystoreFile
                storePassword = System.getenv("KEYSTORE_PASSWORD")
                keyAlias = System.getenv("KEY_ALIAS")
                keyPassword = System.getenv("KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystoreFile != null) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("requests")
            install("Pillow")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("../../app")        // the same Python files the Windows app uses
        }
    }
}
