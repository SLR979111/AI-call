"""Hybrid TTS: phase fillers from local PCM, real answers from Volc Seed-TTS."""
from __future__ import annotations

import os
import wave
from pathlib import Path

from .types import AudioChunk, VoiceError, mono_ms
from .volc_bidir import VolcBidirectionalTts

FILLER_TEXTS = {
    "好的，我明白了。亲，请您稍等。": "identity.wav",
    "好的，我帮您确认一下。亲，请您稍等。": "confirm.wav",
    "好的，我帮您看一下这笔申请。亲，请您稍等。": "offer.wav",
    "好的，我帮您确认处理结果。亲，请您稍等。": "result.wav",
    "好的，我收到您的信息了。亲，请您稍等。": "default.wav",
}

STATIC_TEXTS = {
    "您的申请被驳回，原因是主体资质到期。请更新有效资质后重新提交。": "identity_script.wav",
    "请更新有效资质后重新提交。": "update_submit.wav",
    "您可以在商家后台的资质管理里更新。": "backend_update.wav",
    "审核一般需要一到三个工作日。": "review_time.wav",
    "好的，祝您生意兴隆，再见。": "closing.wav",
}


class HybridTts:
    sample_rate = 24000

    def __init__(self, filler_dir: str | None = None) -> None:
        self.cloud = VolcBidirectionalTts()
        self.filler_dir = Path(filler_dir or os.getenv(
            "HYBRID_FILLER_DIR", "data/voice/fillers-volc"
        ))
        self.fillers: dict[str, bytes] = {}
        for text, name in {**FILLER_TEXTS, **STATIC_TEXTS}.items():
            path = self.filler_dir / name
            if not path.is_file():
                continue
            with wave.open(str(path), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, self.sample_rate):
                    self.fillers[text] = wav.readframes(wav.getnframes())

    def _cached(self, text: str, sample_rate: int) -> bytes | None:
        if sample_rate != self.sample_rate:
            return None
        return self.fillers.get(text)

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        cached = self._cached(text, sample_rate)
        if cached is not None:
            frame_bytes = max(2, (sample_rate // 50) * 2)
            seq = 0
            for offset in range(0, len(cached), frame_bytes):
                cancel.check()
                yield AudioChunk(cached[offset:offset + frame_bytes], seq,
                                 ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                seq += 1
            cancel.check()
            yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
            return
        if not self.fillers:
            raise VoiceError("HYBRID_CONFIG", f"no filler wavs in {self.filler_dir}", True)
        async for chunk in self.cloud.synthesize(
            text, voice=voice, sample_rate=sample_rate, cancel=cancel
        ):
            yield chunk
