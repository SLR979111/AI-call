"""Fast local first pass. The existing cloud ASR still confirms every turn."""

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
