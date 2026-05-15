"""Camera, screen capture, and preview helpers."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable

import PIL.Image

from .config import PREVIEW_WINDOW_NAME

_LANCZOS = getattr(getattr(PIL.Image, "Resampling", PIL.Image), "LANCZOS")


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
        frame_max_size: int = 1600,
        jpeg_quality: int = 92,
        send_interval_seconds: float = 1.5,
    ):
        self.enabled = enabled
        self.brightness = float(brightness)
        self.contrast = float(contrast)
        self.web_enabled = web_enabled
        self.web_host = web_host
        self.web_port = int(web_port)
        self.frame_max_size = max(320, int(frame_max_size))
        self.jpeg_quality = max(40, min(95, int(jpeg_quality)))
        self.send_interval_seconds = max(0.5, float(send_interval_seconds))
        self._latest_jpeg: bytes | None = None
        self._frame_version = 0
        self._frame_condition = threading.Condition()
        self._event_condition = threading.Condition()
        self._event_id = 0
        self._events: list[dict[str, object]] = []
        self._status: dict[str, object] = {
            "task": "No active task",
            "ai": "Waiting for an AI decision",
            "plan": "none",
            "view": "",
            "ui": "",
            "device": "",
            "ir": "",
            "updated_at": time.time(),
        }
        self._task_command_handler: Callable[[str], bool] | None = None
        self._voice_event_handler: Callable[[str, int], bool] | None = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    def set_task_command_handler(self, handler: Callable[[str], bool]) -> None:
        self._task_command_handler = handler

    def set_voice_event_handler(self, handler: Callable[[str, int], bool]) -> None:
        self._voice_event_handler = handler

    def start_web(self) -> None:
        if not self.web_enabled or self._server is not None:
            return
        preview = self

        class StreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path in {"/", "/index.html"}:
                    self._send_index()
                    return
                if path == "/snapshot.jpg":
                    self._send_snapshot()
                    return
                if path == "/stream.mjpg":
                    self._send_stream()
                    return
                if path == "/events":
                    self._send_events()
                    return
                if path == "/status.json":
                    self._send_status()
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/task":
                    self._receive_task()
                    return
                if path == "/task/stop":
                    self._receive_stop()
                    return
                if path == "/voice":
                    self._receive_voice()
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
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100%; background: #111315; color: #f4f6f8; font-family: Arial, sans-serif; }}
    body {{ min-height: 100vh; }}
    main {{ min-height: 100vh; display: grid; grid-template-rows: auto 1fr; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px; background: #1d2329; border-bottom: 1px solid #343d47; }}
    h1 {{ margin: 0; font-size: 18px; font-weight: 700; }}
    button, input {{ font: inherit; }}
    button {{ border: 1px solid #46515d; background: #28313a; color: #f4f6f8; border-radius: 6px; padding: 8px 11px; cursor: pointer; }}
    button.active {{ background: #18633c; border-color: #2f9c63; }}
    button.danger {{ background: #5c2028; border-color: #8d3945; }}
    input {{ min-width: 0; width: 100%; border: 1px solid #46515d; background: #101418; color: #f4f6f8; border-radius: 6px; padding: 9px 10px; }}
    .layout {{ display: grid; grid-template-rows: auto auto minmax(180px, 1fr); min-height: 0; }}
    .controls {{ display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; padding: 12px; background: #171b20; border-bottom: 1px solid #343d47; }}
    .stage {{ display: grid; place-items: center; padding: 12px; min-width: 0; background: #08090a; }}
    img {{ width: 100%; max-width: 1920px; max-height: 58vh; object-fit: contain; background: #000; }}
    .below {{ display: grid; grid-template-columns: minmax(0, 1fr) 420px; gap: 10px; padding: 12px; border-top: 1px solid #343d47; background: #171b20; min-height: 0; }}
    .status {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-content: start; }}
    .panel {{ border: 1px solid #343d47; border-radius: 8px; background: #20262d; padding: 10px; }}
    .label {{ margin: 0 0 6px; color: #aeb7c2; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ margin: 0; line-height: 1.35; overflow-wrap: anywhere; }}
    .wide {{ grid-column: 1 / -1; }}
    .log {{ min-height: 0; max-height: 34vh; overflow: auto; font-size: 13px; line-height: 1.35; }}
    .entry {{ padding: 7px 0; border-bottom: 1px solid #303842; }}
    .entry:last-child {{ border-bottom: 0; }}
    .meta {{ color: #9ea8b3; font-size: 11px; margin-bottom: 3px; }}
    @media (max-width: 980px) {{
      .controls, .below, .status {{ grid-template-columns: 1fr; }}
      img {{ max-height: 48vh; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{PREVIEW_WINDOW_NAME}</h1>
      <button id="voiceToggle" type="button">Enable voice</button>
    </header>
    <section class="layout">
      <form id="taskForm" class="controls">
        <input id="taskInput" type="text" autocomplete="off" placeholder="Enter task, e.g. open YouTube">
        <button id="sendTask" type="submit">Start task</button>
        <button id="stopTask" class="danger" type="button">Stop</button>
      </form>
      <div class="stage"><img src="/stream.mjpg" alt="Live stream"></div>
      <div class="below">
        <section class="status">
        <div class="panel wide">
          <p class="label">Task</p>
          <p id="task" class="value">No active task</p>
        </div>
        <div class="panel wide">
          <p class="label">AI decision</p>
          <p id="ai" class="value">Waiting for an AI decision</p>
        </div>
          <div class="panel">
            <p class="label">Plan</p>
            <p id="plan" class="value">none</p>
          </div>
          <div class="panel">
            <p class="label">IR</p>
            <p id="ir" class="value"></p>
          </div>
        <div class="panel wide">
          <p class="label">Current UI</p>
          <p id="ui" class="value"></p>
        </div>
          <div class="panel">
            <p class="label">View</p>
            <p id="view" class="value"></p>
          </div>
          <div class="panel">
            <p class="label">Device</p>
            <p id="device" class="value"></p>
          </div>
        </section>
        <section class="panel log" id="log"></section>
      </div>
    </section>
  </main>
  <script>
    const fields = {{
      task: document.getElementById("task"),
      ai: document.getElementById("ai"),
      plan: document.getElementById("plan"),
      ir: document.getElementById("ir"),
      ui: document.getElementById("ui"),
      view: document.getElementById("view"),
      device: document.getElementById("device"),
    }};
    const logEl = document.getElementById("log");
    const voiceButton = document.getElementById("voiceToggle");
    const taskForm = document.getElementById("taskForm");
    const taskInput = document.getElementById("taskInput");
    const stopTask = document.getElementById("stopTask");
    let voiceEnabled = false;

    function setText(id, value) {{
      if (fields[id] && value) fields[id].textContent = value;
    }}

    function postVoice(state, id) {{
      fetch("/voice", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ state, id: id || 0 }}),
      }}).catch(() => {{}});
    }}

    function speak(text, id) {{
      if (!voiceEnabled || !("speechSynthesis" in window) || !text) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.0;
      utterance.pitch = 1.0;
      utterance.onstart = () => postVoice("start", id);
      utterance.onend = () => postVoice("end", id);
      utterance.onerror = () => postVoice("end", id);
      window.speechSynthesis.speak(utterance);
    }}

    voiceButton.addEventListener("click", () => {{
      voiceEnabled = !voiceEnabled;
      voiceButton.classList.toggle("active", voiceEnabled);
      voiceButton.textContent = voiceEnabled ? "Voice on" : "Enable voice";
      postVoice(voiceEnabled ? "enabled" : "disabled", 0);
      if (voiceEnabled) speak("Voice enabled", 0);
      else if ("speechSynthesis" in window) {{
        window.speechSynthesis.cancel();
        postVoice("end", 0);
      }}
    }});

    async function postJson(url, body) {{
      const response = await fetch(url, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body || {{}}),
      }});
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }}

    taskForm.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const task = taskInput.value.trim();
      if (!task) return;
      setText("task", task);
      setText("ai", "Task sent from frontend");
      await postJson("/task", {{ task }});
      taskInput.value = "";
    }});

    stopTask.addEventListener("click", async () => {{
      setText("ai", "Stopping task");
      await postJson("/task/stop", {{}});
    }});

    function appendLog(event) {{
      const row = document.createElement("div");
      row.className = "entry";
      const time = new Date((event.time || Date.now() / 1000) * 1000).toLocaleTimeString();
      row.innerHTML = `<div class="meta">${{time}} | ${{event.kind || "log"}}</div><div></div>`;
      row.lastChild.textContent = event.message || "";
      logEl.prepend(row);
      while (logEl.children.length > 120) logEl.removeChild(logEl.lastChild);
    }}

    function applyEvent(event, options) {{
      const shouldSpeak = !options || options.speak !== false;
      const data = event.data || {{}};
      if (data.status) {{
        Object.entries(data.status).forEach(([key, value]) => setText(key, value));
      }}
      if (event.kind === "task") setText("task", event.message);
      if (event.kind === "ai") setText("ai", event.message);
      if (event.kind === "plan") setText("plan", event.message);
      if (event.kind === "ir") setText("ir", event.message);
      if (event.kind === "ui") setText("ui", event.message);
      if (event.kind === "view") setText("view", event.message);
      if (event.kind === "device") setText("device", event.message);
      appendLog(event);
      if (shouldSpeak && event.speak) speak(event.message, event.id);
    }}

    fetch("/status.json")
      .then((response) => response.ok ? response.json() : null)
      .then((snapshot) => {{
        if (!snapshot) return;
        Object.entries(snapshot.status || {{}}).forEach(([key, value]) => setText(key, value));
        (snapshot.recent_events || []).forEach((event) => applyEvent(event, {{ speak: false }}));
      }})
      .catch(() => {{}});

    const events = new EventSource("/events");
    events.addEventListener("log", (message) => {{
      try {{ applyEvent(JSON.parse(message.data)); }} catch (error) {{}}
    }});
  </script>
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

            def _send_status(self) -> None:
                data = json.dumps(preview.status_snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_json_body(self) -> dict[str, object]:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                if not raw:
                    return {}
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}
                return body if isinstance(body, dict) else {}

            def _send_json(self, status: int, payload: dict[str, object]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _receive_task(self) -> None:
                body = self._read_json_body()
                task = str(body.get("task") or "").strip()
                if not task:
                    self._send_json(400, {"ok": False, "error": "task is required"})
                    return
                if preview._task_command_handler is None or not preview._task_command_handler(task):
                    self._send_json(503, {"ok": False, "error": "task queue is not ready"})
                    return
                preview.publish_event(
                    "task",
                    f"Queued from frontend: {task}",
                    data={"status": {"task": task, "ai": "Queued from frontend", "plan": "observe"}},
                )
                self._send_json(200, {"ok": True})

            def _receive_stop(self) -> None:
                if preview._task_command_handler is None or not preview._task_command_handler("/stop"):
                    self._send_json(503, {"ok": False, "error": "task queue is not ready"})
                    return
                preview.publish_event(
                    "task",
                    "Stop requested from frontend",
                    data={"status": {"ai": "Stop requested", "plan": "stop"}},
                )
                self._send_json(200, {"ok": True})

            def _receive_voice(self) -> None:
                body = self._read_json_body()
                state = str(body.get("state") or "").strip().lower()
                event_id = int(body.get("id") or 0)
                if state not in {"enabled", "disabled", "start", "end"}:
                    self._send_json(400, {"ok": False, "error": "invalid voice state"})
                    return
                if preview._voice_event_handler is not None:
                    preview._voice_event_handler(state, event_id)
                self._send_json(200, {"ok": True})

            def _send_events(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                last_id = preview.current_event_id()
                while True:
                    events = preview.wait_for_events(after_id=last_id, timeout_seconds=15.0)
                    if not events:
                        try:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        continue
                    for event in events:
                        last_id = int(event.get("id") or last_id)
                        payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
                        try:
                            self.wfile.write(f"id: {last_id}\n".encode("ascii"))
                            self.wfile.write(b"event: log\n")
                            self.wfile.write(b"data: ")
                            self.wfile.write(payload)
                            self.wfile.write(b"\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return

        self._server = ThreadingHTTPServer((self.web_host, self.web_port), StreamHandler)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        print(f"[web] live stream: http://{self.web_host}:{self.web_port}", flush=True)

    def enhance(self, frame):
        import numpy as np

        if self.brightness == 1.0 and self.contrast == 1.0:
            return frame
        beta = int((self.brightness - 1.0) * 80)
        adjusted = frame.astype(np.float32) * self.contrast + beta
        return np.clip(adjusted, 0, 255).astype(np.uint8)

    def update_web_frame(self, jpeg_bytes: bytes) -> None:
        if not self.web_enabled:
            return
        with self._frame_condition:
            self._latest_jpeg = jpeg_bytes
            self._frame_version += 1
            self._frame_condition.notify_all()

    def publish_event(
        self,
        kind: str,
        message: str,
        *,
        speak: bool = False,
        data: dict[str, object] | None = None,
    ) -> None:
        if not self.web_enabled:
            return
        clean_kind = str(kind or "log").strip().lower() or "log"
        clean_message = str(message or "").strip()
        event_data = dict(data or {})
        with self._event_condition:
            self._event_id += 1
            status_updates = event_data.get("status")
            if isinstance(status_updates, dict):
                self._status.update(status_updates)
            elif clean_kind in self._status:
                self._status[clean_kind] = clean_message
            self._status["updated_at"] = time.time()
            event = {
                "id": self._event_id,
                "time": time.time(),
                "kind": clean_kind,
                "message": clean_message,
                "speak": bool(speak),
                "data": event_data,
            }
            self._events.append(event)
            self._events = self._events[-250:]
            self._event_condition.notify_all()

    def current_event_id(self) -> int:
        with self._event_condition:
            return self._event_id

    def wait_for_events(self, *, after_id: int, timeout_seconds: float) -> list[dict[str, object]]:
        with self._event_condition:
            deadline = time.monotonic() + timeout_seconds
            while self._event_id <= after_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._event_condition.wait(timeout=remaining)
            return [event for event in self._events if int(event.get("id") or 0) > after_id]

    def status_snapshot(self) -> dict[str, object]:
        with self._event_condition:
            return {
                "status": dict(self._status),
                "recent_events": list(self._events[-50:]),
            }

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
    img.thumbnail([preview.frame_max_size, preview.frame_max_size], _LANCZOS)

    image_io = io.BytesIO()
    img.save(image_io, format="jpeg", quality=preview.jpeg_quality, optimize=True)
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
    preview_image.thumbnail([preview.frame_max_size, preview.frame_max_size], _LANCZOS)
    preview_image.save(image_bytes, format="jpeg", quality=preview.jpeg_quality, optimize=True)
    jpeg_bytes = image_bytes.getvalue()
    preview.update_web_frame(jpeg_bytes)
    return preview_frame, jpeg_bytes


FrameCallback = Callable[[bytes], Awaitable[None]]


async def stream_camera(on_frame: FrameCallback, preview: PreviewWindow) -> None:
    import cv2

    preview.start_web()
    cap = await asyncio.to_thread(cv2.VideoCapture, 0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, getattr(preview, "video_width", 1280))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, getattr(preview, "video_height", 720))
    cap.set(cv2.CAP_PROP_FPS, 30)
    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    print(
        f"[video] camera={actual_width}x{actual_height} encode_max={preview.frame_max_size} jpeg_quality={preview.jpeg_quality}",
        flush=True,
    )
    last_sent = 0.0
    send_interval = preview.send_interval_seconds
    try:
        while True:
            frame_result = await asyncio.to_thread(_camera_frame, cap, preview)
            if frame_result is None:
                return
            preview_frame, image_bytes = frame_result
            preview.show(preview_frame)
            now = time.monotonic()
            if send_interval <= 0 or (now - last_sent) >= send_interval:
                last_sent = now
                await on_frame(image_bytes)
            await asyncio.sleep(0.01)
    finally:
        cap.release()


async def stream_screen(on_frame: FrameCallback, preview: PreviewWindow) -> None:
    preview.start_web()
    last_sent = 0.0
    send_interval = preview.send_interval_seconds
    while True:
        frame_result = await asyncio.to_thread(_screen_frame, preview)
        if frame_result is None:
            return
        preview_frame, image_bytes = frame_result
        preview.show(preview_frame)
        now = time.monotonic()
        if send_interval <= 0 or (now - last_sent) >= send_interval:
            last_sent = now
            await on_frame(image_bytes)
        await asyncio.sleep(0.05)
