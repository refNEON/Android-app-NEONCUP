import json
import math
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Protocol

class MediaProcessingError(RuntimeError):
    """可直接转换成 JSON 错误消息返回给前端。"""

class MediaBackend(Protocol):
    """
    Android 原生适配层需要实现的两个方法。

    execute:
        接收 FFmpeg 参数数组，不包含程序名。
        必须同步等待任务完成。
        失败时抛出异常，并携带原生 FFmpeg 日志。

    probe:
        返回与 ffprobe -show_format -show_streams
        对应的 Python dict。
    """

    def execute(self, args: list[str], timeout: float) -> None:
        ...

    def probe(self, path: str, timeout: float) -> dict:
        ...

class SubprocessBackend:
    """电脑端使用。Android 端应替换为原生桥接后端。"""

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    @staticmethod
    def _resolve(program):
        resolved = shutil.which(program)
        if resolved:
            return resolved

        raise MediaProcessingError(
            f"找不到程序: {program}。"
            "电脑端请安装 FFmpeg 并配置路径；"
            "Android 端请通过 configure_backend() 配置原生后端。"
        )

    def _run(self, program, args, timeout, capture_stdout=False):
        executable = self._resolve(program)

        # 错误日志写临时文件，避免长任务日志全部堆积在内存中。
        with tempfile.TemporaryFile(mode="w+b") as error_log:
            try:
                result = subprocess.run(
                    [executable, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=(
                        subprocess.PIPE
                        if capture_stdout
                        else subprocess.DEVNULL
                    ),
                    stderr=error_log,
                    timeout=timeout,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise MediaProcessingError(
                    f"媒体处理超时，限制为 {timeout:g} 秒"
                ) from exc
            except OSError as exc:
                raise MediaProcessingError(
                    f"无法启动媒体处理程序: {exc}"
                ) from exc

            if result.returncode != 0:
                error_log.seek(0, os.SEEK_END)
                length = error_log.tell()
                error_log.seek(max(0, length - 16000))
                detail = error_log.read().decode(
                    "utf-8", errors="replace"
                ).strip()

                raise MediaProcessingError(
                    f"媒体处理失败，退出码 {result.returncode}。\n"
                    f"{detail}"
                )

            return result.stdout if capture_stdout else None

    def execute(self, args, timeout=1800):
        self._run(self.ffmpeg_path, args, timeout)

    def probe(self, path, timeout=30):
        raw = self._run(
            self.ffprobe_path,
            [
                "-v", "error",
                "-show_format",
                "-show_streams",
                "-of", "json",
                path,
            ],
            timeout,
            capture_stdout=True,
        )

        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise MediaProcessingError(
                "无法解析媒体信息"
            ) from exc

        if not isinstance(result, dict):
            raise MediaProcessingError("媒体信息格式不正确")

        return result

_backend = SubprocessBackend()
_native_configured = False

def configure_backend(backend):
    """
    在 App 初始化时调用一次。

    backend 需要提供：
        execute(args, timeout)
        probe(path, timeout)

    Android 端可用 CallbackBackend(execute_fn, probe_fn) 直接包装
    Kotlin 通过 Chaquopy 传进来的两个函数，而不必自己继承类。
    """
    if not callable(getattr(backend, "execute", None)):
        raise TypeError("backend 必须实现 execute()")
    if not callable(getattr(backend, "probe", None)):
        raise TypeError("backend 必须实现 probe()")

    global _backend, _native_configured
    _backend = backend
    # 桌面 SubprocessBackend 之外的一律视为“已注入原生后端”。
    _native_configured = not isinstance(backend, SubprocessBackend)

def current_backend():
    """返回当前生效的后端，便于调试或判断是否忘配原生后端。"""
    return _backend

def is_native_backend():
    """
    True 表示已注入非桌面的原生后端。

    手机端在真正跑任务前应确认返回 True，否则会退回到
    依赖系统 ffmpeg 的 SubprocessBackend，在 Android 上通常不可用。
    """
    return _native_configured

class CallbackBackend:
    """
    把两个回调函数适配成 MediaBackend 协议，方便 Chaquopy 注入。

    用法（Kotlin 侧提供两个可调用对象）：
        execute_fn(args, timeout) -> None   失败抛异常，携带 FFmpeg 日志
        probe_fn(path, timeout)   -> dict    等价 ffprobe -show_format -show_streams -of json
        configure_backend(CallbackBackend(execute_fn, probe_fn))
    """

    def __init__(self, execute_fn, probe_fn):
        if not callable(execute_fn):
            raise TypeError("execute_fn 必须可调用")
        if not callable(probe_fn):
            raise TypeError("probe_fn 必须可调用")

        self._execute = execute_fn
        self._probe = probe_fn

    def execute(self, args, timeout=1800):
        # 原生实现须同步等待完成，失败抛异常。
        return self._execute(list(args), timeout)

    def probe(self, path, timeout=30):
        result = self._probe(path, timeout)
        if not isinstance(result, dict):
            raise MediaProcessingError("原生 probe() 必须返回 dict")
        return result

def _local_path(path):
    path = os.fsdecode(os.fspath(path))

    if path.startswith("content://"):
        raise ValueError(
            "content:// URI 需要先由 Android 层复制到 App 缓存目录，"
            "再传入本地文件路径"
        )

    return os.path.abspath(path)

def _require_file(path, label):
    path = _local_path(path)

    if not os.path.isfile(path):
        raise FileNotFoundError(f"{label}不存在: {path}")

    return path

def _safe_float(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default

def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default

def _positive_number(value, name):
    result = _safe_float(value)

    if result is None or result <= 0:
        raise ValueError(f"{name} 必须是正数")

    return result

def _validate_gain(value, name):
    result = _safe_float(value)

    if result is None or result < 0:
        raise ValueError(f"{name} 必须是非负有限数")

    return result

def _validate_sample_rate(value):
    result = _positive_number(value, "audio_sample_rate")

    if not result.is_integer():
        raise ValueError("audio_sample_rate 必须是整数")

    return int(result)

def _validate_bitrate(value):
    """
    接受 192k、256k、192000 等形式。
    使用参数数组执行，不经过 shell。
    """
    text = str(value).strip().lower()
    numeric = text[:-1] if text.endswith("k") else text

    if not numeric.isdigit() or int(numeric) <= 0:
        raise ValueError("audio_bitrate 应为 192k 等正整数码率")

    return text

def _check_output_path(output_path, input_paths):
    output_real = os.path.normcase(os.path.realpath(output_path))

    for source in input_paths:
        source_real = os.path.normcase(os.path.realpath(source))

        if output_real == source_real:
            raise ValueError("输出路径不能覆盖输入文件")

        if os.path.exists(output_path):
            if os.path.samefile(output_path, source):
                raise ValueError("输出路径不能覆盖输入文件")

@contextmanager
def _temporary_output(output_path, input_paths, suffix):
    output_path = _local_path(output_path)
    _check_output_path(output_path, input_paths)

    parent = os.path.dirname(output_path)
    os.makedirs(parent, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=".media_",
        suffix=suffix,
        dir=parent,
    )
    os.close(fd)

    try:
        yield temp_path

        if not os.path.isfile(temp_path):
            raise MediaProcessingError("处理结束后没有生成输出文件")

        if os.path.getsize(temp_path) == 0:
            raise MediaProcessingError("生成的输出文件为空")

        os.replace(temp_path, output_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def _probe(path, timeout=30):
    result = _backend.probe(path, timeout)

    if not isinstance(result, dict):
        raise MediaProcessingError("后端 probe() 必须返回 dict")

    return result

def _select_streams(probe):
    video_stream = None
    audio_stream = None

    for stream in probe.get("streams", []):
        stream_type = stream.get("codec_type")

        if stream_type == "video" and video_stream is None:
            # 排除音乐文件封面等附加图片流。
            attached = stream.get("disposition", {}).get(
                "attached_pic", 0
            )
            if not attached:
                video_stream = stream

        elif stream_type == "audio" and audio_stream is None:
            audio_stream = stream

    return video_stream, audio_stream

def _stream_index(stream):
    index = _safe_int(stream.get("index"))

    if index is None or index < 0:
        raise MediaProcessingError("媒体流缺少有效 index")

    return index

def _video_duration(video_stream, format_info):
    # 优先使用视频流时长，避免较长音轨影响输出时长。
    duration = _safe_float(video_stream.get("duration"))

    if duration is not None and duration > 0:
        return duration

    duration_ts = _safe_float(video_stream.get("duration_ts"))
    time_base = video_stream.get("time_base")

    if duration_ts is not None and time_base:
        try:
            numerator, denominator = time_base.split("/")
            duration = (
                duration_ts * float(numerator) / float(denominator)
            )
            if math.isfinite(duration) and duration > 0:
                return duration
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    duration = _safe_float(format_info.get("duration"))

    if duration is not None and duration > 0:
        return duration

    raise MediaProcessingError(
        "无法确定视频时长，不能可靠地补齐和裁剪混音"
    )

def analyze_video(video_path: str) -> dict:
    video_path = _require_file(video_path, "视频")
    probe = _probe(video_path)

    format_info = probe.get("format") or {}
    video_stream, audio_stream = _select_streams(probe)

    result = {
        "path": video_path,
        "duration": _safe_float(format_info.get("duration")),
        "size": _safe_int(format_info.get("size")),
        "bit_rate": _safe_int(format_info.get("bit_rate")),
        "video_stream": None,
        "audio_stream": None,
    }

    if video_stream is not None:
        result["video_stream"] = {
            "codec_name": video_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "pix_fmt": video_stream.get("pix_fmt"),
            "r_frame_rate": video_stream.get("r_frame_rate"),
            "avg_frame_rate": video_stream.get("avg_frame_rate"),
            "duration": _safe_float(video_stream.get("duration")),
            "index": _stream_index(video_stream),
        }

    if audio_stream is not None:
        result["audio_stream"] = {
            "codec_name": audio_stream.get("codec_name"),
            "sample_rate": audio_stream.get("sample_rate"),
            "channels": audio_stream.get("channels"),
            "channel_layout": audio_stream.get("channel_layout"),
            "duration": _safe_float(audio_stream.get("duration")),
            "index": _stream_index(audio_stream),
        }

    return result

def extract_audio_from_video(
    video_path: str,
    output_audio_path: str,
    audio_sample_rate: int = 44100,
    *,
    timeout: float = 1800,
) -> str:
    video_path = _require_file(video_path, "视频")
    output_audio_path = _local_path(output_audio_path)

    sample_rate = _validate_sample_rate(audio_sample_rate)
    timeout = _positive_number(timeout, "timeout")

    probe = _probe(video_path)
    _, audio_stream = _select_streams(probe)

    if audio_stream is None:
        raise MediaProcessingError("视频没有可提取的音轨")

    audio_index = _stream_index(audio_stream)

    with _temporary_output(
        output_audio_path,
        [video_path],
        ".wav",
    ) as temp_path:
        _backend.execute(
            [
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i", video_path,
                "-map", f"0:{audio_index}",
                "-vn",
                "-c:a", "pcm_s16le",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-f", "wav",
                temp_path,
            ],
            timeout,
        )

    return output_audio_path

def mix_audio_with_video(
    video_path: str,
    aligned_audio_path: str,
    output_video_path: str,
    original_volume: float = 0.5,
    aligned_volume: float = 1.0,
    audio_bitrate: str = "192k",
    *,
    audio_sample_rate: int = 48000,
    limiter: bool = True,
    enable_loudness_norm: bool = True,
    loudnorm_target_lufs: float = -16.0,
    loudnorm_lra: float = 11.0,
    loudnorm_true_peak: float = -1.5,
    master_loudnorm: bool = False,
    master_target_lufs: float = -14.0,
    peak_ceiling: float = 0.95,
    timeout: float = 1800,
) -> str:
    """
    复制原视频画面，将原音轨与对齐音频混合后输出 MP4。

    original_volume / aligned_volume:
        线性振幅倍数，1.0 为原音量，0.5 约为 -6 dB。
        在启用响度归一后，它们是“相对配比”，不再是绝对音量。

    enable_loudness_norm:
        True 时对原声与对齐音乐两条支路分别做 EBU R128 响度归一
        （loudnorm，目标 loudnorm_target_lufs / true-peak），
        两路先对齐到同一响度基准再按音量配比混合。
        解决“手元声太小 / 音乐盖过原声”这类失衡，并降低相互叠加导致的削波。
        代价：quiet 的手元声会被抬高，噪底也会随之升高，可按需关闭。

    master_loudnorm:
        True 时对混好的整条音轨再做一次总响度归一（母带化），
        输出整体响度更一致。单程 dynamic loudnorm 会轻微改变动态。

    peak_ceiling:
        混音后限制器（alimiter）的线性峰值上限，用于兜底防爆音/削波。
        响度归一后正常配比下限制器几乎不介入，失真可忽略。

    limiter:
        启用峰值限幅。不代替响度匹配；极端增益仍可能造成明显动态压缩。

    Android:
        需通过 configure_backend() 配置原生执行后端。
    """
    video_path = _require_file(video_path, "视频")
    aligned_audio_path = _require_file(
        aligned_audio_path, "对齐音频"
    )
    output_video_path = _local_path(output_video_path)

    if os.path.splitext(output_video_path)[1].lower() != ".mp4":
        raise ValueError("输出视频路径必须以 .mp4 结尾")

    original_volume = _validate_gain(
        original_volume, "original_volume"
    )
    aligned_volume = _validate_gain(
        aligned_volume, "aligned_volume"
    )
    sample_rate = _validate_sample_rate(audio_sample_rate)
    bitrate = _validate_bitrate(audio_bitrate)
    timeout = _positive_number(timeout, "timeout")

    peak_ceiling = _safe_float(peak_ceiling)
    if peak_ceiling is None or not 0 < peak_ceiling <= 1:
        raise ValueError("peak_ceiling 必须在 (0, 1] 之间")

    video_probe = _probe(video_path)
    video_stream, original_stream = _select_streams(video_probe)

    if video_stream is None:
        raise MediaProcessingError("输入文件没有有效视频流")

    aligned_probe = _probe(aligned_audio_path)
    _, aligned_stream = _select_streams(aligned_probe)

    if aligned_stream is None:
        raise MediaProcessingError("对齐音频文件没有有效音轨")

    duration = _video_duration(
        video_stream,
        video_probe.get("format") or {},
    )
    duration_text = f"{duration:.9f}"

    video_index = _stream_index(video_stream)
    aligned_index = _stream_index(aligned_stream)

    filters = []

    def prepare_audio(input_label, gain, output_label):
        # 与前面的对齐流程一致：文件中的第一个解码采样点为 t=0。
        # 先统一采样率；可选地对每条支路做响度归一，再降回目标采样率，
        # 保证 amix 两路采样率/格式一致；最后用 volume 做相对配比。
        chain = (
            f"[{input_label}]"
            f"asetpts=PTS-STARTPTS,"
            f"aresample={sample_rate},"
        )

        if enable_loudness_norm:
            # loudnorm 内部按 192k 处理，处理后再降回目标采样率，
            # 否则两路采样率不一致会让 amix 出错或产生异常。
            chain += (
                f"loudnorm="
                f"I={loudnorm_target_lufs:g}:"
                f"LRA={loudnorm_lra:g}:"
                f"TP={loudnorm_true_peak:g}:"
                f"print_format=none,"
                f"aresample={sample_rate},"
            )

        chain += (
            f"aformat=sample_fmts=fltp:"
            f"channel_layouts=stereo,"
            f"volume={gain:.9g}"
            f"[{output_label}]"
        )
        return chain

    filters.append(
        prepare_audio(
            f"1:{aligned_index}",
            aligned_volume,
            "aligned",
        )
    )

    if original_stream is not None:
        original_index = _stream_index(original_stream)

        filters.append(
            prepare_audio(
                f"0:{original_index}",
                original_volume,
                "original",
            )
        )

        filters.append(
            "[original][aligned]"
            "amix=inputs=2:"
            "duration=longest:"
            "dropout_transition=0:"
            "normalize=0"
            "[mixed]"
        )
        source_label = "mixed"
    else:
        source_label = "aligned"

    final_chain = (
        f"[{source_label}]"
        f"apad=whole_dur={duration_text},"
        f"atrim=duration={duration_text}"
    )

    if master_loudnorm:
        final_chain += (
            f",loudnorm="
            f"I={master_target_lufs:g}:"
            f"LRA={loudnorm_lra:g}:"
            f"TP={loudnorm_true_peak:g}:"
            f"print_format=none,"
            f"aresample={sample_rate}"
        )

    if limiter:
        # level=0：关闭自动补偿增益，保留用户设置的音量关系。
        # latency=1：补偿限幅器前瞻带来的延迟。
        # 响度归一后正常配比下限制器几乎不介入，仅在极端增益时兜底防爆音。
        final_chain += (
            f",alimiter="
            f"limit={peak_ceiling:g}:"
            f"attack=5:"
            f"release=100:"
            f"level=0:"
            f"latency=1"
        )

    final_chain += "[out_audio]"
    filters.append(final_chain)

    with _temporary_output(
        output_video_path,
        [video_path, aligned_audio_path],
        ".mp4",
    ) as temp_path:
        args = [
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i", video_path,
            "-i", aligned_audio_path,
            "-filter_complex", ";".join(filters),
            "-map", f"0:{video_index}",
            "-map", "[out_audio]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", bitrate,
            "-ar", str(sample_rate),
            "-ac", "2",
            "-t", duration_text,
            "-map_metadata", "0",
            "-movflags", "+faststart",
            "-f", "mp4",
            temp_path,
        ]

        try:
            _backend.execute(args, timeout)
        except MediaProcessingError as exc:
            raise MediaProcessingError(
                f"{exc}\n"
                "请检查原生构建是否包含 AAC 编码器、"
                "amix/alimiter 等滤镜，以及输入视频编码是否支持封装为 MP4。"
            ) from exc

    return output_video_path

def enhance_video(
    video_path: str,
    denoised_audio_path: str,
    output_video_path: str,
    original_volume: float = 0.5,
    aligned_volume: float = 1.0,
) -> str:
    """
    兼容入口。
    denoised_audio_path 应传入已经完成对齐的音频。
    """
    return mix_audio_with_video(
        video_path=video_path,
        aligned_audio_path=denoised_audio_path,
        output_video_path=output_video_path,
        original_volume=original_volume,
        aligned_volume=aligned_volume,
    )