# 在 Android Studio 里跑起来（从零到看到画面）

这份是按你本机实际情况写的（Windows + 已有 Android SDK 35 / JDK 17 / Python 3.10.11）。
**这套配置已经在本机用命令行完整跑通过一次**：`BUILD SUCCESSFUL`，
产物 `android/app/build/outputs/apk/debug/app-debug.apk`（debug 包 159MB，双 ABI 正常）。

---

## 第 0 步：装两个东西（只需要一次）

### 1）Android Studio

本机 `D:\Android stdio` 是个**空目录**，SDK 在 `C:\Users\lenovo\AppData\Local\Android\Sdk`
（platform-35 / build-tools 35 已就绪）。

去 <https://developer.android.com/studio> 下载安装即可，安装时选「Standard」。
装完不用单独装 SDK —— 它会自动认到上面那个目录（`local.properties` 里已经写好路径了）。

### 2）Python 3.10（64 位）——**已经装好了**

```
C:\Users\lenovo\AppData\Local\Programs\Python\Python310\python.exe   (3.10.11)
```

Chaquopy 生成 APK 时需要用**同版本**的 CPython 来准备依赖，所以必须是 3.10。

为什么不用本机已有的 3.13：Chaquopy 的原生包仓库里，`scipy` 和 `soundfile`
**只出到 cp310 的轮子**，3.13 会在 `pip install` 阶段直接报 "no matching distribution"。

> 如果以后换机器，重装命令：
> `winget install -e --id Python.Python.3.10 --scope user`
> 如果 Chaquopy 还是找不到它，就在 `app/build.gradle.kts` 里取消 `buildPython(...)`
> 那行注释，写死上面那个路径。

---

## 第 1 步：用 Android Studio 打开工程

两种都行，**推荐第二种**：

| 方式 | 操作 | 说明 |
|---|---|---|
| A | `File > Open` → 选 `D:\project\music_video_helper\v0.3` | 用根目录的 `settings.gradle.kts`，能同时看到 `backend/` 和 `android/` |
| B | `File > Open` → 选 `D:\project\music_video_helper\v0.3\android` | 只当 Android 工程，加载更快，Python 代码在旁边目录 |

打开后右下角会自己开始 **Gradle Sync**。

> 注意：这个目录里原本有个 PyCharm 的 `.idea`，Android Studio 打开后会**覆盖**它。
> 想让 PyCharm 的配置留着，就用方式 B。

---

## 第 2 步：等 Sync 通过（首次 5~15 分钟）

第一次要下 AGP 8.7.3、Kotlin 2.0.21、Chaquopy 17.1.0、FFmpegKit。
**如果卡在 "Downloading ..." 或者报 `Connection timed out`**，是网络问题，按下面配代理：

`File > Settings > Appearance & Behavior > System Settings > HTTP Proxy`
→ 选 `Manual proxy configuration` → 填你本机的代理地址和端口 → `Check connection` 填
`https://dl.google.com` 点测试 → OK → 重新 Sync。

同步成功的标志：底部状态栏不再转圈，`Build` 窗口最后一行是 `BUILD SUCCESSFUL`，
左侧 `app` 模块的 `java / python / res` 都能展开。

### 如果卡在给 Python 装依赖（`installDebugPythonRequirements`）

这一步会从 `chaquo.com` 下载 numpy / scipy / soundfile 的 Android 轮子，
**这个站国内经常连不上或者慢到 30KB/s**。工程里已经准备好了离线方案：

`android/wheels/` 目录里放的就是这些轮子（已经下好了）。
`app/build.gradle.kts` 会检测这个目录，非空就自动加 `--no-index --find-links`，
完全不联网。所以你只要别删这个目录，构建就不会卡在下载上。

> 如果 `wheels/` 空了（比如换了机器），可以让它自己联网下，
> 或者手动补齐：把需要的 `.whl` 从 <https://chaquo.com/pypi-13.1/> 拷进来即可。
> 需要哪几个文件名，看 `app/build.gradle.kts` 里 `install(...)` 的版本号。

