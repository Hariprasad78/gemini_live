"""Audio input/output helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from google.genai import types

from .config import CHANNELS, CHUNK_SIZE, RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE


@dataclass
class AudioRuntime:
    audio_stream: Optional[object] = None
    output_stream: Optional[object] = None
    pya: Optional[object] = None
    module: Optional[object] = None

    def enabled(self, requested: bool) -> bool:
        if not requested:
            return False
        try:
            self._import_pyaudio()
        except RuntimeError:
            return False
        return True

    def ensure_available(self) -> None:
        self._import_pyaudio()

    def _import_pyaudio(self):
        if self.module is not None:
            return self.module
        try:
            import pyaudio
        except Exception as exc:
            raise RuntimeError(
                "Audio mode requires pyaudio, which is not installed or is unusable in this environment."
            ) from exc
        self.module = pyaudio
        return self.module

    def ensure_pyaudio(self):
        module = self._import_pyaudio()
        if self.pya is None:
            self.pya = module.PyAudio()
        return self.pya

    async def open_input_stream(self):
        audio = self.ensure_pyaudio()
        mic_info = audio.get_default_input_device_info()
        self.audio_stream = await asyncio.to_thread(
            audio.open,
            format=self.module.paInt16,
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            input=True,
            input_device_index=mic_info["index"],
            frames_per_buffer=CHUNK_SIZE,
        )
        return self.audio_stream

    async def open_output_stream(self):
        audio = self.ensure_pyaudio()
        self.output_stream = await asyncio.to_thread(
            audio.open,
            format=self.module.paInt16,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        return self.output_stream

    async def read_chunk(self) -> bytes:
        if self.audio_stream is None:
            await self.open_input_stream()
        kwargs = {"exception_on_overflow": False} if __debug__ else {}
        return await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)

    async def write_chunk(self, data: bytes) -> None:
        if self.output_stream is None:
            await self.open_output_stream()
        await asyncio.to_thread(self.output_stream.write, data)

    def build_blob(self, data: bytes) -> types.Blob:
        return types.Blob(
            data=data,
            mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
        )

    def close(self) -> None:
        if self.audio_stream is not None:
            self.audio_stream.close()
            self.audio_stream = None
        if self.output_stream is not None:
            self.output_stream.close()
            self.output_stream = None
        if self.pya is not None:
            self.pya.terminate()
            self.pya = None
