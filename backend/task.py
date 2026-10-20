# task.py
import logging

from celery_app import celery
from config import Config
from pipeline import process_video

logger = logging.getLogger(__name__)

@celery.task(bind=True, name="tasks.process_video_task")
def process_video_task(
    self,
    video_path,
    song_name="",
    game_name=None,
    reference_audio_path=None,
    original_volume=0.4,
    aligned_volume=0.8,
):
    def report_progress(data):
        self.update_state(state="PROGRESS", meta=data)

    try:
        result = process_video(
            video_path=video_path,
            song_name=song_name,
            game_name=game_name,
            reference_audio_path=reference_audio_path,
            original_volume=original_volume,
            aligned_volume=aligned_volume,
            task_id=self.request.id,
            progress_callback=report_progress,
        )

        prefix = Config.RESULT_URL_PREFIX.rstrip("/")
        result["output_url"] = (
            f"{prefix}/{self.request.id}/output.mp4"
        )
        return result

    except Exception:
        logger.exception(
            "视频处理失败，task_id=%s",
            self.request.id,
        )
        raise