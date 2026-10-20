# SyncStage Android 打包工程

把 `backend/` 里的 Python 内核（音频对齐 + 混音）跑进 Android App。

技术选型：**Chaquopy**（在 APK 里嵌 CPython）+ **FFmpegKit**（原生 ffmpeg/ffprobe）。
手机端**不跑** Flask / Celery / Redis —— 那三样只是桌面开发壳子。

---

## 一、先看这里：两个前置条件

1. **构建机上要有 Python 3.10.x**
   Chaquopy 用它来准备依赖。
   为什么不是 3.13：Chaquopy 的原生包仓库 <https://chaquo.com/pypi-13.1/> 里，
   `scipy` 和 `soundfile` **只出到 cp310 的轮子**，3.13 会在 pip 阶段报
   "no matching distribution"。
   装好 3.10 后 Chaquopy 会自动探测；探测不到时，打开 `app/build.gradle.kts`，
   取消 `buildPython(...)` 那行注释并填你的路径。

2. **JDK 17+**，AGP 8.7.3 / Gradle 8.13 已在配置里固定（`gradlew` 也带了）。

> 详细的「Android Studio 从零到跑起来」见 **[ANDROID_STUDIO.md](ANDROID_STUDIO.md)**。

---

## 二、构建

```bash
# 方式一：在 v0.3 根目录构建（推荐，路径不用切）
cd D:/project/music_video_helper/v0.3
gradlew.bat :app:assembleDebug      # Windows
./gradlew :app:assembleDebug        # macOS / Linux

# 方式二：在 android/ 里构建
cd android
gradlew.bat assembleDebug
```

Android Studio 里 `File > Open` 选 **v0.3 根目录** 或 **android/ 目录** 都能跑，
详见 [ANDROID_STUDIO.md](ANDROID_STUDIO.md)。

产物：`android/app/build/outputs/apk/debug/app-debug.apk`

> 本机已实测通过：`BUILD SUCCESSFUL`，debug 包 **159MB**（arm64-v8a + x86_64 双 ABI，
> 内含 CPython 3.10 + numpy/scipy + ffmpeg full-gpl）。只在真机上跑的话，
> 把 `abiFilters` 里的 `"x86_64"` 去掉可以少一半体积。

### 为什么有两套 settings / build 脚本

`v0.3/` 和 `v0.3/android/` 各有一套 `settings.gradle.kts` + `build.gradle.kts` + `gradle.properties`：

- 打开 `v0.3/` → Gradle 用根目录那套，把 `android/app` 当 `:app` 模块
- 打开 `android/` → 用 `android/` 那套

模块本身只有一份（`android/app`），**改插件版本 / 仓库时两个顶层文件都要改**。
`app/build.gradle.kts` 里 `backend/` 的路径是相对模块目录写的（`../../backend`），
所以两种打开方式都对。两套都带 `gradlew` + `gradle-wrapper.jar`，命令行直接能用。

### 依赖来源（两个坑，已处理）

1. **FFmpegKit 原版已下架**：`com.arthenica:ffmpeg-kit-full-gpl` 归档后从 Maven Central 撤了。
   现在用的是社区维护版 `dev.ffmpegkit-maintained:ffmpeg-kit-full:8.1.9`（Maven Central 正常可拉）。
   包名仍是 `com.arthenica.ffmpegkit`，Java/Kotlin 代码一行都不用改。
   它用 `-Wl,-z,max-page-size=16384` 重编过，**所有 .so 都是 16KB 页对齐的** ——
   原版只有 4KB 对齐，在 Android 15+ 的 16KB 页设备上会直接加载失败。
   `settings.gradle.kts` 里保留的阿里云镜像是给其他依赖兜底的，别删。
2. **chaquo 的轮子国内几乎下不动**：`android/wheels/` 里放好了全部 27 个轮子
   （cp310，arm64-v8a + x86_64，含 numpy/scipy 的传递依赖 openblas/libcxx/libgfortran 等），
   构建时自动启用 `--no-index --find-links` 离线安装。该目录不进 Git（54MB），
   删了就会退回联网模式。

---

## 三、目录说明

