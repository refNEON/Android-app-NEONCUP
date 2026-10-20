"""端到端冒烟测试：合成素材 -> 跑完整 pipeline -> 校验对齐结果。
仅用于本地验证，不参与打包。
"""
import json
import os
import subprocess
import sys
import tempfile

# 允许从任意目录直接运行：把 backend/ 加进模块搜索路径。
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import numpy as np
import soundfile as sf

import config
from pipeline import process_video

SR = 44100
DELAY = 3.0          # 视频音轨相对外部音乐延迟 3 秒
DURATION = 24.0


def synth_music(seconds, sr=SR):
    """合成一段有明确节奏的“音乐”，便于对齐。"""
    t = np.arange(int(seconds * sr)) / sr
    y = np.zeros_like(t)

    # 简单琶音 + 打击点，频谱与节奏都比较明显
    notes = [261.6, 329.6, 392.0, 523.3]
    for i, f in enumerate(notes):
        env = ((t * 2.0 + i * 0.25) % 1.0 < 0.5).astype(np.float64)
        y += 0.4 * env * np.sin(2 * np.pi * f * t)

    for beat in np.arange(0, seconds, 0.5):
        idx = int(beat * sr)
        n = int(0.05 * sr)
        y[idx:idx + n] += 0.6 * np.exp(-np.linspace(0, 8, n)) * np.random.randn(n)

    y /= np.max(np.abs(y))
    return y.astype(np.float32)


def main():
    work = tempfile.mkdtemp(prefix="smoke_")
    data_root = os.path.join(work, "data")
    config.Config.configure_storage(data_root)

    music = synth_music(DURATION)
    music_path = os.path.join(work, "music.mp3")
    music_wav = os.path.join(work, "music_ref.wav")
    sf.write(music_wav, music, SR, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", music_wav,
         "-c:a", "libmp3lame", "-b:a", "192k", music_path],
        check=True,
    )

    # 视频音轨 = 音乐延迟 DELAY 秒 + 噪声（模拟手元录音）
    rng = np.random.default_rng(7)
    query = np.zeros(int((DURATION + DELAY) * SR), dtype=np.float32)
    query[int(DELAY * SR):int(DELAY * SR) + len(music)] = music * 0.6
    query += 0.05 * rng.standard_normal(len(query)).astype(np.float32)
    query = np.clip(query, -1, 1)

    query_wav = os.path.join(work, "query.wav")
    sf.write(query_wav, query, SR, subtype="PCM_16")

    video_path = os.path.join(work, "hand.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=27",
         "-i", query_wav,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", video_path],
        check=True,
    )

    steps = []

    def cb(d):
        steps.append(d)
        print(f"[{d['progress']*100:5.1f}%] {d['detail']}", flush=True)

    result = process_video(
        video_path=video_path,
        song_name="",
        reference_audio_path=music_path,
        original_volume=0.4,
        aligned_volume=0.8,
        progress_callback=cb,
    )

    align = result["audio_alignment"]
    print(json.dumps({
        "output_path": result["output_path"],
        "lag_seconds": align["lag_seconds"],
        "confidence": align["confidence"],
        "feature_score": round(align["feature_score"], 4),
        "waveform_score": round(align["waveform_score"], 4),
        "selection_method": align["selection_method"],
        "output_duration": round(align["output_duration"], 3),
        "warnings": align["warnings"],
    }, ensure_ascii=False, indent=2))

    ok = True
    if not os.path.isfile(result["output_path"]):
        print("FAIL: 输出文件不存在")
        ok = False
    if os.path.getsize(result["output_path"]) == 0:
        print("FAIL: 输出文件为空")
        ok = False

    err = abs(align["lag_seconds"] - DELAY)
    print(f"\n对齐误差: {err:.4f}s (期望 ~0)")
    if err > 0.15:
        print(f"FAIL: 对齐误差过大 ({err:.3f}s)")
        ok = False

    print("SMOKE TEST:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
