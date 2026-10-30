plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// En GitHub, el número de compilación hace que cada APK nuevo se instale encima del anterior
val build = (System.getenv("VERSION_CODE") ?: "1").toInt()
val keystore = file("release.keystore")

android {
    namespace = "com.alasstor.myfitness"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.alasstor.myfitness"
        minSdk = 26
        targetSdk = 34
        versionCode = build
        versionName = "1.0.$build"
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    signingConfigs {
        create("release") {
            if (keystore.exists()) {
                storeFile = keystore
                storePassword = System.getenv("KEYSTORE_PASSWORD")
                keyAlias = "myfitness"
                keyPassword = System.getenv("KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystore.exists()) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
    }
    sourceSets {
        getByName("main") {
            // La misma aplicación que en el PC: app.py, forma.py, fitvo2.py e index.html
            srcDir("../../garmin-local")
        }
    }
}
