plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing is driven entirely by environment variables (GitHub Secrets in CI).
// Nothing secret is ever committed. See GITHUB_ACTIONS.md.
val keystorePath: String? = System.getenv("LUMI_KEYSTORE_FILE")
val hasReleaseSigning = !keystorePath.isNullOrBlank() && file(keystorePath!!).exists()

android {
    namespace = "com.lumi.assistant"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.lumi.assistant"
        minSdk = 26
        targetSdk = 34
        versionCode = (System.getenv("LUMI_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("LUMI_VERSION_NAME") ?: "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        // Debug builds are signed with one key that lives in the repository. Without this,
        // every CI runner generates its own debug key, and a build from one run cannot be
        // installed over a build from another (INSTALL_FAILED_UPDATE_INCOMPATIBLE).
        // This key protects nothing: release builds use the secret key below.
        getByName("debug") {
            storeFile = file("debug.keystore")
            storeType = "pkcs12"
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
        if (hasReleaseSigning) {
            create("release") {
                storeFile = file(keystorePath!!)
                storePassword = System.getenv("LUMI_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("LUMI_KEY_ALIAS")
                keyPassword = System.getenv("LUMI_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = false
            if (hasReleaseSigning) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
        freeCompilerArgs = freeCompilerArgs + listOf(
            "-opt-in=androidx.compose.material3.ExperimentalMaterial3Api",
            "-opt-in=androidx.compose.foundation.layout.ExperimentalLayoutApi",
        )
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    lint {
        // Lint runs as its own CI step after the APK is built. It reports, but does not block the build.
        abortOnError = false
        checkReleaseBuilds = false
        textReport = true
        textOutput = file("build/reports/lint-results.txt")
        htmlReport = true
        // Dependency versions are pinned on purpose for reproducible CI builds.
        disable += setOf("GradleDependency", "NewerVersionAvailable", "AndroidGradlePluginVersion", "OldTargetApi", "ExpiredTargetSdkVersion")
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

dependencies {
    implementation(project(":core"))

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.5")

    implementation(platform("androidx.compose:compose-bom:2024.09.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")

    implementation("androidx.work:work-runtime:2.9.1")
    implementation("com.google.android.gms:play-services-location:21.3.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}

// After an APK is assembled, report its package, SDK levels, ABIs and signing certificate, and
// fail the build if they are not what an installable Lumi build must have. In CI this runs
// automatically; locally run `./gradlew :app:verifyDebugApk` (needs bash and ANDROID_HOME).
val pinnedDebugCert: String = rootProject.file("ci/debug-cert.sha256").let { if (it.exists()) it.readText().trim() else "" }
listOf(
    Triple("Debug", "com.lumi.assistant.debug", pinnedDebugCert),
    Triple("Release", "com.lumi.assistant", ""),
).forEach { (variant, expectedPackage, expectedCert) ->
    val verify = tasks.register<Exec>("verify${variant}Apk") {
        group = "verification"
        description = "Reports and validates the metadata and signature of the $variant APK."
        workingDir = rootDir
        commandLine(
            "bash", ".github/scripts/verify-apk.sh", "verify",
            "app/build/outputs/apk/${variant.lowercase()}", expectedPackage, "26", "34", expectedCert,
        )
    }
    if (System.getenv("CI") == "true") {
        tasks.matching { it.name == "assemble$variant" }.configureEach { finalizedBy(verify) }
    }
}
