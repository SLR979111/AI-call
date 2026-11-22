"""Bound an uploaded utterance using the same Silero detector as G2 hearing."""
from collections import deque
from math import sqrt

import numpy as np

from .types import AudioFrame


class SpeechEndpoint:
    FRAME_MS = 20
    TAIL_MS = 160

    def __init__(self, detector, silence_ms: int):
        self.detector = detector
        self.silence_ms = silence_ms
        self.preroll: deque[AudioFrame] = deque(maxlen=16)
        self.onset = deque(maxlen=16)
        self.quiet_ms = 0
        self.last_voiced_ms = None
        self.voiced_bytes = 0
        self.started = False

    def feed(self, frame: AudioFrame, audio: bytearray) -> None:
        samples = np.frombuffer(frame.pcm, dtype="<i2").astype(np.float32)
        rms = sqrt(float(np.dot(samples, samples)) / len(samples)) / 32768
        voiced = self.detector.feed(frame.pcm) >= 0.6 and rms > 0.004
        if self.started:
            audio.extend(frame.pcm)
        else:
            self.preroll.append(frame)
            self.onset.append(voiced)
            if not voiced or sum(self.onset) < 6:
                return
            self.started = True
            for earlier in self.preroll:
                audio.extend(earlier.pcm)
            self.preroll.clear()

        if voiced:
            self.quiet_ms = 0
            self.last_voiced_ms = frame.client_mono_ms
            self.voiced_bytes = len(audio)
        else:
            self.quiet_ms += self.FRAME_MS

    @property
    def ready(self) -> bool:
        return self.started and self.quiet_ms >= self.silence_ms

    def clip(self, audio: bytearray) -> bytes:
        # Preserve unvoiced syllable tails and leave the long noise tail out of ASR.
        return bytes(audio[:self.voiced_bytes + self.TAIL_MS * 32]) if self.started else b""
