"""在桌面上完整模拟 Android 调用路径。

不走 Flask / Celery，而是像 Kotlin 那样：
    initialize(data_root, backend=(execute_fn, probe_fn))
    run_process(video, music, progress_callback=Java风格对象)
execute_fn / probe_fn 内部改用真实 ffmpeg/ffprobe，
从而验证整条"原生后端 -> 内核 -> 结果"链路。
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

import mobile_api

SR = 44100


def execute_fn(args, timeout):
    args = [str(a) for a in args]
    result = subprocess.run(
        ["ffmpeg", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg 失败\n" + result.stderr.decode("utf-8", "replace")[-2000:]
        )


def probe_fn(path, timeout):
    """注意：真实 Android 端这里返回的是 ffprobe 的 JSON 字符串，
    由 android_entry.py 再 json.loads。这里直接模拟最终产物。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams",
         "-of", "json", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe 失败")
    return json.loads(result.stdout.decode("utf-8"))


class JavaProgressSink:
    """模拟 Chaquopy 从 Kotlin 传进来的 Java 对象（不是 Python callable）。"""

    def __init__(self):
        self.events = []

    def onProgress(self, data):
        self.events.append(dict(data))


def main():
    work = tempfile.mkdtemp(prefix="android_sim_")

    music = np.zeros(int(20 * SR), dtype=np.float32)
    t = np.arange(len(music)) / SR
    for f in (261.6, 329.6, 392.0, 523.3):
        music += (0.3 * np.sin(2 * np.pi * f * t)).astype(np.float32)
    for beat in np.arange(0, 20, 0.5):
        i = int(beat * SR)
        n = int(0.04 * SR)
        music[i:i + n] += (0.5 * np.exp(-np.linspace(0, 8, n))).astype(np.float32)
    music /= np.max(np.abs(music))

    music_path = os.path.join(work, "music.mp3")
    sf.write(os.path.join(work, "m.wav"), music, SR, subtype="PCM_16")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i",
                    os.path.join(work, "m.wav"), "-c:a", "libmp3lame",
                    music_path], check=True)

    DELAY = 2.5
    rng = np.random.default_rng(3)
    q = np.zeros(int(23 * SR), dtype=np.float32)
    q[int(DELAY * SR):int(DELAY * SR) + len(music)] = music * 0.55
    q += (0.04 * rng.standard_normal(len(q))).astype(np.float32)
    qwav = os.path.join(work, "q.wav")
    sf.write(qwav, np.clip(q, -1, 1), SR, subtype="PCM_16")

    video_path = os.path.join(work, "hand.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=23",
         "-i", qwav, "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-preset", "ultrafast", "-c:a", "aac", "-shortest", video_path],
        check=True,
    )

    data_root = os.path.join(work, "data")
    info = mobile_api.initialize(data_root, backend=(execute_fn, probe_fn))
    print("initialize ->", info)
    print("is_ready   ->", mobile_api.is_ready())

    sink = JavaProgressSink()
    result = mobile_api.run_process(
        video_path=video_path,
        reference_audio_path=music_path,
        original_volume=0.4,
        aligned_volume=0.8,
        progress_callback=sink,
    )

    print("\nprogress 事件数:", len(sink.events))
    for e in sink.events:
        print(f"  [{float(e['progress'])*100:5.1f}%] {e['detail']}")

    ok = True
    if result.get("status") != "success":
        print("FAIL:", result)
        ok = False
    else:
        a = result["audio_alignment"]
        err = abs(a["lag_seconds"] - DELAY)
        print(f"\nlag={a['lag_seconds']:.4f}s 期望={DELAY}s 误差={err:.4f}s")
        print("confidence:", a["confidence"])
        print("output:", result["output_path"])
        if err > 0.15:
            print("FAIL: 对齐误差过大")
            ok = False
        if not os.path.isfile(result["output_path"]):
            print("FAIL: 输出文件不存在")
            ok = False
        # 结果必须可 JSON 序列化（要跨语言传回 Kotlin）
        json.dumps(result, ensure_ascii=False)
        print("JSON 序列化: OK")

    print("\nANDROID SIM:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
