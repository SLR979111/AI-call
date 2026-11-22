import struct

from app.voice.speech_endpoint import SpeechEndpoint
from app.voice.types import AudioFrame


class Detector:
    def feed(self, pcm):
        return 0.9 if pcm == SPEECH else 0.05


SPEECH = struct.pack("<h", 1200) * 320
NOISE = struct.pack("<h", 2600) * 320


def feed(endpoint, audio, frames, start=0):
    for index, pcm in enumerate(frames, start + 1):
        endpoint.feed(AudioFrame(pcm, index, index * 20), audio)


def test_loud_noise_cannot_start_or_prolong_a_turn():
    endpoint = SpeechEndpoint(Detector(), 900)
    audio = bytearray()
    feed(endpoint, audio, [NOISE] * 250)
    assert not endpoint.started and not endpoint.ready and not audio

    feed(endpoint, audio, [SPEECH] * 16 + [NOISE] * 44, 250)
    assert endpoint.started and not endpoint.ready and endpoint.last_voiced_ms == 5320
    feed(endpoint, audio, [NOISE], 310)
    assert endpoint.ready
    assert endpoint.clip(audio).endswith(NOISE * 8)


def test_a_680_ms_mid_sentence_pause_keeps_both_parts_in_one_turn():
    endpoint = SpeechEndpoint(Detector(), 900)
    audio = bytearray()
    feed(endpoint, audio, [SPEECH] * 16 + [NOISE] * 34)
    assert endpoint.started and not endpoint.ready
    feed(endpoint, audio, [SPEECH] * 12 + [NOISE] * 45, 50)
    assert endpoint.ready and endpoint.last_voiced_ms == 1240
    assert endpoint.clip(audio) == SPEECH * 16 + NOISE * 34 + SPEECH * 12 + NOISE * 8
