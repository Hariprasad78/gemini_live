"""Application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MODEL = "models/gemini-3.1-flash-live-preview"
DEFAULT_MODE = "camera"
PREVIEW_WINDOW_NAME = "Gemini Live Preview"
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024


@dataclass(frozen=True)
class AppConfig:
    video_mode: str = DEFAULT_MODE
    audio_enabled: bool = True
    model: str = MODEL
    ir_profile: str = "default"
    execute_ir: bool = False
    ir_serial_port: str | None = None
    ir_serial_baudrate: int = 115200
    ir_sender_channel: str = "D2"
    ir_device_id: str = "samsung_tv_default"
    ir_dataset_path: str = str(Path("artifacts/ir_dataset.json"))
    agent_max_steps: int = 30
    agent_step_delay_seconds: float = 1.0
    agent_ui_settle_seconds: float = 1.5

    @property
    def preview_enabled(self) -> bool:
        return self.video_mode in {"camera", "screen"}


def build_live_config(audio_enabled: bool):
    from google.genai import types
    from .live_control import LIVE_TV_CONTROL_SYSTEM_INSTRUCTION

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=104857,
            sliding_window=types.SlidingWindow(target_tokens=52428),
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=LIVE_TV_CONTROL_SYSTEM_INSTRUCTION,
        generation_config=types.GenerationConfig(temperature=0.1),
    )
    if audio_enabled:
        config.speech_config = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
            )
        )
    return config
