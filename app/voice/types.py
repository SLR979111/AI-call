from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from app.cancel import CancelToken

QWEN_MAX_TEXT_CHARS = 2000


class VoiceError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    def to_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def mono_ms() -> float:
    return time.perf_counter() * 1000


@dataclass
class AudioChunk:
    pcm: bytes
    seq: int
    sentence_id: int = 0  # assigned by the orchestrator, adapter has no turn state
    is_last: bool = False
    ts_mono_ms: float = 0
    sample_rate: int = 24000


@dataclass
class AudioFrame:
    pcm: bytes
    seq: int
    client_mono_ms: int
    flags: int = 0
    sample_rate: int = 16000

    @classmethod
    def parse(cls, data: bytes) -> "AudioFrame":
        if len(data) != 656:
            raise VoiceError("INVALID_AUDIO_FRAME", "expected 16-byte header + 320 PCM16 samples")
        magic, version, rate, count, seq, ts, flags = struct.unpack(">BBHHIIH", data[:16])
        if (magic, version, rate, count) != (0xA1, 1, 16000, 320):
            raise VoiceError("INVALID_AUDIO_FRAME", "expected A1/v1/16000 Hz/320 samples")
        return cls(data[16:], seq, ts, flags, rate)


@dataclass
class AsrEvent:
    kind: str
    text: str = ""
    confidence: float = 0.0
    audio_start_ms: int = 0
    audio_end_ms: int = 0
    error: dict | None = None


class AsrStream(Protocol):
    async def push(self, frame: AudioFrame) -> None: ...
    async def finish(self) -> None: ...
    def events(self) -> AsyncIterator[AsrEvent]: ...
    async def close(self) -> None: ...


class AsrAdapter(Protocol):
    async def start(self, session_id: str, *, sample_rate: int = 16000,
                    hotwords: list[str] | None = None) -> AsrStream: ...


class TtsAdapter(Protocol):
    sample_rate: int
    def synthesize(self, text: str, *, voice: str = "default", sample_rate: int = 24000,
                   cancel: CancelToken) -> AsyncIterator[AudioChunk]: ...
