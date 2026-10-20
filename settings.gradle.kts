// ===========================================================================
// v0.3 根目录的 Gradle 配置。
//
// 作用：让 Android Studio 可以直接「Open」v0.3 这个目录，
// 一个窗口里同时看到 backend/(Python 内核) 和 android/(App 壳子)。
//
// 真正的 Android 模块是 android/app，这里只是把它 include 进来。
//
// 注意：android/ 目录下也有一套同样的 settings/build 脚本。
//   - 打开 v0.3  → 用这份
//   - 打开 android → 用 android/ 那份
// 两边的「插件版本」要保持一致，改一处记得改另一处。
// ===========================================================================

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
        // Chaquopy 自己的仓库
        maven("https://chaquo.com/maven")
    }
}

rootProject.name = "MusicVideoHelper"

include(":app")
// 模块目录不在根下，显式指过去。
project(":app").projectDir = file("android/app")
