# tests/test_web_server.py
"""
android_web 的自检：静态页面 + 结果视频的 HTTP Range。

只依赖标准库，不需要 ffmpeg，可以在任何机器上跑。
"""
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import android_web  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def main():
    web_root = tempfile.mkdtemp(prefix="web-")
    result_root = tempfile.mkdtemp(prefix="res-")

    with open(os.path.join(web_root, "cupnb.html"), "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><title>SyncStage</title><p>hello</p>")

    task_dir = os.path.join(result_root, "task1")
    os.makedirs(task_dir)
    payload = bytes(range(256)) * 400  # 102400 字节，够测分段
    with open(os.path.join(task_dir, "output.mp4"), "wb") as f:
        f.write(payload)

    server = android_web.start(web_root, result_root)
    base = f"http://127.0.0.1:{server.port}"

    try:
        print("静态页面")
        status, headers, body = fetch(base + "/")
        check("GET / 返回 200", status == 200, f"实际 {status}")
        check("内容类型是 html", "text/html" in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))
        check("页面内容正确", b"SyncStage" in body)

        status, _, body = fetch(base + "/cupnb.html")
        check("GET /cupnb.html 返回 200", status == 200 and b"hello" in body)

        status, _, _ = fetch(base + "/nope.html")
        check("不存在的文件返回 404", status == 404, f"实际 {status}")

        status, _, _ = fetch(base + "/../etc/passwd")
        check("路径穿越被拒绝", status in (400, 404), f"实际 {status}")

        print("结果视频")
        status, headers, body = fetch(base + "/results/task1/output.mp4")
        check("完整下载 200", status == 200, f"实际 {status}")
        check("Content-Length 正确", len(body) == len(payload), f"{len(body)}")
        check("声明支持 Range", headers.get("Accept-Ranges") == "bytes")

        status, headers, body = fetch(
            base + "/results/task1/output.mp4", {"Range": "bytes=100-199"}
        )
        check("分段请求返回 206", status == 206, f"实际 {status}")
        check("分段内容正确", body == payload[100:200], f"{len(body)} 字节")
        check(
            "Content-Range 正确",
            headers.get("Content-Range") == f"bytes 100-199/{len(payload)}",
            headers.get("Content-Range", ""),
        )

        status, headers, body = fetch(
            base + "/results/task1/output.mp4", {"Range": "bytes=-50"}
        )
        check("末尾 N 字节可用", status == 206 and body == payload[-50:], f"实际 {status}")

        status, _, _ = fetch(
            base + "/results/task1/output.mp4", {"Range": "bytes=999999-"}
        )
        check("越界 Range 返回 416", status == 416, f"实际 {status}")

        status, _, _ = fetch(base + "/results/task1/missing.mp4")
        check("缺失的结果返回 404", status == 404, f"实际 {status}")

    finally:
        server.shutdown()

    print(f"\n{ PASSED } 项通过, { FAILED } 项失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
