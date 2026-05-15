"""Live session orchestration."""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from dataclasses import dataclass

from google import genai
from google.genai import types

from .audio import AudioRuntime
from .config import AppConfig, build_live_config
from .live_control import LiveControlExecutor
from .media import PreviewWindow, stream_camera, stream_screen


@dataclass
class AgentTaskState:
    user_request: str
    generation: int
    step_index: int = 0
    last_error: str | None = None
    last_visual_state: str | None = None
    last_command_signature: str | None = None
    last_directional_command_signature: str | None = None
    directional_streak_count: int = 0
    command_history: list[str] | None = None
    visual_history: list[str] | None = None
    device_profile: dict[str, object] | None = None
    ir_device_id: str | None = None
    repeated_same_command_count: int = 0
    awaiting_validation: bool = False


class LiveApp:
    def __init__(self, client: genai.Client, config: AppConfig):
        self.client = client
        self.config = config
        self.live_config = build_live_config(audio_enabled=config.audio_enabled)
        self.audio = AudioRuntime()
        self.audio_enabled = self.audio.enabled(config.audio_enabled)
        self.preview = PreviewWindow(
            enabled=config.preview_enabled and config.local_preview_enabled,
            brightness=config.visual_brightness,
            contrast=config.visual_contrast,
            web_enabled=config.web_stream_enabled,
            web_host=config.web_stream_host,
            web_port=config.web_stream_port,
            frame_max_size=config.video_frame_max_size,
            jpeg_quality=config.video_jpeg_quality,
            send_interval_seconds=config.video_send_interval_seconds,
        )
        self.preview.video_width = config.video_width
        self.preview.video_height = config.video_height
        self.control = LiveControlExecutor(config=config)
        self.audio_in_queue: asyncio.Queue | None = None
        self.out_queue: asyncio.Queue | None = None
        self.prompt_queue: asyncio.Queue | None = None
        self.user_input_queue: asyncio.Queue | None = None
        self.session = None
        self.debug = os.environ.get("LIVE_DEBUG") == "1"
        self.active_task: AgentTaskState | None = None
        self.running = True
        self.task_generation = 0
        self.pending_prompt_sent_at: float | None = None
        self.prompt_sequence = 0
        self.latest_video_blob: types.Blob | None = None
        self.latest_video_version = 0
        self.sent_video_version = 0
        self.last_video_sent_at = 0.0
        self.video_condition = asyncio.Condition()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.voice_complete = asyncio.Event()
        self.voice_complete.set()
        self.frontend_voice_enabled = False
        self.voice_started_at: float | None = None
        self.voice_event_id = 0

    @staticmethod
    def _is_live_session_end_error(exc: Exception) -> bool:
        detail = str(exc).lower()
        return (
            "goaway" in detail
            or "session duration" in detail
            or "failed to close the connection after receiving" in detail
            or "connection aborted" in detail
        )

    def _frontend_event(
        self,
        kind: str,
        message: str,
        *,
        speak: bool = False,
        data: dict[str, object] | None = None,
    ) -> None:
        try:
            self.preview.publish_event(kind, message, speak=speak, data=data)
        except Exception:
            if self.debug:
                traceback.print_exc()

    def _submit_frontend_task(self, text: str) -> bool:
        if not self.running or self.loop is None or self.user_input_queue is None:
            return False
        try:
            self.loop.call_soon_threadsafe(self.user_input_queue.put_nowait, text)
        except RuntimeError:
            return False
        return True

    def _handle_frontend_voice_event(self, state: str, event_id: int) -> bool:
        if self.loop is None:
            return False

        def apply_voice_event() -> None:
            if state == "enabled":
                self.frontend_voice_enabled = True
                if self.debug:
                    print("[voice] frontend voice enabled", flush=True)
            elif state == "disabled":
                self.frontend_voice_enabled = False
                self.voice_started_at = None
                self.voice_complete.set()
                if self.debug:
                    print("[voice] frontend voice disabled", flush=True)
            elif state == "start":
                self.voice_event_id = max(self.voice_event_id, event_id)
                self.voice_started_at = time.monotonic()
                self.voice_complete.clear()
                if self.debug:
                    print(f"[voice] started event={event_id}", flush=True)
            elif state == "end":
                if event_id >= self.voice_event_id:
                    self.voice_started_at = None
                    self.voice_complete.set()
                    if self.debug:
                        print(f"[voice] completed event={event_id}", flush=True)

        try:
            self.loop.call_soon_threadsafe(apply_voice_event)
        except RuntimeError:
            return False
        return True

    async def _wait_for_frontend_voice(self) -> None:
        if not self.frontend_voice_enabled:
            return
        if self.voice_complete.is_set():
            await asyncio.sleep(0.25)
        if self.voice_complete.is_set():
            return
        timeout_seconds = 12.0
        print("[voice] waiting for frontend speech to finish", flush=True)
        self._frontend_event("log", "Waiting for frontend voice to finish before next prompt")
        try:
            await asyncio.wait_for(self.voice_complete.wait(), timeout=timeout_seconds)
        except TimeoutError:
            print("[voice] timeout waiting for frontend speech; continuing", flush=True)
            self._frontend_event("log", "Voice wait timeout; continuing")
            self.voice_started_at = None
            self.voice_complete.set()

    def _handle_live_send_error(self, exc: Exception, *, context: str) -> None:
        detail = str(exc).strip() or exc.__class__.__name__
        if self._is_live_session_end_error(exc):
            message = "Gemini live session ended. Restart the app to open a fresh live session."
            print(f"[session] {message}", flush=True)
            self._frontend_event(
                "guard",
                message,
                speak=True,
                data={"status": {"ai": "Live session ended", "plan": "restart live session"}},
            )
            self.active_task = None
            self._clear_prompt_queue()
            self.running = False
            return
        print(f"[error] live send failed during {context}: {detail}", flush=True)
        self._frontend_event(
            "guard",
            f"Live session stopped during {context}: {detail}",
            speak=True,
            data={"status": {"ai": "Live session stopped", "plan": "check quota or connection"}},
        )
        self.running = False

    async def send_prompt_text(self, text: str) -> None:
        if self.session is None:
            return
        observe_delay = max(0.0, self.config.agent_observe_seconds)
        if observe_delay:
            print(f"[observe] waiting {observe_delay:.1f}s for fresh video", flush=True)
            self._frontend_event("log", f"Waiting {observe_delay:.1f}s for fresh video before asking AI")
            await asyncio.sleep(observe_delay)
        await self.send_current_video_frame(reason="before prompt")
        if not self.running:
            return
        self.prompt_sequence += 1
        self.pending_prompt_sent_at = time.monotonic()
        print(f"[ai] prompt #{self.prompt_sequence} sent", flush=True)
        self._frontend_event("log", f"AI prompt #{self.prompt_sequence} sent")
        try:
            await self.session.send_realtime_input(text=text or ".")
            await self.session.send_realtime_input(activity_end={})
        except Exception as exc:
            self._handle_live_send_error(exc, context="prompt")

    async def read_user_input(self) -> None:
        while True:
            text = await asyncio.to_thread(input, "message > ")
            if self.user_input_queue is not None:
                await self.user_input_queue.put(text)

    def _clear_prompt_queue(self) -> None:
        if self.prompt_queue is None:
            return
        while True:
            try:
                self.prompt_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _stop_active_task(self, *, reason: str) -> None:
        self.task_generation += 1
        self.active_task = None
        self._clear_prompt_queue()
        print(f"[task] {reason}", flush=True)
        self._frontend_event(
            "task",
            f"Task {reason}",
            speak=reason in {"completed", "needs_confirmation", "ir_execution_disabled"},
            data={"status": {"task": f"Task {reason}", "plan": "none"}},
        )

    @staticmethod
    def _normalize_visual_state(value: object) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _command_signature(parsed: dict) -> str:
        commands = parsed.get("commands")
        if not isinstance(commands, list):
            return ""
        parts: list[str] = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().upper()
            repeats = str(item.get("repeats") or "1").strip()
            key = str(item.get("key") or "").strip().upper()
            parts.append(f"{action}:{key}:{repeats}")
        return "|".join(parts)

    @staticmethod
    def _planned_keys_text(parsed: dict) -> str:
        commands = parsed.get("commands")
        if not isinstance(commands, list) or not commands:
            return "none"
        labels: list[str] = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().upper()
            repeats = int(item.get("repeats") or 1)
            key = str(item.get("key") or "").strip().upper()
            label = key or action
            if repeats > 1:
                label = f"{label} x{repeats}"
            labels.append(label)
        return ", ".join(labels) if labels else "none"

    async def update_latest_video_frame(self, image_bytes: bytes) -> None:
        blob = types.Blob(data=image_bytes, mime_type="image/jpeg")
        async with self.video_condition:
            self.latest_video_blob = blob
            self.latest_video_version += 1
            self.video_condition.notify_all()

    async def send_current_video_frame(self, *, reason: str = "latest") -> bool:
        if self.session is None or self.config.video_mode not in {"camera", "screen"}:
            return False
        async with self.video_condition:
            if self.latest_video_blob is None:
                try:
                    await asyncio.wait_for(self.video_condition.wait(), timeout=1.0)
                except TimeoutError:
                    return False
            blob = self.latest_video_blob
            version = self.latest_video_version
        if blob is None:
            return False
        try:
            await self.session.send_realtime_input(video=blob)
        except Exception as exc:
            self._handle_live_send_error(exc, context=reason)
            return False
        self.last_video_sent_at = time.monotonic()
        self.sent_video_version = version
        if self.debug:
            print(f"[video] sent fresh frame v{version} ({reason})", flush=True)
        return True

    @staticmethod
    def _detect_repeated_route(history: list[str]) -> tuple[int, list[str]] | None:
        for length in range(2, 7):
            if len(history) < length * 2:
                continue
            first = history[-length * 2 : -length]
            second = history[-length:]
            if first == second:
                return length, second
        return None

    @staticmethod
    def _append_bounded(history: list[str] | None, item: str, *, limit: int = 18) -> list[str]:
        values = list(history or [])
        if item:
            values.append(item)
        return values[-limit:]

    def _print_agent_status(self, parsed: dict[str, object], *, execution: dict | None) -> None:
        summary = str(parsed.get("summary") or "").strip()
        visual_state = str(parsed.get("visual_state") or "").strip()
        ui_state = self.control.extract_ui_state(parsed)
        device_profile = self.control.extract_device_profile(parsed)
        planned = self._planned_keys_text(parsed)
        print(f"[agent] {summary}", flush=True)
        if visual_state:
            print(f"[view] {visual_state}", flush=True)
            self._frontend_event("view", visual_state, data={"status": {"view": visual_state}})
        if ui_state:
            ui_text = self.control.format_ui_state(ui_state)
            print(f"[ui] {ui_text}", flush=True)
            self._frontend_event("ui", ui_text, data={"status": {"ui": ui_text}})
        if device_profile:
            device_text = self.control.format_device_profile(device_profile)
            print(f"[device] {device_text}", flush=True)
            self._frontend_event("device", device_text, data={"status": {"device": device_text}})
        print(f"[plan] {planned}", flush=True)
        self._frontend_event("plan", planned, data={"status": {"plan": planned}})
        if execution is None:
            return
        if bool(execution.get("ok")):
            device_id = execution.get("device_id")
            suffix = f" using {device_id}" if device_id else ""
            ir_text = f"sent via {execution.get('serial_port')}{suffix}"
            print(f"[ir] {ir_text}", flush=True)
            self._frontend_event("ir", ir_text, data={"status": {"ir": ir_text}})
        elif bool(execution.get("dry_run")):
            ir_text = f"dry-run: {execution.get('detail')}"
            print(f"[ir] {ir_text}", flush=True)
            self._frontend_event("ir", ir_text, data={"status": {"ir": ir_text}})
        else:
            ir_text = f"failed: {execution.get('detail')}"
            print(f"[ir] {ir_text}", flush=True)
            self._frontend_event("ir", ir_text, speak=True, data={"status": {"ir": ir_text}})

    def _print_ai_decision_summary(self, parsed: dict[str, object], *, elapsed_seconds: float | None) -> None:
        elapsed = f"{elapsed_seconds:.2f}s" if elapsed_seconds is not None else "unknown"
        summary = str(parsed.get("summary") or "").strip() or "No summary"
        status = str(parsed.get("task_status") or "UNKNOWN").strip().upper()
        goal_state = self.control.extract_goal_state(parsed)
        subplan = self.control.extract_subplan(parsed)
        ui_state = self.control.extract_ui_state(parsed)
        device_profile = self.control.extract_device_profile(parsed)
        planned = self._planned_keys_text(parsed)
        print(f"[ai] response #{self.prompt_sequence} in {elapsed}", flush=True)
        print(f"[ai] decision: {summary}", flush=True)
        print(f"[ai] status={status} plan={planned}", flush=True)
        spoken = summary
        if planned and planned != "none":
            spoken = f"{summary}. Next action: {planned}."
        self._frontend_event(
            "ai",
            spoken,
            speak=True,
            data={
                "status": {
                    "ai": f"{summary} ({elapsed})",
                    "plan": planned,
                },
                "elapsed_seconds": elapsed_seconds,
                "task_status": status,
            },
        )
        if goal_state:
            goal_text = self.control.format_goal_state(goal_state)
            print(f"[ai] goal: {goal_text}", flush=True)
            self._frontend_event("goal", goal_text)
        if subplan:
            subplan_text = self.control.format_subplan(subplan)
            print(f"[ai] subplan: {subplan_text}", flush=True)
            self._frontend_event("subplan", subplan_text)
        if ui_state:
            ui_text = self.control.format_ui_state(ui_state)
            print(f"[ai] ui: {ui_text}", flush=True)
            self._frontend_event("ui", ui_text, data={"status": {"ui": ui_text}})
        if device_profile:
            device_text = self.control.format_device_profile(device_profile)
            print(f"[ai] device: {device_text}", flush=True)
            self._frontend_event("device", device_text, data={"status": {"device": device_text}})

    async def _enqueue_followup(self, prompt: str, *, generation: int, delay_seconds: float | None = None) -> None:
        if self.prompt_queue is None or not prompt.strip():
            return
        await self._wait_for_frontend_voice()
        delay = self.config.agent_step_delay_seconds if delay_seconds is None else delay_seconds
        if delay > 0:
            await asyncio.sleep(delay)
        if self.active_task is None or self.active_task.generation != generation:
            return
        await self.prompt_queue.put((generation, prompt))

    async def agent_loop(self) -> None:
        while self.running:
            queue_getters: dict[asyncio.Task, str] = {}
            if self.prompt_queue is not None:
                queue_getters[asyncio.create_task(self.prompt_queue.get())] = "prompt"
            if self.user_input_queue is not None:
                queue_getters[asyncio.create_task(self.user_input_queue.get())] = "user"
            if not queue_getters:
                await asyncio.sleep(0.05)
                continue

            done, pending = await asyncio.wait(
                queue_getters.keys(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            completed = next(iter(done))
            value = completed.result()
            origin = queue_getters[completed]

            if origin == "prompt":
                generation, queued_prompt = value
                if self.active_task and self.active_task.generation == generation:
                    print("[auto] validating screen and choosing next action", flush=True)
                    await self.send_prompt_text(queued_prompt)
                continue

            text = str(value or "").strip()
            lowered = text.lower()
            if lowered == "q":
                self.running = False
                return
            if lowered in {"stop", "/stop", "cancel", "/cancel"}:
                if self.active_task is None:
                    continue
                self._stop_active_task(reason="stopped")
                continue
            if not text:
                continue
            self.task_generation += 1
            self.active_task = AgentTaskState(user_request=text, generation=self.task_generation)
            self._clear_prompt_queue()
            print(f"[task] started: {text}", flush=True)
            self._frontend_event(
                "task",
                f"Started: {text}",
                speak=True,
                data={"status": {"task": text, "ai": "Observing current screen", "plan": "observe"}},
            )
            await self.send_prompt_text(self.control.build_initial_task_prompt(text))

    async def send_realtime(self) -> None:
        while True:
            if self.out_queue is None or self.session is None:
                await asyncio.sleep(0.05)
                continue
            kind, msg = await self.out_queue.get()
            if kind == "audio":
                try:
                    await self.session.send_realtime_input(audio=msg)
                except Exception as exc:
                    self._handle_live_send_error(exc, context="audio")
                    return

    async def send_latest_video(self) -> None:
        while True:
            if self.session is None or self.config.video_mode not in {"camera", "screen"}:
                await asyncio.sleep(0.05)
                continue
            min_interval = max(0.5, self.config.video_send_interval_seconds)
            async with self.video_condition:
                while self.latest_video_version <= self.sent_video_version:
                    await self.video_condition.wait()
                blob = self.latest_video_blob
                version = self.latest_video_version
            if blob is None:
                continue
            elapsed = time.monotonic() - self.last_video_sent_at
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            async with self.video_condition:
                blob = self.latest_video_blob
                version = self.latest_video_version
            if blob is None or version <= self.sent_video_version:
                continue
            try:
                await self.session.send_realtime_input(video=blob)
            except Exception as exc:
                self._handle_live_send_error(exc, context="video")
                return
            self.last_video_sent_at = time.monotonic()
            self.sent_video_version = version
            if self.debug:
                print(f"[video] sent latest frame v{version}", flush=True)

    async def listen_audio(self) -> None:
        while True:
            data = await self.audio.read_chunk()
            if self.out_queue is not None:
                await self.out_queue.put(("audio", self.audio.build_blob(data)))

    async def receive_responses(self) -> None:
        while True:
            if self.session is None:
                await asyncio.sleep(0.05)
                continue

            turn = self.session.receive()
            turn_text_parts: list[str] = []
            latest_transcription_text = ""
            async for response in turn:
                if self.debug:
                    print(f"[debug] response={response}", flush=True)
                if data := response.data:
                    if self.audio_enabled and self.audio_in_queue is not None:
                        self.audio_in_queue.put_nowait(data)
                        continue
                if text := response.text:
                    turn_text_parts.append(text)
                    if self.debug:
                        print(text, end="")
                server_content = getattr(response, "server_content", None)
                if not server_content:
                    continue
                output_transcription = getattr(server_content, "output_transcription", None)
                if output_transcription and output_transcription.text:
                    latest_transcription_text = output_transcription.text
                    if self.debug:
                        print(output_transcription.text, end="", flush=True)
                if output_transcription and getattr(output_transcription, "finished", False):
                    if self.debug:
                        print(flush=True)
                if getattr(server_content, "waiting_for_input", False) and not output_transcription:
                    print("[waiting for more input]", flush=True)

            candidate_text = "".join(turn_text_parts).strip() or latest_transcription_text.strip()
            if candidate_text and self.active_task is not None:
                elapsed_seconds: float | None = None
                if self.pending_prompt_sent_at is None:
                    elapsed_seconds = None
                else:
                    elapsed_seconds = time.monotonic() - self.pending_prompt_sent_at
                    self.pending_prompt_sent_at = None
                task_state = self.active_task
                parsed = self.control.parse_response(candidate_text)
                if parsed is None:
                    elapsed = f"{elapsed_seconds:.2f}s" if elapsed_seconds is not None else "unknown"
                    print(f"[ai] response #{self.prompt_sequence} in {elapsed}", flush=True)
                    print("[ai] decision: invalid JSON response; requesting repair", flush=True)
                    self._frontend_event(
                        "ai",
                        f"Invalid AI response after {elapsed}; requesting repair",
                        speak=True,
                        data={"status": {"ai": f"Invalid response ({elapsed})", "plan": "repair"}},
                    )
                    task_state.last_error = "Live model response was not valid JSON."
                    task_state.awaiting_validation = False
                    task_state.step_index += 1
                    if task_state.step_index >= self.config.agent_max_steps:
                        self._stop_active_task(reason="max_steps_reached")
                    else:
                        repair_prompt = self.control.build_repair_prompt(
                            task_state.user_request,
                            step_index=task_state.step_index,
                            max_steps=self.config.agent_max_steps,
                            problem=task_state.last_error,
                            device_profile=task_state.device_profile,
                        )
                        await self._enqueue_followup(
                            repair_prompt,
                            generation=task_state.generation,
                        )
                    continue

                self._print_ai_decision_summary(parsed, elapsed_seconds=elapsed_seconds)

                current_profile = self.control.extract_device_profile(parsed)
                if current_profile:
                    task_state.device_profile = current_profile
                    inferred_device_id = self.control.inferred_device_id(current_profile)
                    if inferred_device_id:
                        task_state.ir_device_id = inferred_device_id

                command_signature = self._command_signature(parsed)
                visual_state = self._normalize_visual_state(parsed.get("visual_state"))
                plan_error = self.control.validate_goal_subplan(parsed)
                if plan_error:
                    task_state.last_error = plan_error
                    task_state.awaiting_validation = False
                    task_state.step_index += 1
                    print(f"[guard] blocked unvalidated plan: {plan_error}", flush=True)
                    self._frontend_event(
                        "guard",
                        f"Blocked unvalidated plan: {plan_error}",
                        speak=True,
                        data={"status": {"ai": "Plan blocked for validation", "plan": "repair"}},
                    )
                    if task_state.step_index >= self.config.agent_max_steps:
                        self._stop_active_task(reason="max_steps_reached")
                    else:
                        followup = self.control.build_repair_prompt(
                            task_state.user_request,
                            step_index=task_state.step_index,
                            max_steps=self.config.agent_max_steps,
                            problem=task_state.last_error,
                            device_profile=task_state.device_profile,
                        )
                        await self._enqueue_followup(
                            followup,
                            generation=task_state.generation,
                        )
                    continue

                plan_error = self.control.validate_app_launch_plan(
                    task_state.user_request,
                    parsed,
                    command_signature,
                )
                if plan_error:
                    task_state.last_error = plan_error
                    task_state.awaiting_validation = False
                    task_state.step_index += 1
                    print(f"[guard] blocked weak app plan: {plan_error}", flush=True)
                    self._frontend_event(
                        "guard",
                        f"Blocked weak app plan: {plan_error}",
                        speak=True,
                        data={"status": {"ai": "Weak app plan blocked", "plan": "repair"}},
                    )
                    if task_state.step_index >= self.config.agent_max_steps:
                        self._stop_active_task(reason="max_steps_reached")
                    else:
                        followup = self.control.build_repair_prompt(
                            task_state.user_request,
                            step_index=task_state.step_index,
                            max_steps=self.config.agent_max_steps,
                            problem=task_state.last_error,
                            device_profile=task_state.device_profile,
                        )
                        await self._enqueue_followup(
                            followup,
                            generation=task_state.generation,
                        )
                    continue

                projected_command_history = self._append_bounded(
                    task_state.command_history,
                    command_signature,
                )
                repeated_route = self._detect_repeated_route(projected_command_history)
                if repeated_route is not None:
                    _, route = repeated_route
                    task_state.last_error = (
                        "Navigation route loop guard: the same command route repeated: "
                        + " -> ".join(route)
                        + ". Do not replay this route. Use a new strategy such as Search/Apps or mark blocked."
                    )
                    task_state.awaiting_validation = False
                    task_state.step_index += 1
                    task_state.command_history = projected_command_history
                    task_state.visual_history = self._append_bounded(task_state.visual_history, visual_state)
                    print(f"[guard] blocked repeated route: {' -> '.join(route)}", flush=True)
                    self._frontend_event(
                        "guard",
                        f"Blocked repeated route: {' -> '.join(route)}",
                        speak=True,
                        data={"status": {"ai": "Repeated route blocked", "plan": "repair"}},
                    )
                    if task_state.step_index >= self.config.agent_max_steps:
                        self._stop_active_task(reason="max_steps_reached")
                    else:
                        followup = self.control.build_repair_prompt(
                            task_state.user_request,
                            step_index=task_state.step_index,
                            max_steps=self.config.agent_max_steps,
                            problem=task_state.last_error,
                            device_profile=task_state.device_profile,
                        )
                        await self._enqueue_followup(
                            followup,
                            generation=task_state.generation,
                        )
                    continue

                if self.control.is_directional_command_signature(command_signature):
                    if task_state.last_directional_command_signature == command_signature:
                        next_directional_streak = task_state.directional_streak_count + 1
                    else:
                        next_directional_streak = 1
                    if next_directional_streak > 3:
                        task_state.last_error = (
                            "Navigation loop guard: the same directional key was requested more than three times. "
                            "Stop scanning in that direction, identify the focused item from the live image, and choose a different strategy."
                        )
                        task_state.awaiting_validation = False
                        task_state.step_index += 1
                        print(f"[guard] blocked repeated navigation: {command_signature}", flush=True)
                        self._frontend_event(
                            "guard",
                            f"Blocked repeated navigation: {command_signature}",
                            speak=True,
                            data={"status": {"ai": "Repeated navigation blocked", "plan": "repair"}},
                        )
                        if task_state.step_index >= self.config.agent_max_steps:
                            self._stop_active_task(reason="max_steps_reached")
                        else:
                            followup = self.control.build_repair_prompt(
                                task_state.user_request,
                                step_index=task_state.step_index,
                                max_steps=self.config.agent_max_steps,
                                problem=task_state.last_error,
                                device_profile=task_state.device_profile,
                            )
                            await self._enqueue_followup(
                                followup,
                                generation=task_state.generation,
                            )
                        continue
                    task_state.last_directional_command_signature = command_signature
                    task_state.directional_streak_count = next_directional_streak
                elif command_signature:
                    task_state.last_directional_command_signature = None
                    task_state.directional_streak_count = 0

                execution = self.control.execute_from_response(
                    parsed,
                    device_id_override=task_state.ir_device_id,
                )
                self._print_agent_status(parsed, execution=execution)

                if execution is not None and bool(execution.get("dry_run")):
                    self._stop_active_task(reason="ir_execution_disabled")
                    continue

                if self.control.task_completed(parsed):
                    self._stop_active_task(reason="completed")
                    continue

                if self.control.needs_confirmation(parsed):
                    self._stop_active_task(reason="needs_confirmation")
                    continue

                if (
                    visual_state
                    and task_state.last_visual_state == visual_state
                    and command_signature
                    and task_state.last_command_signature == command_signature
                ):
                    task_state.repeated_same_command_count += 1
                else:
                    task_state.repeated_same_command_count = 0
                task_state.last_visual_state = visual_state or task_state.last_visual_state
                task_state.last_command_signature = command_signature or task_state.last_command_signature
                task_state.command_history = projected_command_history
                task_state.visual_history = self._append_bounded(task_state.visual_history, visual_state)

                task_state.step_index += 1
                if execution is not None and not bool(execution.get("ok")):
                    task_state.last_error = str(execution.get("detail") or "IR execution failed.")
                    task_state.awaiting_validation = False
                else:
                    task_state.last_error = None

                if task_state.step_index >= self.config.agent_max_steps:
                    self._stop_active_task(reason="max_steps_reached")
                    continue

                if task_state.repeated_same_command_count >= 2:
                    task_state.last_error = (
                        "The visible UI did not change after repeating the same action. "
                        "Choose a different recovery step or mark the task blocked."
                    )
                    followup = self.control.build_repair_prompt(
                        task_state.user_request,
                        step_index=task_state.step_index,
                        max_steps=self.config.agent_max_steps,
                        problem=task_state.last_error,
                        device_profile=task_state.device_profile,
                    )
                    await self._enqueue_followup(
                        followup,
                        generation=task_state.generation,
                    )
                    continue

                if execution is not None and bool(execution.get("ok")):
                    task_state.awaiting_validation = True
                    print("[wait] waiting for UI to settle before validation", flush=True)
                    self._frontend_event("log", "Waiting for UI to settle before validation")
                    followup = self.control.build_validation_prompt(
                        task_state.user_request,
                        parsed,
                        execution,
                        step_index=task_state.step_index,
                        max_steps=self.config.agent_max_steps,
                        device_profile=task_state.device_profile,
                    )
                    await self._enqueue_followup(
                        followup,
                        generation=task_state.generation,
                        delay_seconds=self.config.agent_ui_settle_seconds,
                    )
                    continue

                task_state.awaiting_validation = False

                followup = self.control.build_followup_prompt(
                    task_state.user_request,
                    parsed,
                    execution,
                    step_index=task_state.step_index,
                    max_steps=self.config.agent_max_steps,
                    last_error=task_state.last_error,
                    device_profile=task_state.device_profile,
                )
                await self._enqueue_followup(
                    followup,
                    generation=task_state.generation,
                )

            while self.audio_in_queue is not None and not self.audio_in_queue.empty():
                self.audio_in_queue.get_nowait()

    async def play_audio(self) -> None:
        while True:
            if self.audio_in_queue is None:
                await asyncio.sleep(0.05)
                continue
            bytestream = await self.audio_in_queue.get()
            await self.audio.write_chunk(bytestream)

    async def stream_video(self) -> None:
        if self.config.video_mode == "camera":
            await stream_camera(self.update_latest_video_frame, self.preview)
        elif self.config.video_mode == "screen":
            await stream_screen(self.update_latest_video_frame, self.preview)

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            self.loop = asyncio.get_running_loop()
            if self.config.execute_ir:
                probe = await asyncio.to_thread(self.control.probe_ir)
                if bool(probe.get("success")):
                    print(f"[ir] NodeMCU serial detected on {probe.get('port')}", flush=True)
                    self._frontend_event(
                        "ir",
                        f"NodeMCU serial detected on {probe.get('port')}",
                        data={"status": {"ir": f"ready on {probe.get('port')}"}},
                    )
                else:
                    print(f"[ir] NodeMCU serial probe failed: {probe.get('error')}", flush=True)
                    self._frontend_event(
                        "ir",
                        f"NodeMCU serial probe failed: {probe.get('error')}",
                        speak=True,
                        data={"status": {"ir": "probe failed"}},
                    )
                    return
            else:
                print("[ir] dry-run mode: planned keys will not be sent; add --execute-ir to control the TV", flush=True)
                self._frontend_event(
                    "ir",
                    "dry-run mode: planned keys will not be sent",
                    data={"status": {"ir": "dry-run"}},
                )

            print(
                f"[video] brightness={self.config.visual_brightness} contrast={self.config.visual_contrast}",
                flush=True,
            )
            video_mode_text = (
                "latest-frame mode: no video queue, "
                f"fresh frame before each prompt, background interval={self.config.video_send_interval_seconds:.2f}s"
            )
            print(f"[video] {video_mode_text}", flush=True)
            self._frontend_event(
                "log",
                f"Video brightness={self.config.visual_brightness} contrast={self.config.visual_contrast}; {video_mode_text}",
            )

            async with self.client.aio.live.connect(
                model=self.config.model,
                config=self.live_config,
            ) as session:
                self.session = session
                self.audio_in_queue = asyncio.Queue()
                self.out_queue = asyncio.Queue(maxsize=20)
                if self.prompt_queue is None:
                    self.prompt_queue = asyncio.Queue()
                if self.user_input_queue is None:
                    self.user_input_queue = asyncio.Queue()
                self.preview.set_task_command_handler(self._submit_frontend_task)
                self.preview.set_voice_event_handler(self._handle_frontend_voice_event)

                tasks.append(asyncio.create_task(self.send_latest_video()))
                if self.config.video_mode in {"camera", "screen"}:
                    tasks.append(asyncio.create_task(self.stream_video()))
                tasks.append(asyncio.create_task(self.read_user_input()))
                agent_task = asyncio.create_task(self.agent_loop())
                tasks.append(agent_task)
                tasks.append(asyncio.create_task(self.send_realtime()))
                tasks.append(asyncio.create_task(self.receive_responses()))
                if self.audio_enabled:
                    tasks.append(asyncio.create_task(self.listen_audio()))
                    tasks.append(asyncio.create_task(self.play_audio()))

                await agent_task
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self.running = False
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.audio.close()
            self.preview.close()
