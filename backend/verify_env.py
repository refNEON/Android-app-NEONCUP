# verify_env.py
"""
环境自检：一条命令查清桌面端能不能跑起来。

    python verify_env.py

检查项：
  1. Python 版本
  2. Python 依赖（numpy / scipy / soundfile / Flask / celery / redis ...）
  3. 系统 ffmpeg / ffprobe
  4. Redis 是否可连（Celery 的 broker）
  5. 内核模块能否导入 + 一次纯内存的特征提取冒烟测试
  6. 音乐下载模块 music_scraper 是否存在（不存在则只能走外部音乐）
"""
import importlib
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile

OK = "[OK]  "
WARN = "[WARN]"
FAIL = "[FAIL]"

problems = []


def check(name, ok, detail="", fatal=False):
    mark = OK if ok else (FAIL if fatal else WARN)
    print(f"{mark} {name}" + (f"  {detail}" if detail else ""))

    if not ok:
        problems.append(name)

    return ok


def python_version():
    v = sys.version_info
    ok = v >= (3, 9)
    check("Python 版本", ok, f"{v.major}.{v.minor}.{v.micro}", fatal=not ok)


def package_version(module_name):
    """优先用 importlib.metadata，避免 Flask 等包的 __version__ 弃用告警。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(module_name)
    except Exception:
        try:
            mod = importlib.import_module(module_name)
            return getattr(mod, "__version__", "?")
        except Exception:
            return "?"


def python_packages():
    core = ["numpy", "scipy", "soundfile"]
    optional = ["soxr", "noisereduce"]
    # 显示名 -> (导入名, 分发名)
    web = {
        "flask": ("flask", "flask"),
        "flask-cors": ("flask_cors", "flask-cors"),
        "celery": ("celery", "celery"),
        "redis": ("redis", "redis"),
    }

    for name in core:
        try:
            importlib.import_module(name)
            check(f"核心依赖 {name}", True, package_version(name))
        except ImportError as exc:
            check(f"核心依赖 {name}", False, str(exc), fatal=True)

    for name in optional:
        try:
            importlib.import_module(name)
            check(f"可选依赖 {name}", True, package_version(name))
        except ImportError:
            check(f"可选依赖 {name}", False, "未安装（不影响运行）")

    for label, (module_name, dist_name) in web.items():
        try:
            importlib.import_module(module_name)
            check(f"桌面壳依赖 {label}", True, package_version(dist_name))
        except ImportError:
            check(f"桌面壳依赖 {label}", False, "未安装（Web 服务无法启动）")


def ffmpeg():
    for program in ("ffmpeg", "ffprobe"):
        path = shutil.which(program)

        if not path:
            check(f"系统 {program}", False, "未找到，请安装并加入 PATH")
            continue

        try:
            out = subprocess.run(
                [program, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
            ).stdout.decode("utf-8", "replace")
            first = out.splitlines()[0] if out else ""
            check(f"系统 {program}", True, first[:60])
        except OSError as exc:
            check(f"系统 {program}", False, str(exc))


def redis_server():
    try:
        import redis  # noqa: F401
    except ImportError:
        check("Redis 连通性", False, "redis 包未安装，跳过")
        return

    from config import Config

    try:
        client = redis.Redis.from_url(
            Config.CELERY_BROKER_URL, socket_connect_timeout=2
        )
        client.ping()
        check("Redis 连通性", True, Config.CELERY_BROKER_URL)
        return
    except Exception as exc:
        check(
            "Redis 连通性",
            False,
            f"{type(exc).__name__}（Celery 需要它，先启动 redis-server）",
        )


def kernel_smoke():
    try:
        import numpy as np

        import audio_processor
        import config
        import mobile_api  # noqa: F401
        import pipeline    # noqa: F401
        import video_processor
    except Exception as exc:
        check("内核模块导入", False, f"{type(exc).__name__}: {exc}", fatal=True)
        return

    check("内核模块导入", True, "config/pipeline/audio/video/mobile_api")

    # 纯内存的特征提取冒烟测试，不碰磁盘和 ffmpeg。
    try:
        sr = 22050
        y = (np.sin(2 * np.pi * 440 * np.arange(sr * 3) / sr) * 0.5)
        feat = audio_processor._build_feature(
            y.astype(np.float32), sr, config.Config.HOP_LENGTH
        )
        finite = bool(np.all(np.isfinite(feat)))
        check("特征提取冒烟测试", finite, f"shape={feat.shape}")
    except Exception as exc:
        check("特征提取冒烟测试", False, f"{type(exc).__name__}: {exc}",
              fatal=True)


def song_lookup():
    exists = importlib.util.find_spec("music_scraper") is not None
    check(
        "音乐下载模块 music_scraper",
        exists,
        "可用（支持只填歌名）" if exists
        else "未提供，只能选择外部音乐文件",
    )


def storage():
    try:
        from config import Config

        Config.ensure_dirs()
        probe = os.path.join(Config.TEMP_DIR, ".write_test")

        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("1")

        os.remove(probe)
        check("数据目录可写", True, Config.DATA_DIR)
    except Exception as exc:
        check("数据目录可写", False, f"{type(exc).__name__}: {exc}", fatal=True)


def main():
    print("=" * 62)
    print("  music_video_helper 环境自检")
    print("=" * 62)
    print()

    python_version()
    print()
    python_packages()
    print()
    ffmpeg()
    print()
    redis_server()
    print()
    storage()
    print()
    kernel_smoke()
    song_lookup()

    print()
    print("=" * 62)

    if problems:
        print(f"发现 {len(problems)} 项需要处理：")
        for item in problems:
            print(f"  - {item}")
        print()
        print("提示：python -m pip install -r requirements.txt")
        return 1

    print("全部检查通过，可以启动 start.bat。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
