# test.py
import argparse
import json

from pipeline import process_video

def main():
    parser = argparse.ArgumentParser(
        description="测试音乐对齐与视频混音"
    )
    parser.add_argument("--video", default="test/video.mp4")
    parser.add_argument("--music", default="test/music.mp3")
    parser.add_argument("--original-volume", type=float, default=0.4)
    parser.add_argument("--aligned-volume", type=float, default=0.8)

    parser.add_argument(
        "--no-denoise",
        action="store_true",
        help="关闭外部音乐降噪",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="低可信度时停止合成",
    )

    args = parser.parse_args()

    def progress(data):
        percent = data["progress"] * 100
        print(f"[{percent:5.1f}%] {data['detail']}", flush=True)

    result = process_video(
        video_path=args.video,
        reference_audio_path=args.music,
        original_volume=args.original_volume,
        aligned_volume=args.aligned_volume,
        progress_callback=progress,
        denoise_external=not args.no_denoise,
        reject_low_confidence=args.strict,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()