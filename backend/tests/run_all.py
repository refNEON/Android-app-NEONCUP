# run_all.py
"""
一条命令跑完所有需要 ffmpeg 的端到端测试。

    python tests/run_all.py

这些测试会真实调用系统 ffmpeg/ffprobe 并生成临时素材，
所以没装 ffmpeg 时会失败。纯内存的检查请用 verify_env.py。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

TESTS = [
    "test_progress_callback.py",  # Chaquopy Java 回调对象不能被当函数调（真机 bug 回归）
    "test_pipeline_smoke.py",   # 桌面链路：合成素材 -> 对齐 -> 出片
    "test_mobile_path.py",      # 用原生后端接口跑一遍（模拟 Kotlin 调用）
    "test_android_bridge.py",   # android_entry.py 的跨语言契约
]


def main():
    results = []

    for name in TESTS:
        print()
        print("=" * 62)
        print(f"  {name}")
        print("=" * 62)

        code = subprocess.call(
            [sys.executable, os.path.join(HERE, name)],
            cwd=HERE,
        )
        results.append((name, code))

    print()
    print("=" * 62)
    print("汇总")

    failed = [name for name, code in results if code != 0]

    for name, code in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")

    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
