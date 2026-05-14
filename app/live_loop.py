"""Live session orchestration."""

from __future__ import annotations

import asyncio
import os
import traceback
from dataclasses import dataclass

from google import genai

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
        )
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

    async def send_prompt_text(self, text: str) -> None:
        if self.session is None:
            return
        await self.session.send_realtime_input(text=text or ".")
        await self.session.send_realtime_input(activity_end={})

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

    def _print_agent_status(self, parsed: dict[str, object], *, execution: dict | None) -> None:
        summary = str(parsed.get("summary") or "").strip()
        visual_state = str(parsed.get("visual_state") or "").strip()
        planned = self._planned_keys_text(parsed)
        print(f"[agent] {summary}", flush=True)
        if visual_state:
            print(f"[view] {visual_state}", flush=True)
        print(f"[plan] {planned}", flush=True)
        if execution is None:
            return
        if bool(execution.get("ok")):
            print(f"[ir] sent via {execution.get('serial_port')}", flush=True)
        else:
            print(f"[ir] failed: {execution.get('detail')}", flush=True)

    async def _enqueue_followup(self, prompt: str, *, generation: int, delay_seconds: float | None = None) -> None:
        if self.prompt_queue is None or not prompt.strip():
            return
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
                self._stop_active_task(reason="stopped")
                continue
            if not text:
                continue
            self.task_generation += 1
            self.active_task = AgentTaskState(user_request=text, generation=self.task_generation)
            self._clear_prompt_queue()
            print(f"[task] started: {text}", flush=True)
            await self.send_prompt_text(self.control.build_initial_task_prompt(text))

    async def send_realtime(self) -> None:
        while True:
            if self.out_queue is None or self.session is None:
                await asyncio.sleep(0.05)
                continue
            kind, msg = await self.out_queue.get()
            if kind == "audio":
                await self.session.send_realtime_input(audio=msg)
            elif kind == "video":
                await self.session.send_realtime_input(video=msg)

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
                task_state = self.active_task
                parsed = self.control.parse_response(candidate_text)
                if parsed is None:
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
                        )
                        await self._enqueue_followup(
                            repair_prompt,
                            generation=task_state.generation,
                        )
                    continue

                execution = self.control.execute_from_response(parsed)
                self._print_agent_status(parsed, execution=execution)

                if self.control.task_completed(parsed):
                    self._stop_active_task(reason="completed")
                    continue

                if self.control.needs_confirmation(parsed):
                    self._stop_active_task(reason="needs_confirmation")
                    continue

                visual_state = self._normalize_visual_state(parsed.get("visual_state"))
                command_signature = self._command_signature(parsed)
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
                    )
                    await self._enqueue_followup(
                        followup,
                        generation=task_state.generation,
                    )
                    continue

                if execution is not None and bool(execution.get("ok")):
                    task_state.awaiting_validation = True
                    print("[wait] waiting for UI to settle before validation", flush=True)
                    followup = self.control.build_validation_prompt(
                        task_state.user_request,
                        parsed,
                        execution,
                        step_index=task_state.step_index,
                        max_steps=self.config.agent_max_steps,
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
        if self.out_queue is None:
            return
        if self.config.video_mode == "camera":
            await stream_camera(self.out_queue, self.preview)
        elif self.config.video_mode == "screen":
            await stream_screen(self.out_queue, self.preview)

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            async with self.client.aio.live.connect(
                model=self.config.model,
                config=self.live_config,
            ) as session:
                self.session = session
                self.audio_in_queue = asyncio.Queue()
                self.out_queue = asyncio.Queue(maxsize=5)
                if self.prompt_queue is None:
                    self.prompt_queue = asyncio.Queue()
                if self.user_input_queue is None:
                    self.user_input_queue = asyncio.Queue()

                tasks.append(asyncio.create_task(self.read_user_input()))
                tasks.append(asyncio.create_task(self.agent_loop()))
                tasks.append(asyncio.create_task(self.send_realtime()))
                tasks.append(asyncio.create_task(self.receive_responses()))
                if self.config.video_mode in {"camera", "screen"}:
                    tasks.append(asyncio.create_task(self.stream_video()))
                if self.audio_enabled:
                    tasks.append(asyncio.create_task(self.listen_audio()))
                    tasks.append(asyncio.create_task(self.play_audio()))

                await tasks[1]
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