```
android/
├── app/build.gradle.kts          # Chaquopy + FFmpegKit + 依赖版本
├── app/src/main/
│   ├── AndroidManifest.xml
│   ├── java/com/syncstage/app/
│   │   ├── MainActivity.kt       # UI：选文件 / 进度 / 播放分享
│   │   ├── PythonEngine.kt       # Chaquopy 封装 + 进度回调桥
│   │   └── FfmpegBackend.kt      # 原生 ffmpeg / ffprobe 后端
│   ├── python/
│   │   ├── android_entry.py      # Kotlin 唯一认识的入口（需要提交）
│   │   └── config.py ...         # 构建时从 ../../backend 自动同步（不要提交）
│   └── res/
└── gradle/wrapper/
```

### Python 内核怎么进 APK 的

`app/build.gradle.kts` 里有个 `syncPythonKernel` 任务，构建时把
`backend/` 下这 5 个文件复制进 `app/src/main/python/`：

```
config.py  pipeline.py  audio_processor.py  video_processor.py  mobile_api.py
```

桌面壳子（`app.py` / `celery_app.py` / `task.py`）**不会**被打进去。
`android_entry.py` 是 Android 专属的，手写维护。

---

## 四、调用链

```
MainActivity (主线程)
   └─ lifecycleScope(Dispatchers.Default)
        └─ PythonEngine.process(...)
             └─ Python: android_entry.start(video, music, ..., progressSink)
                  └─ mobile_api.run_process(...)
                       └─ pipeline.process_video(...)
                            ├─ video_processor  ──► FfmpegBackend.execute / probe
                            └─ audio_processor  ──► numpy / scipy / soundfile
```

进度回调：`PythonEngine.ProgressSink.onProgress(PyObject)` 被 Python 调用，
内部再 `runOnUiThread` 切回主线程更新进度条。

结果：Python 返回 **JSON 字符串**，Kotlin 用 `org.json.JSONObject` 解析。
成功时取 `output_path`，失败时取 `error`。

---

## 五、关键配置点

| 项目 | 位置 | 说明 |
|------|------|------|
| Python 版本 | `chaquopy.defaultConfig.version` | 3.10（scipy/soundfile 只有 cp310 轮子） |
| pip 依赖 | `chaquopy.defaultConfig.pip` | numpy 1.23.3 / scipy 1.8.1 / soundfile 0.13.1 / soxr 0.3.6 |
| ABI | `android.defaultConfig.ndk.abiFilters` | arm64-v8a + x86_64（Python 3.10+ 只有 64 位轮子） |
| 大内存 | `AndroidManifest android:largeHeap` | 音频特征计算需要 |
| 数据目录 | `PythonEngine.bootstrap` | `context.filesDir/syncstage` |

### 依赖版本怎么定

Chaquopy 不从 PyPI 装原生包，而是用它自己的仓库
<https://chaquo.com/pypi-13.1/>。**能不能用某个包，取决于那里有没有对应 `cp3XX` 的轮子**：

| 包 | 仓库里有轮子的 Python 版本 | 本工程用的版本 |
|---|---|---|
| numpy | 3.8 ~ 3.13（1.26.2 是最高） | 1.23.3 |
| scipy | **只到 3.10**（最高 1.8.1） | 1.8.1 |
| soundfile | **只到 3.10**（0.13.1） | 0.13.1 |
| soxr | 3.8 ~ 3.13 | 0.3.6 |
| cffi | soundfile 依赖，自动装 | 1.15.1 |

所以「Python 版本」这一项没有选择余地：要用 scipy + soundfile，就得是 3.10。

`noisereduce` **不要**打包：它可选，而且会连带拉入 matplotlib（体积大、手机上没意义）。
缺失时 `denoise_audio()` 直接透传原音频，不影响功能。

---

## 六、已知限制

- **Python 只能停在 3.10**：Chaquopy 仓库里 scipy / soundfile 没有 3.11+ 的轮子。
  （Chaquopy 的 `libpython3.10.so` 本身是 16KB 对齐的，所以 3.10 不影响 16KB 合规。）
  真要升到 3.13 就得改造内核：
  `soundfile` 换 `wave` 模块、`scipy.signal.correlate` 换 `np.fft`、
  `butter/sosfiltfilt` 换自实现。这是唯一的技术债。
- **只支持本地音乐文件**：手机端没有 `music_scraper`，只填歌名会被直接拒绝。
- **处理很吃资源**：3 分钟视频在手机上大约要几十秒到几分钟，且会占用
  100~200MB 内存。已在 `audio_processor` 里做了分块 STFT 来压低峰值内存。
- 首次启动要解压 CPython 和 numpy/scipy，会比较慢（`bootstrap` 在后台线程做）。

---

## 七、16KB 页对齐（Google Play 硬性要求）

