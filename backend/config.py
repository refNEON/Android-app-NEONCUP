# config.py
import os
import tempfile


def _env_int(name, default, minimum=None, maximum=None):
    """读取整数环境变量；缺失或非法时退回默认值，不抛异常。"""
    raw = os.getenv(name)

    if raw is None or not str(raw).strip():
        return default

    try:
        value = int(str(raw).strip())
    except ValueError:
        return default

    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default

    return value


class Config:
    # 桌面端默认使用系统临时目录。
    # 正式部署时建议通过 MEDIA_APP_DATA_DIR 指定持久化目录。
    DATA_DIR = os.path.abspath(
        os.getenv(
            "MEDIA_APP_DATA_DIR",
            os.path.join(tempfile.gettempdir(), "music_video_app"),
        )
    )

    TEMP_DIR = os.path.join(DATA_DIR, "temp")
    UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
    RESULT_DIR = os.path.join(DATA_DIR, "results")
    REFERENCE_AUDIO_DIR = os.path.join(DATA_DIR, "reference_audio")

    RESULT_URL_PREFIX = "/results"

    MAX_CONTENT_LENGTH = 1024 * 1024 * 1024

    SECRET_KEY = os.getenv("SECRET_KEY", "local-development-only")

    # 手机/局域网访问：默认绑定 0.0.0.0，让同一网络内的设备可访问。
    HOST = os.getenv("HOST", "0.0.0.0")
    # 环境变量非法时自动退回 5000，而不是启动即崩。
    PORT = _env_int("PORT", 5000, minimum=1, maximum=65535)

    # Flask 直接托管前端页面，手机浏览器只需打开 http://<本机IP>:5000/
    # 默认指向与 backend 同级的 frontend 目录，可用 FRONTEND_DIR 覆盖。
    FRONTEND_DIR = os.path.abspath(
        os.getenv(
            "FRONTEND_DIR",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "frontend",
            ),
        )
    )

    CELERY_BROKER_URL = os.getenv(
        "CELERY_BROKER_URL",
        "redis://localhost:6379/0",
    )
    CELERY_RESULT_BACKEND = os.getenv(
        "CELERY_RESULT_BACKEND",
        "redis://localhost:6379/1",
    )

    # 可选：逗号分隔的 HTML 页面来源。
    # 同源访问不需要设置。手机 WebView / Capacitor / file:// 等
    # 跨源场景下若未配置，则默认放开（仅建议开发环境使用）。
    CORS_ORIGINS = [
        item.strip()
        for item in os.getenv("CORS_ORIGINS", "").split(",")
        if item.strip()
    ] or ["*"]

    ORIGINAL_VOLUME = 0.4
    ALIGNED_VOLUME = 0.8

    # 外部音轨降噪关
    DENOISE_EXTERNAL = False
    # 原音轨降噪开
    DENOISE_QUERY = True

    # 低可信度默认生成结果，同时返回提示。
    REJECT_LOW_CONFIDENCE = False

    ANALYSIS_SR = 22050
    HOP_LENGTH = 512
    # 对齐最大偏移窗口。原为 None（全搜索），容易被远处的伪峰带偏。
    # 限制为 60 秒既能覆盖常见的手元片头/黑屏错位，又能提升稳定性与速度。
    # 素材对得比较准时，可以进一步调小（例如 20~30）以提高精度。
    MAX_SHIFT_SECONDS = 60.0

    # ============ 混音质量 ============
    # 对“原声”和“对齐音乐”两条支路分别做响度归一（EBU R128 loudnorm），
    # 让两路先到达同一响度基准，再由 original_volume / aligned_volume
    # 做相对配比。这样能同时解决“音乐盖过手元声 / 手元声太小”的失衡问题。
    MIX_ENABLE_LOUDNESS_NORM = True
    MIX_LOUDNORM_TARGET_LUFS = -16.0   # 每条支路的基准响度
    MIX_LOUDNORM_LRA = 11.0
    MIX_LOUDNORM_TRUE_PEAK = -1.5      # 支路级真峰值上限(dBTP)

    # 混音之后限制器的线性峰值上限，防止爆音/削波。0.95 ≈ -0.45 dBFS。
    MIX_PEAK_CEILING = 0.95

    # 可选：对最终整条混好的音轨再做一次总响度归一（母带化）。
    # 打开后输出整体响度更一致，但单程 dynamic loudnorm 会轻微改变动态。
    MIX_MASTER_LOUDNORM = False
    MIX_MASTER_TARGET_LUFS = -14.0

    @classmethod
    def configure_storage(cls, root):
        """在 Android 启动或任务开始前调用，不要在任务中途修改。"""
        cls.DATA_DIR = os.path.abspath(os.fspath(root))
        cls.TEMP_DIR = os.path.join(cls.DATA_DIR, "temp")
        cls.UPLOAD_DIR = os.path.join(cls.DATA_DIR, "uploads")
        cls.RESULT_DIR = os.path.join(cls.DATA_DIR, "results")
        cls.REFERENCE_AUDIO_DIR = os.path.join(
            cls.DATA_DIR, "reference_audio"
        )
        cls.ensure_dirs()

    @classmethod
    def ensure_dirs(cls):
        for path in (
            cls.TEMP_DIR,
            cls.UPLOAD_DIR,
            cls.RESULT_DIR,
            cls.REFERENCE_AUDIO_DIR,
        ):
            os.makedirs(path, exist_ok=True)