---

## 第 3 步：准备一台设备

### 真机（推荐，快得多）

1. 手机开 **开发者选项**（设置 → 关于手机 → 连点「版本号」7 次）
2. 开发者选项里打开 **USB 调试**
3. 数据线连电脑，手机上点「允许 USB 调试」
4. 电脑上确认能看到设备：

```powershell
C:\Users\lenovo\AppData\Local\Android\Sdk\platform-tools\adb.exe devices
# 期望：xxxxxxxx    device
```

### 模拟器（也能用，但很慢）

`Device Manager > Create Device`，**镜像必须选 x86_64 或 arm64**（API 24 以上）。
不要选 armeabi-v7a / x86：`abiFilters` 里没有它们，装上去会闪退。

---

## 第 4 步：运行

顶部工具栏：设备下拉框选你的手机 → 点绿色 ▶ `Run 'app'`（或 `Shift + F10`）。

首次会自动做这些事（`build.gradle.kts` 里的 `syncPythonKernel` 任务）：

```
backend/config.py          ┐
backend/pipeline.py        │
backend/audio_processor.py ├─► 复制到 android/app/src/main/python/
backend/video_processor.py │   （桌面壳子 app.py / celery_app.py / task.py 不进包）
backend/mobile_api.py      ┘
```

然后 Chaquopy 把 CPython 3.10 + numpy/scipy/soundfile/soxr 打进 APK。
产物在 `android/app/build/outputs/apk/debug/app-debug.apk`。

---

## 第 5 步：在 App 里操作

界面就四个东西：两个「选择」按钮、两个音量滑杆、一个「开始」。

1. 点「选择视频」→ 相册里挑一段手元视频（mp4/mov 都行）
2. 点「选择音乐」→ 挑一个音频文件（mp3/wav/m4a/flac）
3. 调两个滑杆：`原声` 控制视频原声大小，`对齐音乐` 控制贴上去的音乐大小
4. 点「开始」

**第一次点「开始」会先等十几秒到几十秒**：Chaquopy 在解压 CPython 运行时和 numpy/scipy。
状态栏会显示「正在初始化运行环境…」，这是正常的，别退出去。

之后状态栏会依次显示「提取音频 → 特征提取 → 互相关对齐 → 混音 → 出片」这类进度。
跑完出现 ▶ 播放 和 分享 两个按钮，成片在 App 私有目录里。

---

## 第 6 步：出问题看哪里

Android Studio 底部的 **Logcat**，过滤框输入：

```
tag:SyncStage | python.stdout | python.stderr
```

- `SyncStage` —— Kotlin 侧的日志（Python 启动耗时、bootstrap 结果、异常堆栈）
- `python.stdout` / `python.stderr` —— Python 内核的 print 和 traceback

Python 里抛的异常会原样出现在 `python.stderr` 里，**这是排查的核心**。

### 常见报错对照

| 现象 | 原因 / 处理 |
|---|---|
| Sync 报 `Could not find com.chaquo.python:gradle:17.1.0` | `settings.gradle.kts` 的 `pluginManagement.repositories` 里缺 `mavenCentral()`，或网络没通 |
| `Python 3.10 not found` / `buildPython` 报错 | 第 0 步没装 3.10，或装完没重启 Android Studio。也可以在 `app/build.gradle.kts` 里取消 `buildPython(...)` 注释写死路径 |
| `pip` 报 `no matching distribution found for scipy==1.8.1` | 网络问题（chaquo.com 拉不到），配代理后 `Build > Clean Project` 再重试 |
| 装上去点开就闪退 | 大概率 ABI 不匹配：模拟器用了 x86/armeabi 镜像，换个 x86_64 或 arm64 的 |
| 「初始化失败：…」 | 看 `python.stderr`。多为数据目录不可写或 `android_entry.py` 没同步进包 |
| 「处理失败：尚未注入原生 FFmpeg 后端」 | `bootstrap()` 没执行成功，通常是 `FfmpegBackend` 里 ffmpeg-kit 没加载起来，看 `SyncStage` 标签 |
| 处理到一半 App 被系统杀掉 | 内存不够。用 `adb logcat | findstr lowmemorykiller` 确认；换成更短的视频（3 分钟内）先验证链路 |
| 进度一直 0% 不动 | Python 在跑但没触发回调，看 `python.stdout` 有没有停在某一步 |