面向 Android 15（API 35）+ 的应用必须支持 16KB 内存页，否则 16KB 页设备上
`dlopen` 会直接失败。**两层都要对齐**，缺一层都算不合格：

| 层 | 检查内容 | 谁负责 |
|----|---------|--------|
| ELF 层 | 每个 `.so` 的 `PT_LOAD` 段 `p_align >= 0x4000` 且 `p_offset ≡ p_vaddr (mod 0x4000)` | 编译方（`-Wl,-z,max-page-size=16384` 或 NDK r28+） |
| ZIP 层 | 未压缩存储的 `.so` 在 APK 内的数据偏移 `% 16384 == 0` | AGP 8.5.1+ 自动做 |

**当前状态：两层都过了。** 关键是换掉了 FFmpegKit —— 原版 `com.arthenica` 的
`libavcodec.so` 等 6 个库是 4KB 对齐的，换成维护版后全部 16KB 对齐；
Chaquopy 的 `libpython3.10.so` 等本来就是对齐的。

一条命令自检：

```bash
python android/check_16kb.py android/app/build/outputs/apk/debug/app-debug.apk
# 或直接用官方工具（build-tools 35+）：
zipalign -c -P 16 -v 4 app-debug.apk     # 结尾要打印 Verification successful
```

输出示例（全绿才算过）：

```
lib/*.so 数量: 38  压缩方式: {0} (0=STORED 不压缩)
ZIP 内未按 16KB 偏移: 无 ✅
ELF LOAD 段未 16KB 对齐: 无 ✅
```

> 注意：`zipalign -c -P 16` 后面还要跟一个 `<align>` 参数（通常是 `4`，
> 用于压缩条目），写成 `zipalign -c -P 16 apk` 会报 usage 错误。

---

## 八、主界面是网页（frontend/cupnb.html）

App 打开后就是一个 WebView，加载的是：

> **`android/app/src/main/assets/web/cupnb.html`** ← 要改界面就改这一个文件

改完直接 `./gradlew :app:assembleDebug` 就会生效。**构建不会覆盖它**。

`frontend/cupnb.html` 是桌面版 Flask 用的同一份页面，和 App 互不影响。
如果你在桌面版改好了、想把它搬进 App，才需要手动跑一次（会覆盖 assets 里的同名文件）：

```bash
./gradlew :app:pullWebFromFrontend
```

> 历史坑：以前构建时会自动把 `frontend/` 复制过来，导致手改 `assets/web/` 的内容
> 每次都被悄悄冲掉。现在这个同步改成手动任务了。

### 它是怎么跑起来的

```
页面  file:///android_asset/web/cupnb.html     ← 直接从 assets 读，不依赖 Python，秒开
   │
   ├─ JS bridge（window.SyncStage）
   │     ├─ startTask(JSON)   → 选好的文件路径交给 Python 内核
   │     └─ taskStatus(id)    → {state, progress, detail, output_url}
   │
   └─ <video src="http://127.0.0.1:<port>/results/…">
          │
          └─ Python 本地服务（127.0.0.1:随机端口）
                └─ /results/…   结果视频（支持 Range，可拖动进度条）
```

**页面和内核是解耦的**：以前页面由 Python 的 http 服务提供，内核慢或服务没起来，
界面就永远卡在「正在加载」。现在页面走 `file:///android_asset`，几十毫秒就出来；
Python 只在后台起服务，专门给 `<video>` 供流。

两个必须的开关（否则页面/视频会白）：
- `settings.allowFileAccess = true` —— Android 11+ 默认禁止 file:// 访问；
- `settings.mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW` ——
  file:// 页面加载 http 视频算混合内容，默认会被拦。

`output_url` 必须是**绝对地址** `http://127.0.0.1:<port>/results/<id>/output.mp4`；
写成 `/results/...` 会在 file:// 页面下解析成 `file:///results/...` 而打不开。

页面本来是给 Flask 写的，`assets/web/native-bridge.js` 在 `onPageStarted` /
`onPageFinished` 各注入一次（有 `__syncStageBridgeReady` 守卫），
把 `fetch('/api/upload')` 和 `fetch('/api/tasks/xxx')` 改接到 bridge 上，
所以 **HTML 一行都不用改**，桌面版还能继续连 Flask 用。

文件选择走 WebView 原生的 `<input type=file>` → `onShowFileChooser` → SAF；
Kotlin 在回调里顺手把内容复制到缓存（`copyToCache`），Python 只认本地路径。

