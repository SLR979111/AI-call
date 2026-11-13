from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from .types import VoiceError

VoiceNotConfigured = VoiceError


def providers() -> tuple[str, str]:
    legacy = os.getenv("VOICE_PROVIDER", "").strip().lower()
    test = os.getenv("APP_ENV") == "test"
    legacy_asr = {"sherpa-onnx": "sherpa"}.get(legacy, legacy)
    legacy_tts = {"sherpa": "melo", "sherpa-onnx": "melo"}.get(legacy, legacy)
    asr = os.getenv("ASR_PROVIDER", legacy_asr or ("fake" if test else "sherpa")).strip().lower()
    tts = os.getenv("TTS_PROVIDER", legacy_tts or ("fake" if test else "qwen3")).strip().lower()
    return asr, tts


def create_tts(provider: str | None = None):
    provider = provider or providers()[1]
    if os.getenv("TTS_FALLBACK_PROVIDER", "").strip():
        raise VoiceError("TTS_CONFIG", "TTS_FALLBACK_PROVIDER must be empty; voice fallback is disabled")
    if provider == "fake":
        from .fake import FakeTts
        return FakeTts()
    if provider == "qwen3":
        from .qwen import QwenTts
        return QwenTts(os.getenv("QWEN_TTS_URL", "http://127.0.0.1:8090"))
    if provider == "volc":
        from .volc import VolcTts
        return VolcTts()
    if provider == "hybrid":
        from .hybrid import HybridTts
        return HybridTts()
    if provider == "melo":
        from .melo import MeloTts
        try:
            speed = float(os.getenv("MELO_SPEED", "1.0"))
        except ValueError as exc:
            raise VoiceError("MELO_CONFIG", "MELO_SPEED must be a number") from exc
        return MeloTts(os.getenv("MELO_MODEL_DIR", "models/vits-melo-tts-zh_en"), speed=speed)
    raise VoiceError("TTS_CONFIG", f"unknown TTS_PROVIDER={provider}")


def create_asr(provider: str | None = None):
    provider = provider or providers()[0]
    if provider == "fake":
        from .fake import FakeAsr
        return FakeAsr()
    if provider == "sherpa":
        from .sherpa_asr import SherpaAsr
        return SherpaAsr(os.getenv("ASR_MODEL_DIR", "models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"))
    if provider == "volc":
        from .volc_asr import VolcAsr
        return VolcAsr()
    raise VoiceError("ASR_CONFIG", f"unknown ASR_PROVIDER={provider}")


@dataclass
class VoiceStatus:
    asr_provider: str
    tts_provider: str
    asr_ready: bool = False
    tts_ready: bool = False
    asr_detail: str = "not initialized"
    tts_detail: str = "not initialized"

    @property
    def provider(self):
        return self.asr_provider if self.asr_provider == self.tts_provider else f"{self.asr_provider}/{self.tts_provider}"

    @property
    def detail(self):
        return f"ASR: {self.asr_detail}; TTS: {self.tts_detail}"

    def to_dict(self):
        return {"provider": self.provider, "asr_ready": self.asr_ready, "tts_ready": self.tts_ready,
                "detail": self.detail,
                "asr": {"provider": self.asr_provider, "ready": self.asr_ready, "detail": self.asr_detail},
                "tts": {"provider": self.tts_provider, "ready": self.tts_ready, "detail": self.tts_detail}}


def voice_status() -> VoiceStatus:
    # Configuration-only helper retained for callers outside an application lifespan.
    asr, tts = providers()
    return VoiceStatus(asr, tts, asr == "fake", tts == "fake",
                       "test provider" if asr == "fake" else "not initialized",
                       "test tone" if tts == "fake" else "not initialized")


class UnavailableTts:
    sample_rate = 24000

    def __init__(self, error):
        self.error = error

    async def synthesize(self, *args, **kwargs):
        raise self.error
        yield  # async iterator contract even on failure


class VoiceRuntime:
    def __init__(self):
        self.status = voice_status()
        self.asr = None
        self.tts = None

    async def initialize(self):
        # Only the selected providers are imported / loaded.
        for kind, factory in (("asr", create_asr), ("tts", create_tts)):
            provider = getattr(self.status, f"{kind}_provider")
            try:
                adapter = await asyncio.to_thread(factory, provider)
                setattr(self, kind, adapter)
                setattr(self.status, f"{kind}_ready", True)
                setattr(self.status, f"{kind}_detail", "loaded" if provider != "fake" else "test provider")
            except Exception as exc:
                setattr(self.status, f"{kind}_ready", False)
                setattr(self.status, f"{kind}_detail", str(exc))
                if kind == "tts":
                    self.tts = UnavailableTts(VoiceError("TTS_NOT_READY", str(exc)))
        await self.refresh()

    async def refresh(self):
        if self.status.tts_provider == "qwen3" and hasattr(self.tts, "health"):
            self.status.tts_ready, self.status.tts_detail = await self.tts.health()
        return self.status

    async def start_asr(self, session_id, *, client_endpointing=False):
        if self.asr is None:
            raise VoiceError("ASR_NOT_READY", self.status.asr_detail)
        if client_endpointing:
            return await self.asr.start(session_id, client_endpointing=True)
        return await self.asr.start(session_id)
