"""Local SenseVoice ASR used as the fast final transcriber of the G2 link.

The cloud ``/v1/tools/asr`` round trip costs ~0.7 s on every utterance, which is
the whole remaining budget of a cached answer.  SenseVoice is non-autoregressive:
it finalises a short Chinese utterance in ~0.1-0.2 s on four threads and, unlike
the streaming zipformer, keeps punctuation and digits intact.  ``make_transcriber``
keeps the cloud as fallback so the link still works without the model files.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL = ROOT / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def default_threads() -> int:
    """SenseVoice concurrency that actually helps on this machine.

    A short utterance is single-digit tokens, so the decoder cannot scale to
    every core: measured P50 on a 4-core box was 347 ms at one thread, 205 ms
    at two, 345 ms at four and 811 ms at eight.  Half the cores (capped at 2,
    at least 1) keeps the endpoint thread and the TTS/LLM streams responsive
    while still beating the naive "use every core" default.
    """
    cores = os.cpu_count() or 1
    return max(1, min(2, max(1, cores // 2)))


class LocalAsr:
    def __init__(self, model_dir: str | Path | None = None, threads: int | None = None):
        self.model_dir = Path(model_dir or os.getenv("LOCAL_ASR_DIR") or DEFAULT_MODEL)
        self.threads = threads or int(
            os.getenv("LOCAL_ASR_THREADS") or default_threads())
        self._recognizer = None

    @property
    def ready(self) -> bool:
        return self._recognizer is not None

    def load(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        model = self.model_dir / "model.int8.onnx"
        if not model.exists():
            model = self.model_dir / "model.onnx"
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(self.model_dir / "tokens.txt"),
            num_threads=self.threads, use_itn=True, language=os.getenv("LOCAL_ASR_LANG", "zh"))

    def transcribe_sync(self, pcm: bytes, sample_rate: int = 16000) -> str:
        self.load()
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if sample_rate != 16000:
            from scipy.signal import resample_poly
            samples = resample_poly(samples, 16000, sample_rate).astype(np.float32)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    async def transcribe(self, pcm: bytes) -> str:
        return await asyncio.to_thread(self.transcribe_sync, pcm)


def make_transcriber(local: LocalAsr | None, cloud):
    """Prefer the local model; fall back to the cloud call when it is absent or fails."""
    async def transcribe(pcm: bytes) -> str:
        if local is not None and local.ready:
            try:
                text = await local.transcribe(pcm)
                if text:
                    return text
            except Exception:  # noqa: BLE001 - never break a call over a local model
                pass
        return await cloud.transcribe(pcm)
    return transcribe


def load_from_env() -> LocalAsr | None:
    if os.getenv("LOCAL_ASR", "1") != "1":
        return None
    asr = LocalAsr()
    if not (asr.model_dir / "tokens.txt").exists():
        return None
    return asr
