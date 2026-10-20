plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------------------
// 把 backend/ 下的「平台无关内核」同步进 Chaquopy 的 Python 源码目录，
// 这样内核只维护一份，不用来回复制。
// 桌面壳子（app.py / celery_app.py / task.py）不会被打进 APK。
// ---------------------------------------------------------------------------
val kernelFiles = listOf(
    "config.py",
    "pipeline.py",
    "audio_processor.py",
    "video_processor.py",
    "mobile_api.py",
    "android_web.py",   // WebView 用的本地静态服务
)

// backend/ 的位置相对本模块固定（app -> android -> v0.3/backend），
// 这样无论 Gradle 根目录是 v0.3 还是 v0.3/android，路径都不会错。
val backendDir = layout.projectDirectory.dir("../../backend").asFile

// 可选的本地轮子缓存（android/wheels）。详见下面 pip 块里的说明。
val wheelhouse = layout.projectDirectory.dir("../../android/wheels").asFile
// 只有全部下完（下载脚本会写这个标记）才启用离线模式，
// 否则半套轮子会让 pip 报错。
val wheelhouseReady = File(wheelhouse, ".complete").isFile &&
    (wheelhouse.listFiles()?.count { it.extension == "whl" } ?: 0) >= 12

// 用 Copy 而不是 Sync：Sync 会删除目标目录里"没被复制"的文件，
// 那样会把 Android 专属的 android_entry.py 一起删掉。
val syncPythonKernel = tasks.register<Copy>("syncPythonKernel") {
    description = "把 backend 的 Python 内核同步到 Chaquopy 源码目录"
    doFirst {
        if (!backendDir.isDirectory) {
            throw GradleException(
                "找不到 Python 内核目录: ${backendDir.absolutePath}\n" +
                    "请确认 android/ 和 backend/ 是同一级的兄弟目录。"
            )
        }
    }
    from(backendDir) {
        include(kernelFiles)
    }
    into(layout.projectDirectory.dir("src/main/python"))
}

// ---------------------------------------------------------------------------
// 主界面的网页就在 src/main/assets/web/，**改这里就是改 App**，构建不会覆盖它。
//
// 这里曾经挂过一个自动同步任务（每次构建把 frontend/ 复制过来），
// 结果手改的内容会被悄悄冲掉，所以现在改成默认不同步：
//   * 想让 App 变样  -> 直接编辑 src/main/assets/web/cupnb.html，assembleDebug 即可；
//   * 想拿 frontend/ 那一份覆盖过来 -> 手动跑 ./gradlew :app:pullWebFromFrontend
//                                     （会覆盖 assets 里的同名文件，慎用）。
//
// frontend/cupnb.html 仍是桌面版 Flask 用的页面，两者互不影响。
// ---------------------------------------------------------------------------
val frontendDir = layout.projectDirectory.dir("../../frontend").asFile
val webAssetsDir = layout.projectDirectory.dir("src/main/assets/web").asFile

tasks.register<Copy>("pullWebFromFrontend") {
    description = "手动把 frontend/ 的网页覆盖到 assets/web（会冲掉手改内容）"
    group = "syncstage"
    onlyIf { frontendDir.isDirectory }
    from(frontendDir) {
        include("**/*.html", "**/*.css", "**/*.js")
        include("**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.svg", "**/*.webp", "**/*.gif")
        // native-bridge.js 是 Android 专属适配层，frontend 里不该有同名文件
        exclude("native-bridge.js")
    }
    into(webAssetsDir)
}

// 必须在 Chaquopy 收集 Python 源码之前完成同步。
// 注意：这里不再挂网页同步任务 —— 网页以 assets/web 为准，手改不会被覆盖。
tasks.named("preBuild") { dependsOn(syncPythonKernel) }

// Chaquopy 的 pip / 源码收集任务名字里都带 Python，
// 但要排除 syncPythonKernel 自己 —— 否则它会依赖自己，Gradle 报
// "Circular dependency between :app:syncPythonKernel"。
tasks.matching { it.name.contains("Python") && it.name != "syncPythonKernel" }
    .configureEach {
        dependsOn(syncPythonKernel)
    }

android {
    namespace = "com.syncstage.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.syncstage.app"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.3.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            // Python 3.12+ 只支持 64 位 ABI。
            // arm64-v8a 覆盖几乎所有真机，x86_64 用于模拟器。
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources {
            // FFmpegKit 与 Python 运行时都会带 libc++_shared.so
            pickFirsts += listOf("**/libc++_shared.so")
            excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
        }
    }
}

chaquopy {
    defaultConfig {
        // 为什么是 3.10 而不是 3.13：
        // Chaquopy 的原生包仓库 https://chaquo.com/pypi-13.1/ 里，
        // scipy 和 soundfile 只出到 cp310 的轮子（cp311+ 没有）。
        // 用 3.13 会在 pip 阶段直接报 "no matching distribution"。
        // 想用 3.13 就必须把 scipy / soundfile 从内核里去掉（见 README「已知限制」）。
        version = "3.10"

        // 构建机上需要同版本 CPython（3.10.x），Chaquopy 会自动探测。
        // 探测不到时取消下面注释并改成你的路径：
        // buildPython("C:/Users/<你>/AppData/Local/Programs/Python/Python310/python.exe")

        pip {
            // 下面这些版本都是 2026-10 在 chaquo.com/pypi-13.1 上
            // 真实存在的 cp310 轮子。scipy 没有 1.11+ 的 Android 轮子，
            // 只能停在 1.8.1；它兼容的 numpy 上限是 1.25，故配 1.23.3。
            install("numpy==1.23.3")
            install("scipy==1.8.1")
            install("soundfile==0.13.1")   // 自动带 chaquo-libsndfile
            install("soxr==0.3.6")

            // 离线模式：android/wheels/ 里已经放好了轮子，就完全跳过联网。
            // chaquo.com 在国内经常连不上/极慢，第一次先把轮子下好放进去，
            // 之后每次构建都是秒过。目录不完整就自动回到联网模式。
            if (wheelhouseReady) {
                options("--no-index")
                options("--find-links", wheelhouse.absolutePath)
            }
        }
    }

    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.activity:activity-ktx:1.9.3")

    // 原生 FFmpeg / FFprobe 后端。必须带 ffprobe，所以用 full。
    //
    // 这里用的是社区维护版（原 com.arthenica 已归档并从 Maven Central 撤包），
    // 关键差别：它用 -Wl,-z,max-page-size=16384 重新构建过，
    // 所有 .so 都是 16KB 页对齐的 —— 原版 libavcodec.so 等只有 4KB 对齐，
    // 在 Android 15+ 的 16KB 页设备上会直接加载失败。
    // 包名仍是 com.arthenica.ffmpegkit，Java/Kotlin 代码一行都不用改。
    //
    // 这里选 LGPL 的 full 变体（非 -gpl），对闭源 App 更稳妥；
    // 我们只用到 pcm_s16le / aac / amix / -c:v copy，LGPL 变体完全覆盖。
    // 需要 x264/x265 时再换成 ffmpeg-kit-full-gpl:8.1.9。
    implementation("dev.ffmpegkit-maintained:ffmpeg-kit-full:8.1.9")
}
