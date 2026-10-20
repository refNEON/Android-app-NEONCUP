# mobile_api.py
"""
Android / Chaquopy 侧的稳定调用面（平台无关内核的唯一入口）。

设计意图
--------
桌面端用的是 Flask + Celery + Redis + subprocess-ffmpeg 的“开发壳子”；
打包进 Android 时**不需要**也**不应该**依赖那三件套。手机端真正的流程是：

    1) App 启动时调用 initialize(...)
       - 指定 App 私有可写目录（内部缓存/结果目录）
       - 注入原生 FFmpeg 后端（FFmpegKit / ffmpeg-mobile，通过
         CallbackBackend 把两个 Kotlin 回调包进来）
    2) 用户选好视频 + 外部音乐后，在**后台线程**里调用 run_process(...)
       - 通过 progress_callback 把进度回抛给 Kotlin，再更新 UI
    3) 拿到返回的 output_path，直接用文件路径播放/分享

本模块只 import 平台无关内核（config / pipeline / video_processor），
不 import flask / celery / redis，方便在设备上单独加载运行。

线程约定
--------
run_process() 是同步阻塞的，必须在非 UI 线程调用（Android 侧用
协程/线程池/WorkManager）。Chaquopy 回调进来的函数在哪个线程执行由
调用方保证。
"""
import json
import os
import uuid
import warnings

from config import Config
import video_processor
from pipeline import process_video

# 手机没有音乐下载模块（music_scraper），只支持“外部音乐文件”这条路。
SUPPORTED_VIDEO_EXT = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv",
}
SUPPORTED_AUDIO_EXT = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma",
}


def initialize(data_root, backend=None):
    """
    App 启动时调用一次。

    data_root:
        App 私有、可持久读写的目录（如 context.getFilesDir() 下的子目录）。
        内部会自动创建 temp / uploads / results / reference_audio。
    backend:
        原生 FFmpeg 后端，需实现 execute(args, timeout) / probe(path, timeout)。
        可以直接传实现了协议的对象，也可以传 (execute_fn, probe_fn) 元组，
        由 CallbackBackend 自动包装。
    """
    if not data_root:
        raise ValueError("data_root 不能为空")

    Config.configure_storage(data_root)

    if backend is not None:
        if isinstance(backend, (tuple, list)):
            if len(backend) != 2:
                raise ValueError("backend 元组需为 (execute_fn, probe_fn)")
            backend = video_processor.CallbackBackend(backend[0], backend[1])
        video_processor.configure_backend(backend)

    return {
        "status": "ok",
        "data_root": Config.DATA_DIR,
        "native_backend": video_processor.is_native_backend(),
    }


def is_ready():
    """
    返回 (ready: bool, message: str)。

    ready=False 通常意味着还没注入原生后端：此时会退回依赖系统 ffmpeg 的
    SubprocessBackend，在 Android 上一般不可用。
    """
    native = video_processor.is_native_backend()
    if native:
        return True, "原生后端已就绪"
    return False, "尚未注入原生 FFmpeg 后端，请先调用 initialize(..., backend=...)"


def _safe_callback(fn):
    """
    给回调套一层容错：进度上报失败不应该把整个任务搞崩。

    典型场景：Java 侧回调对象已经随 Activity 销毁，或者 onProgress 里
    做了切线程 / 更新 UI 的操作失败。这类异常只记录，不向上抛。
    """
    def wrapper(data):
        try:
            return fn(data)
        except Exception:  # noqa: BLE001 - 进度上报失败不影响主流程
            import traceback

            traceback.print_exc()
            return None

    return wrapper


def _make_progress_callback(callback):
    """
    把各种形式的回调统一成 Python callable。

    支持三种输入：
    - None：不需要进度；
    - 带有 onProgress(data) / on_progress(data) / onEvent(data) 方法的对象
      （Chaquopy 从 Kotlin 传进来的 Java 对象就属于这一类）；
    - Python callable（函数 / lambda / Chaquopy 的函数式接口代理）：直接使用。

    ⚠️ 顺序很重要：**必须先判断方法式，再判断 callable**。
    Chaquopy 会给所有 Java 对象都挂上 __call__，所以
    `callable(java_object)` 恒为 True；但该对象只有在实现了
    「函数式接口」（只有一个抽象方法的接口）时才真的能被调用，
    否则会在调用那一刻抛：
        "Xxx is not callable because it implements no functional interfaces"
    所以不能一上来就 `if callable(callback): return callback`。
    """
    if callback is None:
        return None

    # 1) 方法式回调：Java/Kotlin 对象（Chaquopy）
    for name in ("onProgress", "on_progress", "onEvent"):
        method = getattr(callback, name, None)

        if method is not None and callable(method):
            return _safe_callback(lambda data, _m=method: _m(data))

    # 2) 真正的 Python callable / 函数式接口代理
    if callable(callback):
        return _safe_callback(callback)

    warnings.warn(
        "无法识别的 progress_callback 类型，本次任务将没有进度回调",
        RuntimeWarning,
    )
    return None


