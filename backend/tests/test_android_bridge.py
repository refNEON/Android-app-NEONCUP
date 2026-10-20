"""验证 android_entry.py（Chaquopy 桥接层）的契约是否正确。

在桌面上模拟 Kotlin 侧传入的两个 Java 风格对象：
它们不是 Python callable，而是带 .execute() / .probe() 方法的对象，
probe 返回 JSON **字符串**（不是 dict）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID_PY = os.path.join(
    HERE, "..", "android", "app", "src", "main", "python"
)

sys.path.insert(0, HERE)
sys.path.insert(0, ANDROID_PY)

import android_entry  # noqa: E402

SR = 44100


class NativeBackend:
    """模拟 Chaquopy 从 Kotlin 传进来的 Java 对象。"""

    def __init__(self):
        self.calls = {"execute": 0, "probe": 0}

    def execute(self, args, timeout):
        self.calls["execute"] += 1
        args = [str(a) for a in args]
        r = subprocess.run(
            ["ffmpeg", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                "ffmpeg 失败\n" + r.stderr.decode("utf-8", "replace")[-1500:]
            )

    def probe(self, path, timeout):
        self.calls["probe"] += 1
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError("ffprobe 失败")
        # 关键：返回字符串，不是 dict
        return r.stdout.decode("utf-8")


class JavaProgress:
    def __init__(self):
        self.events = []

    def onProgress(self, data):
        self.events.append(dict(data))


def main():
    work = tempfile.mkdtemp(prefix="bridge_")

    t = np.arange(int(18 * SR)) / SR
    music = sum(
        0.3 * np.sin(2 * np.pi * f * t) for f in (261.6, 329.6, 392.0, 523.3)
    ).astype(np.float32)
    music /= np.max(np.abs(music))

    music_path = os.path.join(work, "music.mp3")
    sf.write(os.path.join(work, "m.wav"), music, SR, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i",
         os.path.join(work, "m.wav"), "-c:a", "libmp3lame", music_path],
        check=True,
    )

    DELAY = 1.8
    q = np.zeros(int(21 * SR), dtype=np.float32)
    q[int(DELAY * SR):int(DELAY * SR) + len(music)] = music * 0.5
    qwav = os.path.join(work, "q.wav")
    sf.write(qwav, q, SR, subtype="PCM_16")

    video_path = os.path.join(work, "hand.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=256x144:rate=15:duration=21",
         "-i", qwav, "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-preset", "ultrafast", "-c:a", "aac", "-shortest", video_path],
        check=True,
    )

    native = NativeBackend()
    data_root = os.path.join(work, "data")

    print("--- bootstrap ---")
    info = android_entry.bootstrap(data_root, native)
    print(json.dumps(info, ensure_ascii=False))

    ok = True
    if info.get("status") != "ok" or not info.get("native_backend"):
        print("FAIL: bootstrap 未注入原生后端")
        ok = False

    print("--- is_ready ---")
    print(android_entry.is_ready())

    print("--- start ---")
    sink = JavaProgress()
    raw = android_entry.start(
        video_path, music_path, 0.4, 0.8, sink
    )
    print("progress 事件:", len(sink.events))
    result = json.loads(raw)
    print(json.dumps({
        k: result[k] for k in ("status", "output_path")
        if k in result
    }, ensure_ascii=False, indent=2))

    if result.get("status") != "success":
        print("FAIL:", result)
        ok = False
    else:
        a = result["audio_alignment"]
        err = abs(a["lag_seconds"] - DELAY)
        print(f"lag={a['lag_seconds']:.4f}s 期望={DELAY}s 误差={err:.4f}s")
        print("confidence:", a["confidence"])
        if err > 0.15:
            print("FAIL: 对齐误差过大")
            ok = False

    print(f"原生调用次数: {native.calls}")
    if native.calls["execute"] < 2 or native.calls["probe"] < 2:
        print("FAIL: 原生后端没有被真正调用")
        ok = False

    print("\nBRIDGE TEST:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
