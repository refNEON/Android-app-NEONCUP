// 这份只在「Android Studio 直接打开 android/ 目录」时生效。
// 如果打开的是 v0.3 根目录，用的是 v0.3/settings.gradle.kts。
// 两处的插件版本要保持一致。
pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        // FFmpegKit（arthenica）归档后已把产物从 Maven Central 撤掉，
        // 阿里云镜像还留着缓存，否则 com.arthenica:ffmpeg-kit-full-gpl 解析不到。
        // 顺带也让其它中央仓库依赖下载快很多。
        maven("https://maven.aliyun.com/repository/public")
        mavenCentral()
        // Chaquopy 的原生包仓库（numpy/scipy/soundfile 的 Android 轮子）
        maven("https://chaquo.com/maven")
    }
}

rootProject.name = "SyncStage"
include(":app")