def _check_ext(path, allowed, label):
    ext = os.path.splitext(path or "")[1].lower()
    if ext not in allowed:
        raise ValueError(f"{label}格式不支持: {ext or '(无扩展名)'}")


def run_process(
    video_path,
    reference_audio_path=None,
    song_name="",
    game_name=None,
    original_volume=None,
    aligned_volume=None,
    task_id=None,
    progress_callback=None,
    require_native_backend=True,
):
    """
    在后台线程里跑一次“对齐 + 混音 + 出片”。

    返回一个**一定可 JSON 序列化**的 dict：
        成功: {"status": "success", "task_id", "output_path", ...}
        失败: {"status": "error", "task_id", "error_type", "error"}

    注意：手机端必须提供 reference_audio_path（外部音乐文件）。
    只给 song_name 需要联网抓取，Android 侧不支持，会直接返回错误。

    require_native_backend:
        默认 True。手机上若未注入原生 FFmpeg 后端，会退回到依赖
        系统 ffmpeg 的 SubprocessBackend（Android 上通常没有），
        报错会非常难懂；这里提前拦掉，返回结构化错误。
        桌面联调时可传 False 跳过该检查。
    """
    task_id = task_id or uuid.uuid4().hex

    try:
        if require_native_backend and not video_processor.is_native_backend():
            raise RuntimeError(
                "尚未注入原生 FFmpeg 后端，请先调用 "
                "initialize(..., backend=...) 再执行任务"
            )

        # ---- 入参校验（尽量给出结构化、可回传给 UI 的错误）----
        if not video_path or not os.path.isfile(video_path):
            raise FileNotFoundError(f"视频不存在: {video_path}")

        _check_ext(video_path, SUPPORTED_VIDEO_EXT, "视频")

        has_reference = bool(reference_audio_path) and os.path.isfile(
            reference_audio_path
        )
        if not has_reference:
            if str(song_name).strip():
                raise ValueError(
                    "手机端不支持仅凭歌名抓取音乐，请选择外部音乐文件"
                )
            raise ValueError("缺少外部音乐文件")

        _check_ext(reference_audio_path, SUPPORTED_AUDIO_EXT, "音乐")

        # 音量：不传则用配置默认值。
        if original_volume is None:
            original_volume = Config.ORIGINAL_VOLUME
        if aligned_volume is None:
            aligned_volume = Config.ALIGNED_VOLUME

        result = process_video(
            video_path=video_path,
            song_name=song_name,
            game_name=game_name,
            reference_audio_path=reference_audio_path,
            original_volume=original_volume,
            aligned_volume=aligned_volume,
            task_id=task_id,
            progress_callback=_make_progress_callback(progress_callback),
        )

        # 统一状态，并保证可序列化。
        result = dict(result)
        result["status"] = "success"
        result["task_id"] = task_id
        return _json_safe(result)

    except Exception as exc:
        # 不吞栈，交给原生层记录；这里只回传简短可读信息。
        import traceback

        traceback.print_exc()
        return {
            "status": "error",
            "task_id": task_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def run_process_json(*args, **kwargs):
    """run_process 的 JSON 字符串版本，方便跨语言边界传输。"""
    return json.dumps(
        run_process(*args, **kwargs),
        ensure_ascii=False,
        default=str,
    )


def _json_safe(obj):
    """
    递归转成 JSON 可表达的结构：numpy 标量、bytes 等统一降级。

    用一次 json.dumps(default=str) 验证可序列化，避免把
    非可序列化对象带回 Kotlin 侧。
    """
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
        return obj
    except (TypeError, ValueError):
        # 极端情况：逐个转换。
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
