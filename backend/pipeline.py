# pipeline.py
import logging
import math
import os
import shutil
import tempfile
import uuid

from config import Config
from audio_processor import denoise_audio, align_audio, apply_manual_lag
from video_processor import (
    analyze_video,
    extract_audio_from_video,
    mix_audio_with_video,
)

logger = logging.getLogger(__name__)

def _validate_task_id(task_id):
    value = str(task_id)

    if (
        not value
        or len(value) > 100
        or any(
            char not in
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789_-"
            for char in value
        )
    ):
        raise ValueError("task_id 格式不正确")

    return value

def _validate_volume(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是数值") from exc

    # 对前端暴露一个保守范围；底层处理函数仍可独立调用。
    if not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError(f"{name} 必须在 0～2 之间")

    return value

def supports_song_lookup():
    """
    当前运行环境是否内置了音乐下载模块（music_scraper）。

    桌面端若没有该模块，只填歌名的请求会一直失败；
    调用方可以先用这个函数提前拒绝，给出明确的提示。
    """
    import importlib.util

    try:
        return importlib.util.find_spec("music_scraper") is not None
    except (ImportError, ValueError):
        return False


def _resolve_reference_audio(
    song_name,
    workspace,
    reference_audio_path=None,
):
    if reference_audio_path:
        path = os.path.abspath(os.fspath(reference_audio_path))
    else:
        if not song_name:
            raise ValueError("歌曲名称和外部音乐至少提供一个")

        try:
            from music_scraper import download_reference_audio
        except ImportError as exc:
            raise RuntimeError(
                "当前构建没有内置音乐下载模块（music_scraper），"
                "请直接选择外部音乐文件"
            ) from exc

        path = download_reference_audio(song_name, workspace)

        if not path:
            raise RuntimeError("音乐下载模块没有返回文件路径")

        path = os.path.abspath(os.fspath(path))

    if not os.path.isfile(path):
        raise FileNotFoundError(f"外部音乐不存在: {path}")

    return path

def process_video(
    video_path,
    song_name="",
    game_name=None,
    reference_audio_path=None,
    original_volume=0.4,
    aligned_volume=0.8,
    *,
    task_id=None,
    progress_callback=None,
    denoise_external=None,
    denoise_query=None,
    reject_low_confidence=None,
    manual_lag_seconds=None,
):
    """
    与运行平台无关的处理入口。

    progress_callback 接收一个 dict。
    Android 调用时必须在工作线程中执行本函数。

    manual_lag_seconds:
        None（默认）= 自动对齐，走频谱+波形搜索。
        给定数值   = **跳过搜索**，直接按这个秒数平移音乐。
                     正数 = 音乐延后，负数 = 音乐提前。
                     用户在界面上填了「手动对齐延迟」时走这条路。
    """
    task_id = _validate_task_id(task_id or uuid.uuid4().hex)
    original_volume = _validate_volume(
        original_volume, "original_volume"
    )
    aligned_volume = _validate_volume(
        aligned_volume, "aligned_volume"
    )

    video_path = os.path.abspath(os.fspath(video_path))
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"视频不存在: {video_path}")

    denoise_external = (
        Config.DENOISE_EXTERNAL
        if denoise_external is None else denoise_external
    )
    denoise_query = (
        Config.DENOISE_QUERY
        if denoise_query is None else denoise_query
    )
    reject_low_confidence = (
        Config.REJECT_LOW_CONFIDENCE
        if reject_low_confidence is None
        else reject_low_confidence
    )

    Config.ensure_dirs()

    workspace = tempfile.mkdtemp(
        prefix=f"{task_id}_",
        dir=Config.TEMP_DIR,
    )

    result_dir = os.path.join(Config.RESULT_DIR, task_id)
    os.makedirs(result_dir, exist_ok=True)
    output_path = os.path.join(result_dir, "output.mp4")

    def emit(status, progress, detail, **extra):
        if progress_callback is not None:
            progress_callback({
                "status": status,
                "progress": round(progress, 4),
                "detail": detail,
                **extra,
            })

    try:
        emit("analyzing_video", 0.05, "正在分析视频")
        video_meta = analyze_video(video_path)

        if not video_meta.get("video_stream"):
            raise ValueError("输入文件没有有效视频流")

        # 当前自动对齐流程必须有原音轨。
        # 单独的混音函数仍可处理没有原音轨的视频。
        if not video_meta.get("audio_stream"):
            raise ValueError("视频没有原音轨，无法进行自动音频对齐")

        emit("extracting_audio", 0.15, "正在提取视频音轨")
        extracted_path = os.path.join(workspace, "video_audio.wav")
        extract_audio_from_video(video_path, extracted_path)

        query_path = extracted_path

        if denoise_query:
            emit(
                "denoising_query",
                0.25,
                "正在处理用于分析的视频音轨",
            )
            query_path = denoise_audio(
                extracted_path,
                os.path.join(workspace, "video_analysis.wav"),
            )

        emit(
            "loading_reference_audio",
            0.35,
            "正在准备外部音乐",
        )
        reference_path = _resolve_reference_audio(
            song_name,
            workspace,
            reference_audio_path,
        )

        # 虽然函数名是 from_video，底层显式选择音频流，
        # 同样支持 MP3、M4A、WAV 等音频输入。
        music_pcm_path = os.path.join(workspace, "music_pcm.wav")
        extract_audio_from_video(reference_path, music_pcm_path)

        music_for_alignment = music_pcm_path

        if denoise_external:
            emit(
                "denoising_audio",
                0.45,
                "正在对外部音乐进行保守降噪",
            )
            music_for_alignment = denoise_audio(
                music_pcm_path,
                os.path.join(workspace, "music_denoised.wav"),
            )

        if manual_lag_seconds is None:
            emit("aligning_audio", 0.60, "正在对齐外部音乐")
            aligned_path = os.path.join(workspace, "music_aligned.wav")

            alignment = align_audio(
                query_audio_path=query_path,
                reference_audio_path=music_for_alignment,
                output_audio_path=aligned_path,
                analysis_sr=Config.ANALYSIS_SR,
                hop_length=Config.HOP_LENGTH,
                max_shift_seconds=Config.MAX_SHIFT_SECONDS,
            )
        else:
            # 人工延迟：用户已经听出偏差，不再搜索，直接按给定秒数平移。
            lag_value = float(manual_lag_seconds)
            if not math.isfinite(lag_value):
                raise ValueError("手动延迟必须是有限数值")
            if abs(lag_value) > Config.MAX_SHIFT_SECONDS:
                raise ValueError(
                    "手动延迟超出允许范围"
                    f"（±{Config.MAX_SHIFT_SECONDS:.0f} 秒）"
                )

            emit(
                "aligning_audio",
                0.60,
                f"正在按人工延迟 {lag_value * 1000:+.0f} ms 对齐",
            )
            aligned_path = os.path.join(workspace, "music_aligned.wav")

            alignment = apply_manual_lag(
                query_audio_path=query_path,
                reference_audio_path=music_for_alignment,
                output_audio_path=aligned_path,
                lag_seconds=lag_value,
            )

        if not isinstance(alignment, dict):
            raise RuntimeError("align_audio() 必须返回 dict")

        actual_aligned_path = alignment.get("output_audio_path")
        if (
            not actual_aligned_path
            or not os.path.isfile(actual_aligned_path)
        ):
            raise RuntimeError("对齐完成后没有生成有效音频文件")

        alignment = dict(alignment)
        warning_messages = list(alignment.get("warnings") or [])

        if alignment.get("confidence") == "low":
            if reject_low_confidence:
                raise RuntimeError(
                    "对齐可信度较低，已停止合成，请检查输入音乐"
                )
            if not warning_messages:
                warning_messages.append("对齐可信度较低，建议试听")

        # 明确表示是否因策略拒绝，不把相关系数当作正确率。
        alignment["accepted_by_policy"] = True

        emit(
            "mixing_video",
            0.82,
            "正在混合音轨并生成 MP4",
            confidence=alignment.get("confidence", "unknown"),
            warnings=warning_messages,
        )

        mix_audio_with_video(
            video_path=video_path,
            aligned_audio_path=actual_aligned_path,
            output_video_path=output_path,
            original_volume=original_volume,
            aligned_volume=aligned_volume,
            enable_loudness_norm=Config.MIX_ENABLE_LOUDNESS_NORM,
            loudnorm_target_lufs=Config.MIX_LOUDNORM_TARGET_LUFS,
            loudnorm_lra=Config.MIX_LOUDNORM_LRA,
            loudnorm_true_peak=Config.MIX_LOUDNORM_TRUE_PEAK,
            master_loudnorm=Config.MIX_MASTER_LOUDNORM,
            master_target_lufs=Config.MIX_MASTER_TARGET_LUFS,
            peak_ceiling=Config.MIX_PEAK_CEILING,
        )

        # 中间文件即将删除，结果中不要留下可被误认为有效的路径。
        alignment.pop("output_audio_path", None)

        result = {
            "status": "success",
            "task_id": task_id,
            "progress": 1.0,
            "detail": "视频处理完成",
            "output_path": output_path,
            "video_meta": video_meta,
            "audio_alignment": alignment,
            "warnings": warning_messages,
            "song_name": song_name,
            "game_name": game_name,
        }

        emit("success", 1.0, "视频处理完成")
        return result

    finally:
        shutil.rmtree(workspace, ignore_errors=True)