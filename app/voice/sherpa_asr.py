from __future__ import annotations

import asyncio
from pathlib import Path
import numpy as np

from .types import AsrEvent, VoiceError


def require_files(root: Path, names: list[str]):
    missing = [name for name in names if not (root / name).exists()]
    if missing:
        raise VoiceError("MODEL_NOT_CONFIGURED", f"missing files in {root}: {', '.join(missing)}")


class SherpaAsr:
    def __init__(self, model_dir: str, *, finish_padding_ms: int = 300):
        root = Path(model_dir)
        require_files(root, ["tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"])
        self.finish_padding_ms = finish_padding_ms
        import sherpa_onnx
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(root / "tokens.txt"), encoder=str(root / "encoder.int8.onnx"),
            decoder=str(root / "decoder.onnx"), joiner=str(root / "joiner.int8.onnx"),
            num_threads=2, sample_rate=16000, feature_dim=80, decoding_method="greedy_search",
            enable_endpoint_detection=True, rule1_min_trailing_silence=0.8,
            rule2_min_trailing_silence=0.8, rule3_min_utterance_length=15.0,
        )

    async def start(self, session_id, *, sample_rate=16000, hotwords=None):
        if sample_rate != 16000 or hotwords:
            raise VoiceError("ASR_CONFIG", "ASR requires 16000 Hz; hotwords unsupported by greedy_search")
        return SherpaAsrStream(self.recognizer, finish_padding_ms=self.finish_padding_ms)


class SherpaAsrStream:
    def __init__(self, recognizer, *, finish_padding_ms: int = 300):
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()
        self.queue = asyncio.Queue(maxsize=64)
        self.lock = asyncio.Lock()
        self.closed = False
        self.samples = 0
        self.start_ms = 0
        self.last_text = ""
        self.finish_padding = np.zeros(finish_padding_ms * 16, dtype=np.float32)

    def _emit(self, event):
        # A slow consumer may miss partial revisions, never block finalization.
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)

    def _decode(self, pcm=None, finish=False):
        if pcm:
            self.stream.accept_waveform(16000, np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
        if finish:
            self.stream.accept_waveform(16000, self.finish_padding)
            self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        text = self.recognizer.get_result(self.stream)
        endpoint = finish or self.recognizer.is_endpoint(self.stream)
        if endpoint and not finish:
            self.recognizer.reset(self.stream)
        return text, endpoint

    async def push(self, frame):
        async with self.lock:
            if self.closed:
                raise VoiceError("ASR_CLOSED", "ASR stream is closed")
            self.samples += len(frame.pcm) // 2
            text, final = await asyncio.to_thread(self._decode, frame.pcm)
            if final or (text and text != self.last_text):
                self._emit(AsrEvent("final" if final else "partial", text,
                                              audio_start_ms=self.start_ms,
                                              audio_end_ms=self.samples // 16))
            self.last_text = "" if final else text
            if final:
                self.start_ms = self.samples // 16

    async def finish(self):
        if self.closed:
            return
        async def flush():
            async with self.lock:
                text, _ = await asyncio.to_thread(self._decode, finish=True)
                self._emit(AsrEvent("final", text, audio_start_ms=self.start_ms,
                                              audio_end_ms=self.samples // 16))
        try:
            await asyncio.wait_for(flush(), timeout=0.28)
        except Exception as exc:
            self._emit(AsrEvent("error", error={
                "code": "ASR_FINISH_TIMEOUT" if isinstance(exc, TimeoutError) else "ASR_FAILED",
                "message": str(exc) or "ASR finalization exceeded 280 ms", "retryable": True}))
        finally:
            await self.close()

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self):
        if not self.closed:
            self.closed = True
            # No native reset here: a timed-out decode may still own this stream.
            self._emit(None)
