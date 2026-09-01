android {
  compileSdk = 35
  defaultConfig {
    minSdk = 26
    // ShugoCore embeds CPython (Chaquopy) and jros2 (Fast-DDS).
    buildConfig {
      manifest = "app/src/main/AndroidManifest.xml"
    }
    compileOptions {
      sourceCompatibility = JavaVersion.VERSION_17
      targetCompatibility = JavaVersion.VERSION_17
      isCoreLibraryDesugaringEnabled = true
    }
    packaging {
      resources {
        excludes += "/META-INF/LICENSE.md"
        excludes += "/META-INF/NOTICE.md"
      }
    }
  }
}

dependencies {
  // Core library desugaring for Java 17 features.
  coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")

  // Fast-DDS ROS 2 client for Android.
  implementation("us.ihmc:jros2-android:1.5.1")

  // Chaquopy embeds the Python runtime that runs ShugoCore.
  implementation("org.python:chaquopy:16.0.0")
}