from __future__ import annotations

import os
import re
import threading
import wave
from math import gcd
from pathlib import Path
from time import monotonic

from contextlib import aclosing
from app.cancel import TurnCancelled

from .sherpa_asr import require_files
from .streaming import thread_stream
from ..cache import cache_key, tts_cache
from .types import AudioChunk, VoiceError, mono_ms

_SPOKEN_INITIALISMS = {"AI": "人工智能", "APP": "应用"}
_INITIALISM = re.compile(r"(?<![A-Za-z0-9])(?:AI|APP)(?![A-Za-z0-9])")


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

        # Pre-render fixed sentences (phase fillers) at load time. Lazily
        # synthesizing a filler holds the TTS lock for ~1 s, which delays the
        # real answer's first chunk behind it; a prewarmed match starts in ~1 ms.
        self.prewarmed: dict[tuple[str, int, str], bytes] = {}
        from scipy.signal import resample_poly
        import numpy as np
        for text in (item.strip() for item in os.getenv("MELO_PREWARM_TEXTS", "").split("|")):
            if not text:
                continue
            spoken = _INITIALISM.sub(lambda match, t=text: _SPOKEN_INITIALISMS[match[0]], text)
            samples = np.asarray(self.model.generate(spoken, sid=0, speed=self.speed).samples, dtype=np.float32)
            for rate in (24000, 44100):
                if rate == self.sample_rate:
                    resampled = samples
                else:
                    factor = gcd(rate, self.sample_rate)
                    resampled = resample_poly(samples, rate // factor, self.sample_rate // factor)
                pcm = (np.clip(resampled, -1, 1) * 32767).astype("<i2").tobytes()
                if pcm:
                    self.prewarmed[(text, rate, "default")] = pcm

        # Optional pre-rendered fixed filler. Cache only an exact text/rate
        # match so a stale recording can never replace a different sentence.
        self.filler_text = os.getenv("MELO_FILLER_TEXT", "").strip()
        self.filler_pcm = b""
        self.filler_rate = 0
        filler_path = os.getenv("MELO_FILLER_AUDIO", "").strip()
        if self.filler_text and filler_path and Path(filler_path).is_file():
            with wave.open(filler_path, "rb") as wav:
                if wav.getnchannels() == 1 and wav.getsampwidth() == 2:
                    self.filler_pcm = wav.readframes(wav.getnframes())
                    self.filler_rate = wav.getframerate()

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if voice != "default" or sample_rate not in (8000, 16000, 24000, 44100, 48000):
            raise VoiceError("TTS_CONFIG", "unsupported voice or sample rate")

        prewarmed = self.prewarmed.get((text, sample_rate, voice))
        if prewarmed:
            frame_bytes = max(2, (sample_rate // 50) * 2)
            seq = 0
            for offset in range(0, len(prewarmed), frame_bytes):
                cancel.check()
                yield AudioChunk(prewarmed[offset:offset + frame_bytes], seq,
                                 ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                seq += 1
            cancel.check()
            yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
            return

        if self.filler_pcm and text == self.filler_text and sample_rate == self.filler_rate:
            frame_bytes = max(2, (sample_rate // 50) * 2)
            seq = 0
            for offset in range(0, len(self.filler_pcm), frame_bytes):
                cancel.check()
                yield AudioChunk(self.filler_pcm[offset:offset + frame_bytes], seq,
                                 ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                seq += 1
            cancel.check()
            yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
            return

        # Completed sentences are deterministic for a fixed speed/rate, so the
        # decoded PCM survives a restart. A hit streams in ~1 ms instead of
        # paying ~0.3 s of VITS inference per sentence.
        audio_key = cache_key("melo-v1", self.speed, voice, sample_rate, text)
        store = tts_cache()
        cached_pcm = store.get_bytes("pcm", audio_key)
        if cached_pcm:
            frame_bytes = max(2, (sample_rate // 50) * 2)
            seq = 0
            for offset in range(0, len(cached_pcm), frame_bytes):
                cancel.check()
                yield AudioChunk(cached_pcm[offset:offset + frame_bytes], seq,
                                 ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                seq += 1
            cancel.check()
            yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
            return

        def produce(emit, stop):
            import numpy as np
            from scipy.signal import resample_poly
            deadline = monotonic() + 0.6
            while not stop.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise VoiceError("TTS_BUSY", "Melo is still generating after 600 ms", True)
                if self.lock.acquire(timeout=min(0.05, remaining)):
                    break
            else:
                return
            seq = 0
            collected = bytearray()
            try:
                if stop.is_set():
                    return

                def callback(samples, progress):
                    nonlocal seq
                    if stop.is_set():
                        return 0
                    samples = np.asarray(samples, dtype=np.float32)
                    if sample_rate != self.sample_rate:
                        factor = gcd(sample_rate, self.sample_rate)
                        samples = resample_poly(samples, sample_rate // factor, self.sample_rate // factor)
                    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
                    collected.extend(pcm)
                    chunk = AudioChunk(pcm, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                    seq += 1
                    return int(emit(chunk))
                spoken_text = _INITIALISM.sub(lambda match: _SPOKEN_INITIALISMS[match[0]], text)
                self.model.generate(spoken_text, sid=0, speed=self.speed, callback=callback)
                if not stop.is_set():
                    emit(AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate))
                    store.put_bytes("pcm", audio_key, bytes(collected))
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
