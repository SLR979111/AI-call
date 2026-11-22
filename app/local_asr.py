"""Local SenseVoice final transcription and Paraformer first-pass recognition.

The cloud ``/v1/tools/asr`` round trip costs ~0.7 s on every utterance, which is
the whole remaining budget of a cached answer.  SenseVoice is non-autoregressive:
it finalises a short Chinese utterance in ~0.1-0.2 s on four threads and, unlike
the streaming zipformer, keeps punctuation and digits intact.  ``make_transcriber``
keeps the cloud as fallback so the link still works without the model files.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soxr

ROOT = Path(__file__).resolve().parents[1]
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
            samples = soxr.resample(samples, sample_rate, 16000)
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


MODEL = "sherpa-onnx-paraformer-zh-small-2024-03-09"
SHA256 = "da92b3db5218c5be53aad53e57d1b6e63e7fc98a0e054fbdd6dbe18e9c6b1450"
FILES = ("model.int8.onnx", "tokens.txt")


def model_dir(kind="asr", model=MODEL):
    cache = Path(os.getenv("LOCALAPPDATA") or os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "ai-call-g2" / kind / model


def install():
    install_model("asr", MODEL, "asr-models", SHA256, FILES)


def install_model(kind, model, release, checksum, files):
    target = model_dir(kind, model)
    if all((target / name).is_file() for name in files):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        archive = Path(temp) / "model.tar.bz2"
        url = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{release}/{model}.tar.bz2"
        with urllib.request.urlopen(url, timeout=30) as remote, archive.open("wb") as out:
            shutil.copyfileobj(remote, out)
        with archive.open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() != checksum:
                raise ValueError("Model checksum mismatch")
        # Some models keep their pronunciation dictionaries in nested directories.
        required = {f"{model}/{name}" for name in files}
        with tarfile.open(archive, "r|bz2") as tar:
            for entry in tar:
                if entry.isfile() and entry.name in required:
                    tar.extract(entry, path=temp, filter="data")
        staging = Path(temp) / model
        if not all((staging / name).is_file() for name in files):
            raise ValueError("Incomplete model archive")
        for name in files:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            (staging / name).replace(dest)


class FirstPassASR:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-pass-asr")

    @classmethod
    def load(cls):
        root = model_dir()
        if not all((root / name).is_file() for name in FILES):
            return None
        try:
            import sherpa_onnx
            return cls(sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(root / FILES[0]), tokens=str(root / FILES[1]), num_threads=2))
        except (ImportError, RuntimeError, OSError) as exc:
            logging.getLogger(__name__).warning("First-pass ASR unavailable: %s", type(exc).__name__)
            return None

    def decode(self, pcm):
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()

    async def transcribe(self, pcm):
        return await asyncio.get_running_loop().run_in_executor(self.worker, self.decode, pcm)

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    install()
