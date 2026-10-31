from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass


class VoiceNotConfigured(Exception):
    pass


@dataclass
class VoiceStatus:
    provider: str
    asr_ready: bool
    tts_ready: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "asr_ready": self.asr_ready,
            "tts_ready": self.tts_ready,
            "detail": self.detail,
        }


def voice_status() -> VoiceStatus:
    """Report what the process can actually use.

    The fake skeleton uses browser speechSynthesis for TTS and text input for
    ASR. Real CPU ASR/TTS (sherpa-onnx) is a W2 milestone; this function makes
    the boundary explicit instead of pretending it is ready.
    """
    provider = os.getenv("VOICE_PROVIDER", "fake").strip().lower()
    if provider == "fake":
        return VoiceStatus("fake", True, True, "browser speechSynthesis + text input")
    if provider in {"sherpa", "sherpa-onnx"}:
        installed = importlib.util.find_spec("sherpa_onnx") is not None
        return VoiceStatus(
            "sherpa-onnx",
            installed,
            installed,
            "sherpa_onnx installed" if installed else "install sherpa-onnx and mount models/",
        )
    if provider in {"funasr", "cosyvoice"}:
        return VoiceStatus(provider, False, False, f"{provider} adapter not wired in v0.1")
    return VoiceStatus(provider, False, False, f"unknown VOICE_PROVIDER={provider}")


def ensure_voice_provider() -> str:
    status = voice_status()
    if not (status.asr_ready and status.tts_ready):
        raise VoiceNotConfigured(status.detail)
    return status.provider
