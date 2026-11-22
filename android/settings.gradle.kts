// Android version of Comic Scraper. Built by GitHub Actions (.github/workflows/build.yml).
// The Python engine in ../app is the same code the Windows app runs (via Chaquopy).
pluginManagement {
    repositories {
        gradlePluginPortal()
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "ComicScraper"
include(":app")
