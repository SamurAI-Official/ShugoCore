dependencyResolutionManagement {
  repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
  repositories {
    google()
    mavenCentral()
    maven { url = uri("https://robotlabfiles.ihmc.us/repository/") }
  }
}

dependencies {
  // Chaquopy for the CPython runtime (embeds ShugoCore).
  implementation("org.python:chaquopy:16.0.0")
}