---

## 附一：已经踩过并修好的坑（你不用再踩）

这几条都是本机真实构建时暴露出来的，**代码里已经改完**：

| 报错 | 原因 | 处理 |
|---|---|---|
| `Circular dependency between :app:syncPythonKernel` | 同步任务名里含 "Python"，被自己的名字匹配到，变成依赖自己 | 加了 `it.name != "syncPythonKernel"` 排除 |
| `Could not find com.arthenica:ffmpeg-kit-full-gpl:6.0-2` | FFmpegKit 归档后把它从 Maven Central 撤了 | 换成维护版 `dev.ffmpegkit-maintained:ffmpeg-kit-full:8.1.9`（包名不变，代码不用改） |
| `APK is not compatible with 16 KB devices` | 原版 ffmpeg-kit 的 `libavcodec.so` 等 6 个库只有 4KB 对齐 | 同一条：换成维护版后全部 16KB 对齐；用 `android/check_16kb.py` 复核 |
| `Argument type mismatch: actual type is 'kotlin.Any'` | JSON 兜底分支一边是 `JSONObject` 一边是 `String` | 统一 `.toString()` |
| `Suspend function 'copyToCache' should be called only from a coroutine` | 文件选择回调不是协程 | 包进 `lifecycleScope.launch` |
| `No matching distribution found for numpy/scipy` | ① 版本号在 chaquo 仓库不存在 ② 国内连不上 chaquo | 固定到真实存在的 cp310 版本 + `android/wheels` 离线缓存 |

---

## 附二：APK 165MB 太大了怎么办

debug 包大是正常的：CPython 3.10 运行时 + numpy/scipy/openblas + FFmpeg full，
**而且是 arm64-v8a 和 x86_64 两套**。

- 只在真机（几乎所有手机都是 arm64）上跑 → 把 `app/build.gradle.kts` 里
  `abiFilters += listOf("arm64-v8a", "x86_64")` 的 `"x86_64"` 删掉，体积直接少一半。
  代价是 x86_64 模拟器跑不了（arm64 系统镜像可以，但 Windows 上很慢）。
- 想更小就得换 ffmpeg 包：`ffmpeg-kit-full` → `ffmpeg-kit-audio`（只留音频，
  但**必须带 ffprobe**，`min` 版没有 ffprobe，会 probe 失败）。
- release 包开 `isMinifyEnabled = true` 还能再压一点（对 159MB 帮助有限，
  体积大头是 .so，不是代码）。

---

## 附三：不用 Android Studio，纯命令行也能测

```powershell
# 1) 编译 debug 包
cd D:\project\music_video_helper\v0.3
.\gradlew.bat :app:assembleDebug

# 2) 装到手机
C:\Users\lenovo\AppData\Local\Android\Sdk\platform-tools\adb.exe install -r `
  android\app\build\outputs\apk\debug\app-debug.apk

# 3) 启动 + 盯日志
C:\Users\lenovo\AppData\Local\Android\Sdk\platform-tools\adb.exe shell am start -n com.syncstage.app/.MainActivity
C:\Users\lenovo\AppData\Local\Android\Sdk\platform-tools\adb.exe logcat -s SyncStage python.stdout python.stderr
```

> 命令行跑 Gradle 也可能撞上代理问题。临时加参数即可（把端口换成你自己的）：
> `.\gradlew.bat :app:assembleDebug -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7890`
