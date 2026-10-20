// 顶层构建脚本：只声明插件，不写具体配置。
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // Chaquopy：把 CPython 和 Python 内核打进 APK。
    // 17.1.0 支持 Python 3.10~3.15，要求 AGP 7.3~9.4、minSdk >= 24。
    id("com.chaquo.python") version "17.1.0" apply false
}
