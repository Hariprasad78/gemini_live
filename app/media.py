"""Camera, screen capture, and preview helpers."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import PIL.Image
from google.genai import types

from .config import PREVIEW_WINDOW_NAME


class PreviewWindow:
    def __init__(
        self,
        enabled: bool,
        *,
        brightness: float = 1.0,
        contrast: float = 1.0,
        web_enabled: bool = False,
        web_host: str = "0.0.0.0",
        web_port: int = 8080,
    ):
        self.enabled = enabled
        self.brightness = float(brightness)
        self.contrast = float(contrast)
        self.web_enabled = web_enabled
        self.web_host = web_host
        self.web_port = int(web_port)
        self._latest_jpeg: bytes | None = None
        self._frame_version = 0
        self._frame_condition = threading.Condition()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    def start_web(self) -> None:
        if not self.web_enabled or self._server is not None:
            return
        preview = self

        class StreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                if self.path in {"/", "/index.html"}:
                    self._send_index()
                    return
                if self.path == "/snapshot.jpg":
                    self._send_snapshot()
                    return
                if self.path == "/stream.mjpg":
                    self._send_stream()
                    return
                self.send_error(404)

            def _send_index(self) -> None:
                page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{PREVIEW_WINDOW_NAME}</title>
  <style>
    html, body {{ margin: 0; height: 100%; background: #101214; color: #f3f4f6; font-family: Arial, sans-serif; }}
    main {{ min-height: 100%; display: grid; grid-template-rows: auto 1fr; }}
    header {{ padding: 12px 16px; background: #1c2127; border-bottom: 1px solid #313943; }}
    h1 {{ margin: 0; font-size: 18px; font-weight: 700; }}
    .stage {{ display: grid; place-items: center; padding: 12px; }}
    img {{ width: 100%; max-width: 1280px; max-height: calc(100vh - 72px); object-fit: contain; background: #000; }}
  </style>
</head>
<body>
  <main>
    <header><h1>{PREVIEW_WINDOW_NAME}</h1></header>
    <section class="stage"><img src="/stream.mjpg" alt="Live stream"></section>
  </main>
</body>
</html>"""
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_snapshot(self) -> None:
                frame_data = preview.wait_for_jpeg(timeout_seconds=2.0)
                if frame_data is None:
                    self.send_error(503, "No video frame is available yet")
                    return
                frame, _ = frame_data
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)

            def _send_stream(self) -> None:
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                last_version = 0
                while True:
                    frame_data = preview.wait_for_jpeg(
                        timeout_seconds=5.0,
                        after_version=last_version,
                    )
                    if frame_data is None:
                        continue
                    frame, last_version = frame_data
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        return

        self._server = ThreadingHTTPServer((self.web_host, self.web_port), StreamHandler)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        print(f"[web] live stream: http://{self.web_host}:{self.web_port}", flush=True)

    def enhance(self, frame):
        import cv2

        if self.brightness == 1.0 and self.contrast == 1.0:
            return frame
        beta = int((self.brightness - 1.0) * 80)
        return cv2.convertScaleAbs(frame, alpha=self.contrast, beta=beta)

    def update_web_frame(self, jpeg_bytes: bytes) -> None:
        if not self.web_enabled:
            return
        with self._frame_condition:
            self._latest_jpeg = jpeg_bytes
            self._frame_version += 1
            self._frame_condition.notify_all()

    def wait_for_jpeg(
        self,
        *,
        timeout_seconds: float,
        after_version: int | None = None,
    ) -> tuple[bytes, int] | None:
        with self._frame_condition:
            if after_version is None:
                if self._latest_jpeg is None:
                    self._frame_condition.wait(timeout=timeout_seconds)
            else:
                deadline = time.monotonic() + timeout_seconds
                while self._frame_version <= after_version:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._frame_condition.wait(timeout=remaining)
            if self._latest_jpeg is None:
                return None
            return self._latest_jpeg, self._frame_version

    def show(self, frame) -> None:
        if not self.enabled:
            return
        import cv2

        cv2.imshow(PREVIEW_WINDOW_NAME, frame)
        cv2.waitKey(1)

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.enabled:
            import cv2

            cv2.destroyAllWindows()


def _encode_frame(frame, preview: PreviewWindow) -> bytes:
    import cv2

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = PIL.Image.fromarray(frame_rgb)
    img.thumbnail([1024, 1024])

    image_io = io.BytesIO()
    img.save(image_io, format="jpeg")
    jpeg_bytes = image_io.getvalue()
    preview.update_web_frame(jpeg_bytes)
    return jpeg_bytes


def _camera_frame(cap, preview: PreviewWindow):
    ret, frame = cap.read()
    if not ret:
        return None
    enhanced_frame = preview.enhance(frame)
    return enhanced_frame, _encode_frame(enhanced_frame, preview)


def _screen_frame(preview: PreviewWindow):
    import cv2
    import numpy as np

    try:
        import mss
    except ImportError as exc:
        raise ImportError("Please install mss package using 'pip install mss'") from exc

    with mss.mss() as sct:
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)

    preview_image = PIL.Image.frombytes("RGB", shot.size, shot.rgb)
    preview_frame = cv2.cvtColor(np.array(preview_image), cv2.COLOR_RGB2BGR)
    preview_frame = preview.enhance(preview_frame)
    image_bytes = io.BytesIO()
    preview_image = PIL.Image.fromarray(cv2.cvtColor(preview_frame, cv2.COLOR_BGR2RGB))
    preview_image.thumbnail([1024, 1024])
    preview_image.save(image_bytes, format="jpeg")
    jpeg_bytes = image_bytes.getvalue()
    preview.update_web_frame(jpeg_bytes)
    return preview_frame, jpeg_bytes


async def stream_camera(queue: asyncio.Queue, preview: PreviewWindow) -> None:
    import cv2

    preview.start_web()
    cap = await asyncio.to_thread(cv2.VideoCapture, 0)
    last_sent = 0.0
    send_interval = 0.5
    try:
        while True:
            frame_result = await asyncio.to_thread(_camera_frame, cap, preview)
            if frame_result is None:
                return
            preview_frame, image_bytes = frame_result
            preview.show(preview_frame)
            now = time.monotonic()
            if (now - last_sent) >= send_interval:
                last_sent = now
                await queue.put(("video", types.Blob(data=image_bytes, mime_type="image/jpeg")))
            await asyncio.sleep(0.01)
    finally:
        cap.release()


async def stream_screen(queue: asyncio.Queue, preview: PreviewWindow) -> None:
    preview.start_web()
    last_sent = 0.0
    send_interval = 0.5
    while True:
        frame_result = await asyncio.to_thread(_screen_frame, preview)
        if frame_result is None:
            return
        preview_frame, image_bytes = frame_result
        preview.show(preview_frame)
        now = time.monotonic()
        if (now - last_sent) >= send_interval:
            last_sent = now
            await queue.put(("video", types.Blob(data=image_bytes, mime_type="image/jpeg")))
        await asyncio.sleep(0.05)
