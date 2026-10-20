# android_web.py
"""
Android 内嵌的极简静态服务（只给 WebView 用）。

为什么需要它
------------
主界面换成 frontend/cupnb.html 后，WebView 需要一个"后端地址"：

  * 页面本身（以及它引用的 assets）要能被 http:// 加载；
  * <video src="/results/xxx/output.mp4"> 要能被播放和拖动进度，
    这要求服务端支持 HTTP Range。

任务提交**不走这里**：WebView 里的大文件 multipart 上传既慢又难保证内存，
改由 Kotlin 的 JS bridge 直接把本地路径交给 Python 内核。这里只负责
"把文件发出去"，职责单一，也便于以后替换 / 升级界面。

安全：只监听 127.0.0.1，随机端口，进程退出即失效。
"""
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 64 * 1024

# 不用 mimetypes.guess_type：它在 Windows 上首次调用会去读注册表（可能卡好几秒），
# 在 Android 上也依赖系统数据库。这里写死一份够用的表，行为可控。
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def _safe_join(root, relative):
    """把相对路径安全地拼到 root 下，越界（../）一律拒绝。"""
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, relative))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


class _Handler(BaseHTTPRequestHandler):
    server_version = "SyncStage/0.3"
    protocol_version = "HTTP/1.1"

    # 由 start() 注入
    web_root = ""
    result_dir = ""

    def log_message(self, fmt, *args):  # 别把日志刷到 logcat
        pass

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("", "/", "/index.html"):
            path = "/cupnb.html"

        if path.startswith("/results/"):
            relative = path[len("/results/"):]
            target = _safe_join(self.result_dir, relative)
            self._send_file(target, supports_range=True)
            return

        target = _safe_join(self.web_root, path.lstrip("/"))
        self._send_file(target, supports_range=False)

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ("", "/", "/index.html"):
            path = "/cupnb.html"
        root, relative = (
            (self.result_dir, path[len("/results/"):])
            if path.startswith("/results/")
            else (self.web_root, path.lstrip("/"))
        )
        target = _safe_join(root, relative)
        if not target or not os.path.isfile(target):
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", self._content_type(target))
        self.send_header("Content-Length", str(os.path.getsize(target)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    # ---------- 发送 ----------

    @staticmethod
    def _content_type(path):
        ext = os.path.splitext(path)[1].lower()
        return _MIME_TYPES.get(ext, "application/octet-stream")

    def _send_file(self, path, supports_range):
        if not path or not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return

        size = os.path.getsize(path)
        start, end = 0, size - 1
        partial = False

        range_header = self.headers.get("Range") if supports_range else None
        if range_header:
            match = _RANGE_RE.match(range_header.strip())
            if match:
                first, second = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    if second:
                        end = int(second)
                elif second:  # bytes=-N 取末尾 N 字节
                    start = max(0, size - int(second))
                end = min(end, size - 1)

                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", self._content_type(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # 用户拖动进度条会中断旧请求，属于正常现象
                    return
                remaining -= len(chunk)


class WebServer:
    def __init__(self, httpd):
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="syncstage-web",
            daemon=True,
        )

    def start(self):
        self._thread.start()
        return self.port

    def shutdown(self):
        try:
            self._httpd.shutdown()
        except Exception:
            pass


def start(web_root, result_dir):
    """启动服务并返回 WebServer（.port 即随机端口）。"""
    if not web_root or not os.path.isdir(web_root):
        raise ValueError(f"web_root 不存在: {web_root}")
    if not result_dir or not os.path.isdir(result_dir):
        raise ValueError(f"result_dir 不存在: {result_dir}")

    handler = type(
        "SyncStageHandler",
        (_Handler,),
        {"web_root": web_root, "result_dir": result_dir},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    return WebServer(httpd)
