from __future__ import annotations

import threading
from math import gcd
from pathlib import Path

from contextlib import aclosing
from app.cancel import TurnCancelled

from .sherpa_asr import require_files
from .streaming import thread_stream
from .types import AudioChunk, VoiceError, mono_ms


class MeloTts:
    sample_rate = 44100

    def __init__(self, model_dir: str, *, speed: float = 1.0):
        if not 0.75 <= speed <= 1.25:
            raise VoiceError("MELO_CONFIG", "Melo speed must be between 0.75 and 1.25")
        self.speed = speed
        root = Path(model_dir)
        rules = ["phone.fst", "date.fst", "number.fst", "new_heteronym.fst"]
        require_files(root, ["model.onnx", "tokens.txt", "lexicon.txt", "dict", *rules])
        import sherpa_onnx
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(root / "model.onnx"), tokens=str(root / "tokens.txt"),
                    lexicon=str(root / "lexicon.txt"), dict_dir=str(root / "dict")),
                num_threads=4, provider="cpu"),
            rule_fsts=",".join(str(root / name) for name in rules), max_num_sentences=1)
        if not config.validate():
            raise VoiceError("MELO_CONFIG", "invalid Melo model configuration")
        self.model = sherpa_onnx.OfflineTts(config)
        self.sample_rate = self.model.sample_rate
        self.lock = threading.Lock()
        # Load resampler and native lazy state before reporting readiness.
        from scipy.signal import resample_poly  # noqa: F401
        self.model.generate("您好。", sid=0, speed=self.speed)

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if voice != "default" or sample_rate not in (8000, 16000, 24000, 44100, 48000):
            raise VoiceError("TTS_CONFIG", "unsupported voice or sample rate")

        def produce(emit, stop):
            import numpy as np
            from scipy.signal import resample_poly
            if not self.lock.acquire(blocking=False):
                raise VoiceError("TTS_BUSY", "Melo is already generating", True)
            seq = 0
            try:
                def callback(samples, progress):
                    nonlocal seq
                    if stop.is_set():
                        return 0
                    samples = np.asarray(samples, dtype=np.float32)
                    if sample_rate != self.sample_rate:
                        factor = gcd(sample_rate, self.sample_rate)
                        samples = resample_poly(samples, sample_rate // factor, self.sample_rate // factor)
                    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
                    chunk = AudioChunk(pcm, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                    seq += 1
                    return int(emit(chunk))
                self.model.generate(text, sid=0, speed=self.speed, callback=callback)
                if not stop.is_set():
                    emit(AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate))
            finally:
                self.lock.release()

        try:
            async with aclosing(thread_stream(produce, cancel)) as stream:
                async for chunk in stream:
                    cancel.check()
                    yield chunk
        except (VoiceError, TurnCancelled):
            raise
        except Exception as exc:
            raise VoiceError("MELO_FAILED", str(exc), True) from exc
