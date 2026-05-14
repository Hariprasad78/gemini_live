"""Camera, screen capture, and preview helpers."""

from __future__ import annotations

import asyncio
import io
import time

import PIL.Image
from google.genai import types

from .config import PREVIEW_WINDOW_NAME


class PreviewWindow:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def show(self, frame) -> None:
        if not self.enabled:
            return
        import cv2

        cv2.imshow(PREVIEW_WINDOW_NAME, frame)
        cv2.waitKey(1)

    def close(self) -> None:
        if self.enabled:
            import cv2

            cv2.destroyAllWindows()


def _encode_frame(frame) -> bytes:
    import cv2

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = PIL.Image.fromarray(frame_rgb)
    img.thumbnail([1024, 1024])

    image_io = io.BytesIO()
    img.save(image_io, format="jpeg")
    return image_io.getvalue()


def _camera_frame(cap):
    ret, frame = cap.read()
    if not ret:
        return None
    return frame, _encode_frame(frame)


def _screen_frame():
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
    image_bytes = io.BytesIO()
    preview_image.save(image_bytes, format="jpeg")
    return preview_frame, image_bytes.getvalue()


async def stream_camera(queue: asyncio.Queue, preview: PreviewWindow) -> None:
    import cv2

    cap = await asyncio.to_thread(cv2.VideoCapture, 0)
    last_sent = 0.0
    send_interval = 0.5
    try:
        while True:
            frame_result = await asyncio.to_thread(_camera_frame, cap)
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
    last_sent = 0.0
    send_interval = 0.5
    while True:
        frame_result = await asyncio.to_thread(_screen_frame)
        if frame_result is None:
            return
        preview_frame, image_bytes = frame_result
        preview.show(preview_frame)
        now = time.monotonic()
        if (now - last_sent) >= send_interval:
            last_sent = now
            await queue.put(("video", types.Blob(data=image_bytes, mime_type="image/jpeg")))
        await asyncio.sleep(0.05)
