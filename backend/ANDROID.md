# Android 集成说明（SyncStage / music_video_helper）

这份文档说明如何把 `backend/` 里的 Python 内核跑进 Android App。
核心思想：**手机端只跑“平台无关内核”，不用 Flask / Celery / Redis /
系统 subprocess-ffmpeg**，那三样只是电脑端的开发壳子。

> **可编译的工程已经在 `../android/` 下建好了**（Chaquopy + FFmpegKit）。
> 构建步骤、目录说明、排错见 `../android/README.md`。
> 本文剩下部分讲的是内核这一侧的约定。

## 一、分层

- ~~桌面壳子（打包时**不要**带上）：`app.py`、`celery_app.py`、`task.py`、
  `requirements.txt` 里的 flask/celery/redis。~~
  **已于 2026-10-21 从工程中删除**，现在只有 App 一条链路。
- 平台无关内核（要打进 APK）：`config.py`、`pipeline.py`、
  `audio_processor.py`、`video_processor.py`、`mobile_api.py`、
  `android_web.py`、`requirements-android.txt`。

内核只 `import numpy/scipy/soundfile`（`soxr` 可选），不碰 Web 框架，
也不再依赖 `librosa`（特征提取已改为纯 numpy + scipy，见下）。
`video_processor.py` 里对 FFmpeg 的调用全部走 `MediaBackend` 抽象，
`_local_path()` 还会拒绝 `content://`（要求原生层先把文件落到 App 缓存）。

## 二、调用面（Kotlin 只对接 android_entry / mobile_api）

实际工程里 Kotlin 只认识 `android/app/src/main/python/android_entry.py`，
它由 Gradle 自动和内核一起打进 APK；`mobile_api` 是它下面的一层。

```python
# 1) App 启动时初始化一次（android_entry.bootstrap）
bootstrap(data_root, backend)
#   data_root: context.getFilesDir()/子目录，可持久读写
#   backend:   Chaquopy 传进来的 Java 对象，带两个方法——
#              backend.execute(args, timeout)
#              backend.probe(path, timeout) -> JSON 字符串
#   注意：Java 对象不是 Python callable，android_entry 负责包成 callable。

# 2) 用户选文件后，在后台线程跑（android_entry.start，返回 JSON 字符串）
start(
    video_path,                 # 本地绝对路径，不能是 content://
    music_path,                 # 外部音乐文件（手机端必需）
    original_volume=0.4,
    aligned_volume=0.8,
    progress_callback=cb,       # 带 onProgress(data) 方法的对象即可
) -> str                        # '{"status":"success","output_path":...}'

# 3) 直接用 mobile_api 也可以（返回 dict）
run_process(...)     -> dict
run_process_json(...) -> str
```

成功返回里有 `output_path`（`<data_root>/results/<task_id>/output.mp4`），
直接用文件路径播放/分享。失败返回结构化错误，不会抛异常到 Kotlin。

## 三、原生 FFmpeg 后端怎么接

用 `video_processor.CallbackBackend` 把 Kotlin 的两个回调包进来即可：

```python
from video_processor import CallbackBackend, configure_backend

def execute_fn(args, timeout):
    # args 是不含程序名的 ffmpeg 参数数组
    # 调 FFmpegKit/ffmpeg-mobile，同步等待；退出码非 0 时抛异常并带日志
    ...

def probe_fn(path, timeout):
    # 等价: ffprobe -v error -show_format -show_streams -of json <path>
    # 返回已解析好的 dict
    ...

configure_backend(CallbackBackend(execute_fn, probe_fn))
```

可用 `is_native_backend()` / `is_ready()` 判断是否已经注入成功，避免误用
桌面 `SubprocessBackend`（Android 上通常没有系统 ffmpeg）。
`run_process()` 默认还会再拦一次：没注入原生后端就直接返回结构化错误，
不会带着难懂的 ffmpeg 报错往下走。

## 四、对齐链路只喂 WAV（重要，降低原生依赖）

`pipeline.process_video` 会先用 ffmpeg 把“视频原声”和“外部音乐”都
`extract_audio_from_video(...)` 成 44.1kHz 单声道 WAV，之后解码只用
`soundfile`（libsndfile）读 WAV，不需要 mp3/m4a/flac 的解码器。

采样率转换 `audio_processor._resample()` 优先用可选的 `soxr`，
缺 soxr 时自动退回 `scipy.resample_poly`，所以设备上可以不打包 soxr。

## 五、打包注意事项（Chaquopy）

- **必带**：`numpy`、`scipy`、`soundfile`（及其 `libsndfile.so`）。
  这三个在 Android/ARM 都有可用轮子，打包友好。
- **已彻底移除 librosa → numba/llvmlite**：`_build_feature` 里的
  mel / 色度 / 起音 / STFT 现在都是纯 numpy + scipy 实现，
  不再有 Android 上最难搞的 numba 依赖。
- `noisereduce` 可选；不装则 `denoise_audio()` 直接透传原音频（不报错）。
- 存储目录用 App 私有可写路径；`content://` URI 必须在 Kotlin 层
  先 copy 到缓存再传绝对路径给 `run_process`。
- `run_process` 阻塞耗时，务必在后台线程/协程执行；`progress_callback`
  回到 UI 要切主线程。
- 只给歌名不选文件 → 直接返回错误（手机端无联网抓取）。
- 峰值内存：特征提取已改成**分块 STFT**，3 分钟素材约 120MB、
  5 分钟约 200MB（改之前 5 分钟要 1GB+，手机上必然 OOM）。
  Manifest 里已经开了 `android:largeHeap="true"`。

## 六、桌面 / 手机对照

| 能力        | 桌面壳子                | Android                 |
|-------------|-------------------------|-------------------------|
| 入口        | app.py + cupnb.html     | mobile_api.run_process  |
| 任务调度    | Celery + Redis          | Kotlin 线程/WorkManager |
| FFmpeg      | subprocess 系统 ffmpeg  | FFmpegKit 原生后端注入  |
| 前端        | Flask 同源托管 /        | 原生 UI 或 WebView      |
| 结果获取    | output_url (HTTP)       | output_path (文件)      |