页面里的「下载视频」是 `<a href="http://127.0.0.1:...">`，WebView 不会自己下载，
由 `setDownloadListener` 把 URL 还原成本地文件，复用「导出到影片/SyncStage」的逻辑。

### 换成别的页面

1. 把新页面放进 `src/main/assets/web/`（默认入口是 `cupnb.html`）；
2. 如果文件名不叫 `cupnb.html`，改 `MainActivity.PAGE_URL`
   （`file:///android_asset/web/你的文件名.html`）；
3. 页面里的接口约定保持不变即可（`/api/upload` 返回 `{task_id}`，
   `/api/tasks/<id>` 返回 `{state, progress, detail, output_url}`），
   或者自己在 `native-bridge.js` 里改映射；
4. `./gradlew :app:assembleDebug`。

> 服务只监听 `127.0.0.1`，端口随机，不对外网开放。
> `AndroidManifest` 里的 `usesCleartextTraffic` 和 `INTERNET` 权限就是为了它。

---

## 九、排错

| 现象 | 排查 |
|------|------|
| `buildPython` 找不到 | 装 Python **3.10**，或在 `build.gradle.kts` 里显式指定路径 |
| `Could not find com.arthenica:ffmpeg-kit-*` | 原版已撤包。依赖现在指向 `dev.ffmpegkit-maintained:ffmpeg-kit-full:8.1.9`，别改回 `com.arthenica` |
| 「not compatible with 16 KB devices」 | 跑 `python android/check_16kb.py <apk>` 定位是哪个 .so，见下面「16KB 页对齐」 |
| pip 报 "no matching wheel" | 去 chaquo.com/pypi-13.1 查实际可用版本（见上面那张表）；`android/wheels/` 里有本地缓存则走离线 |
| `Xxx is not callable because it implements no functional interfaces` | Chaquopy 给所有 Java 对象都挂了 `__call__`，但只有实现**函数式接口**的类才真能被调用。`ProgressSink` 已实现 `PythonProgress`（`fun interface`）；Python 侧 `_make_progress_callback` 也改成优先走 `onProgress()` 方法 |
| 启动后提示"尚未注入原生后端" | `bootstrap()` 没成功，看 logcat 里 `python.stderr` 的堆栈 |
| FFmpeg 执行失败 | logcat 搜 `FFmpeg 执行失败`，末尾 4000 字符是 ffmpeg 日志 |
| 读文件失败 | SAF 的 `content://` URI 必须先复制到缓存目录；见 `MainActivity.copyToCache` |
| 白屏 / 一直"正在加载界面" | 页面现在走 `file:///android_asset`，正常几十毫秒就消失。若一直停着，`adb logcat -s SyncStage` 会打出「开始加载页面」和失败原因（`onReceivedError`） |
| 页面正常但点"开始"提示内核还在启动 | 正常现象：首次启动 CPython + 加载 numpy/scipy 要十几秒。`adb logcat -s SyncStage python.stderr` 看 `bootstrap ->` 和「内核就绪，端口」 |
| 页面正常但结果视频黑屏 | `output_url` 必须是绝对地址 `http://127.0.0.1:<port>/results/…`；从 file:// 页面播 http 视频还需要 `mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW` |
| 页面加载出来但点"上传"没反应 | `onShowFileChooser` 没接住，看是不是被别的 WebView 覆盖；`accept` 类型只支持 `video/*` 和 `audio/*` |
| 选完文件仍提示"请先上传视频文件" | file:// 页面下 WebView 不一定把 `content://` URI 变成 `input.files`。`MainActivity.notifyFilePicked()` 会调用 `window.__syncStageSetFile()` 兜底注入一个假 File；`adb logcat -s SyncStage` 应能看到「已选文件 videoFile -> xxx.mp4」 |
| 点"提交任务"没进度条 | 先看 `adb logcat -s SyncStage`：应有 `startTask -> <id> payload=...`。没有说明页面 JS 报错了（bridge 已把页面 console 转发到 logcat，搜 `JS[`）；有但立刻失败，会打出 `任务 <id> -> FAILURE ...` |
| 页面 JS 报错看不到 | `WebChromeClient.onConsoleMessage` 已把所有 console 输出转发到 logcat（tag `SyncStage`，格式 `JS[文件:行号] 信息`） |
| 结果视频不能拖动 | 本地服务的 Range 实现有问题，跑 `python backend/tests/test_web_server.py` 自检 |
