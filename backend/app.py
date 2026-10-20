# app.py
import math
import os
import re
import shutil
import uuid

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from celery.result import AsyncResult

from config import Config
from celery_app import celery
from task import process_video_task
from pipeline import supports_song_lookup

app = Flask(__name__)
app.config.from_object(Config)

if Config.CORS_ORIGINS:
    CORS(
        app,
        resources={
            r"/api/*": {"origins": Config.CORS_ORIGINS},
            r"/results/*": {"origins": Config.CORS_ORIGINS},
        },
    )

Config.ensure_dirs()

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma",
}

TASK_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

def _extension(file):
    return os.path.splitext(file.filename or "")[1].lower()

def _volume(field, default):
    try:
        value = float(request.form.get(field, default))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是数值") from exc

    if not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError(f"{field} 必须在 0～2 之间")

    return value

@app.errorhandler(413)
def upload_too_large(error):
    return jsonify({"error": "上传文件超过大小限制"}), 413

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})

@app.route("/api/upload", methods=["POST"])
def upload_file():
    video_file = request.files.get("videoFile")
    audio_file = request.files.get("audioFile")

    song_name = request.form.get("songName", "").strip()
    game_name = request.form.get("gameName", "").strip() or None

    if video_file is None or not video_file.filename:
        return jsonify({"error": "缺少视频文件"}), 400

    if _extension(video_file) not in VIDEO_EXTENSIONS:
        return jsonify({"error": "不支持的视频格式"}), 400

    has_audio = audio_file is not None and bool(audio_file.filename)

    if has_audio and _extension(audio_file) not in AUDIO_EXTENSIONS:
        return jsonify({"error": "不支持的音频格式"}), 400

    if not song_name and not has_audio:
        return jsonify({
            "error": "歌曲名称和外部音乐至少提供一个"
        }), 400

    # 只填歌名需要 music_scraper。没有内置时立刻拒绝，
    # 避免任务进了队列才失败、用户只看到一句笼统的“处理失败”。
    if song_name and not has_audio and not supports_song_lookup():
        return jsonify({
            "error": "当前服务没有内置音乐下载模块，请直接选择外部音乐文件"
        }), 400

    try:
        original_volume = _volume("originalVolume", "0.4")
        aligned_volume = _volume("alignedVolume", "0.8")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    task_id = uuid.uuid4().hex
    upload_dir = os.path.join(Config.UPLOAD_DIR, task_id)

    # task.json 用于区分本服务创建的任务和不存在的任务。
    # 创建失败时整个目录会清理。
    marker_path = os.path.join(upload_dir, "task.json")

    try:
        os.makedirs(upload_dir, exist_ok=False)

        # 固定角色文件名，避免同名文件覆盖。
        video_path = os.path.join(
            upload_dir,
            "video" + _extension(video_file),
        )
        video_file.save(video_path)

        reference_audio_path = None

        if has_audio:
            reference_audio_path = os.path.join(
                upload_dir,
                "music" + _extension(audio_file),
            )
            audio_file.save(reference_audio_path)

        with open(marker_path, "w", encoding="utf-8") as marker:
            marker.write("{}")

        process_video_task.apply_async(
            kwargs={
                "video_path": video_path,
                "song_name": song_name,
                "game_name": game_name,
                "reference_audio_path": reference_audio_path,
                "original_volume": original_volume,
                "aligned_volume": aligned_volume,
            },
            task_id=task_id,
            # Broker 发送失败时不进行长时间自动重试。
            retry=False,
        )

    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        app.logger.exception("保存文件或创建任务失败")

        return jsonify({
            "error": "创建任务失败，请检查存储空间和任务服务"
        }), 503

    return jsonify({
        "task_id": task_id,
        "message": "任务已创建",
    }), 202

def _known_task(task_id):
    if not TASK_ID_PATTERN.fullmatch(task_id):
        return False

    return os.path.isfile(
        os.path.join(Config.UPLOAD_DIR, task_id, "task.json")
    )

@app.route("/api/tasks/<task_id>", methods=["GET"])
def get_task_status(task_id):
    if not _known_task(task_id):
        return jsonify({"error": "任务不存在"}), 404

    try:
        result = AsyncResult(task_id, app=celery)
        state = result.state

        if state == "PENDING":
            return jsonify({
                "task_id": task_id,
                "state": state,
                "status": "waiting",
                "progress": 0.0,
                "detail": "任务等待中",
            })

        if state == "PROGRESS":
            info = result.info
            data = info if isinstance(info, dict) else {}

            return jsonify({
                **data,
                "task_id": task_id,
                "state": state,
            })

        if state == "SUCCESS":
            data = result.result
            data = data if isinstance(data, dict) else {}

            return jsonify({
                **data,
                "task_id": task_id,
                "state": state,
            })

        if state == "FAILURE":
            return jsonify({
                "task_id": task_id,
                "state": state,
                "status": "failed",
                "detail": str(result.info),
            }), 500

        return jsonify({
            "task_id": task_id,
            "state": state,
            "detail": "",
        })

    except Exception:
        app.logger.exception("读取任务状态失败")
        return jsonify({"error": "暂时无法读取任务状态"}), 503

@app.route(
    "/results/<task_id>/<path:filename>",
    methods=["GET"],
)
def download_result(task_id, filename):
    if not _known_task(task_id):
        return jsonify({"error": "任务不存在"}), 404

    # 当前只开放最终视频。
    if filename != "output.mp4":
        return jsonify({"error": "文件不存在"}), 404

    directory = os.path.join(Config.RESULT_DIR, task_id)

    return send_from_directory(
        directory,
        filename,
        as_attachment=False,
        conditional=True,
    )

@app.route("/", methods=["GET"])
def serve_frontend():
    if os.path.isdir(Config.FRONTEND_DIR):
        return send_from_directory(Config.FRONTEND_DIR, "cupnb.html")

    return jsonify({
        "status": "ok",
        "note": "未找到前端目录，请设置 FRONTEND_DIR 环境变量",
    })

@app.route("/assets/<path:filename>", methods=["GET"])
def serve_frontend_assets(filename):
    assets_dir = os.path.join(Config.FRONTEND_DIR, "assets")
    if not os.path.isdir(assets_dir):
        return jsonify({"error": "资源不存在"}), 404

    return send_from_directory(assets_dir, filename)

if __name__ == "__main__":
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=False,
        use_reloader=False,
    )