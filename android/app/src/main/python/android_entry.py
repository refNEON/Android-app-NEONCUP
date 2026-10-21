# android_entry.py
"""
Kotlin 侧唯一需要认识的 Python 模块。

它只做一件事：把 Kotlin 传进来的 Java 后端对象
（带 execute / probe 两个方法）包装成 mobile_api 需要的 Python 后端，
然后初始化存储目录。

之所以要包一层：
- Chaquopy 传进来的 Java 对象不能当 Python 函数直接调用 —— 它虽然
  callable() 为 True（Chaquopy 给所有 Java 对象都挂了 __call__），
  但只有实现了「函数式接口」的类才真的能被调用，否则会抛
  "… is not callable because it implements no functional interfaces"。
  所以要把 backend.execute / backend.probe 方法包成 Python 闭包。
  进度回调同理，由 mobile_api._make_progress_callback 统一处理。
- 原生 probe 返回的是 ffprobe 的 JSON 字符串，
  这里统一 json.loads 成 video_processor 需要的 dict。
"""
import json

import android_web
import mobile_api

# 供 Kotlin 侧查询：确认 Python 内核确实被打包进来了。
KERNEL_VERSION = "0.3.0"

# 只启一次，重复调用直接返回同一个端口。
_web_server = None


def start_web_server(web_root, result_dir):
    """
    给 WebView 起一个只监听 127.0.0.1 的静态服务。

    返回端口号（int）。网页里 <video> 要能拖动进度条，
    所以 android_web 实现了 HTTP Range。
    """
    global _web_server
    if _web_server is None:
        _web_server = android_web.start(str(web_root), str(result_dir))
    return int(_web_server.port)


def bootstrap(data_root, backend):
    """
    App 启动时调用一次。

    backend:
        Chaquopy 传进来的 Java/Kotlin 对象，**不是** Python callable。
        它必须有两个方法：

            backend.execute(args, timeout) -> None
                args 为不含程序名的 ffmpeg 参数列表；失败时抛异常。
            backend.probe(path, timeout) -> str
                等价 ffprobe -show_format -show_streams -of json，
                返回 JSON **字符串**（Py 侧负责 json.loads）。
    """
    execute = getattr(backend, "execute", None)
    probe = getattr(backend, "probe", None)

    if execute is None or probe is None:
        raise TypeError(
            "backend 必须提供 execute() 与 probe() 两个方法，"
            f"实际收到: {type(backend).__name__}"
        )

    def execute_fn(args, timeout):
        execute(list(args), float(timeout))

    def probe_fn(path, timeout):
        raw = probe(str(path), float(timeout))

        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")

        if isinstance(raw, dict):
            # 某些原生实现可能直接给 dict，兼容处理。
            return raw

        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"原生 probe 返回的不是合法 JSON: {raw[:200]!r}"
            ) from exc

    info = mobile_api.initialize(
        data_root,
        backend=(execute_fn, probe_fn),
    )
    info["kernel_version"] = KERNEL_VERSION
    return info


def is_ready():
    """返回 {ready: bool, message: str}，Kotlin 启动后可先自检一次。"""
    ready, message = mobile_api.is_ready()
    return {"ready": bool(ready), "message": str(message)}


def start(
    video_path,
    music_path,
    original_volume=None,
    aligned_volume=None,
    progress_callback=None,
    manual_lag_ms=None,
):
    """
    在**后台线程**里跑一次对齐 + 混音 + 出片。

    manual_lag_ms:
        用户手填的「手动对齐延迟」，单位毫秒，字符串或数字都行。
        正数 = 音乐延后，负数 = 音乐提前。
        传空串 / None 表示不手动干预，走自动对齐。

    返回 JSON 字符串（跨语言边界最稳）：
        {"status": "success", "output_path": ..., ...}
        {"status": "error", "error": ..., "error_type": ...}
    """
    return mobile_api.run_process_json(
        video_path=str(video_path),
        reference_audio_path=str(music_path) if music_path else None,
        original_volume=original_volume,
        aligned_volume=aligned_volume,
        progress_callback=progress_callback,
        manual_lag_ms=manual_lag_ms,
    )
