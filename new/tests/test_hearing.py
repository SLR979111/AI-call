import asyncio
import struct
from array import array

from app.hearing import Hearing as RealHearing


class TestSpeech:
    # Endpoint/cancellation unit tests use synthetic markers. Real-model audio
    # classification is exercised separately by scripts/check_acoustics.py.
    def feed(self, pcm):
        return .95 if max(array('h',pcm))>1000 else 0.
    def reset(self): pass


def Hearing(*args,**kwargs):
    kwargs.setdefault('detector',TestSpeech())
    return RealHearing(*args,**kwargs)


class ASR:
    def __init__(self):
        self.requests = []
        self.release = asyncio.Event()

    async def transcribe(self, pcm):
        self.requests.append(pcm)
        await self.release.wait()
        return "我想说明两个事情"


class Input:
    def __init__(self, hearing): self.hearing, self.seq = hearing, 0
    async def feed(self, frames, value=4000, *, speech=True, playback=False):
        for _ in range(frames):
            pcm = array('h', [value]*320).tobytes()
            rms=abs(value)/32768
            flags=(1 if speech and value else 0) | (2 if playback else 0)
            await self.hearing.feed(struct.pack('<IdIfffff',self.seq,self.seq*20.,flags,rms,rms,0.,0.,0.)+pcm+pcm)
            self.seq += 1
            await asyncio.sleep(0)


async def test_pause_replaces_stalled_partial_before_endpoint():
    cloud, results = ASR(), []

    async def noop(*args):
        pass

    async def text(value, at):
        results.append(value)

    hearing = Hearing(cloud, on_start=noop, on_partial=noop, on_text=text, on_error=noop)
    feed = Input(hearing)
    try:
        await feed.feed(52)
        assert len(cloud.requests) == 1
        await feed.feed(8, 0)
        assert len(cloud.requests) == 2 and len(cloud.requests[1]) > len(cloud.requests[0])
        assert not results
        cloud.release.set()
        await feed.feed(30, 0)
        await asyncio.sleep(0)
        assert results == ["我想说明两个事情"]
        assert len(cloud.requests) == 2
    finally:
        await hearing.close()


async def test_resume_before_asr_finishes_keeps_both_parts_and_commits_once():
    cloud, results, errors = ASR(), [], []
    async def noop(*a): pass
    async def text(value, at): results.append((value,at))
    async def error(value): errors.append(value)
    hearing = Hearing(cloud,on_start=noop,on_partial=noop,on_text=text,on_error=error)
    feed = Input(hearing)
    await feed.feed(25)
    await feed.feed(40,0)  # finalization starts, ASR has not returned yet
    await feed.feed(20)   # continuation cancels that finalization
    await feed.feed(40,0)
    cloud.release.set()
    await asyncio.sleep(.02)
    assert len(results) == 1
    assert not errors
    assert len(cloud.requests[-1]) >= (25+40+20)*640
    await hearing.close()


async def test_gap_invalidates_input_instead_of_submitting_a_truncated_sentence():
    cloud, results, errors = ASR(), [], []
    cloud.release.set()
    async def noop(*a): pass
    async def text(value, at): results.append(value)
    async def error(value): errors.append(value)
    hearing = Hearing(cloud,on_start=noop,on_partial=noop,on_text=text,on_error=error)
    feed = Input(hearing)
    await feed.feed(20)
    feed.seq += 1
    await feed.feed(10)
    await feed.feed(40,0)
    assert not results
    assert len(errors) == 1
    await feed.feed(20)
    await feed.feed(40,0)
    await asyncio.sleep(.02)
    assert len(results) == 1
    await hearing.close()


async def test_complete_short_answer_commits_after_short_silence():
    class ShortASR:
        async def transcribe(self, pcm):
            return '是的。'

    results = []
    async def noop(*a): pass
    async def text(value, at): results.append(value)
    hearing = Hearing(ShortASR(), on_start=noop, on_partial=noop,
                      on_text=text, on_error=noop)
    feed = Input(hearing)
    try:
        silence = hearing.defer_short // 20
        await feed.feed(20)
        await feed.feed(silence - 1, 0)
        assert not results
        await feed.feed(1, 0)
        await asyncio.sleep(.01)
        assert results == ['是的。']
    finally:
        await hearing.close()


async def test_short_answer_followed_by_continuation_resets_fast_endpoint():
    class ContinuingASR:
        def __init__(self):
            self.calls = 0

        async def transcribe(self, pcm):
            self.calls += 1
            return '是的。' if self.calls == 1 else '是的，但是还有材料没准备'

    results = []
    async def noop(*a): pass
    async def text(value, at): results.append(value)
    hearing = Hearing(ContinuingASR(), on_start=noop, on_partial=noop,
                      on_text=text, on_error=noop)
    feed = Input(hearing)
    try:
        await feed.feed(20)
        await feed.feed(hearing.defer_short // 20 - 2, 0)
        assert hearing.defer_to == hearing.defer_short
        await feed.feed(20)
        assert hearing.defer_to == hearing.defer_base
        wait = hearing.defer_base // 20
        await feed.feed(wait - 2, 0)
        assert not results
        await feed.feed(2, 0)
        await asyncio.sleep(.01)
        assert results == ['是的，但是还有材料没准备']
    finally:
        await hearing.close()


async def test_noise_pulse_does_not_start_a_turn():
    cloud, starts = ASR(), []
    async def start(): starts.append(True)
    async def noop(*a): pass
    hearing = Hearing(cloud,on_start=start,on_partial=noop,on_text=noop,on_error=noop)
    feed = Input(hearing)
    await feed.feed(2)
    await feed.feed(60,0)
    assert not starts and not cloud.requests
    await hearing.close()


async def test_backend_does_not_bypass_the_conservative_playback_gate():
    cloud, starts = ASR(), []
    async def start(): starts.append(True)
    async def noop(*a): pass
    hearing = Hearing(cloud, on_start=start, on_partial=noop, on_text=noop,
                      on_error=noop, is_playing=lambda: True)
    feed = Input(hearing)
    await feed.feed(6, 2000)  # 120 ms pulse, formerly enough to cancel playback.
    await feed.feed(40, 0)
    await feed.feed(25, 330, speech=False, playback=True)
    await feed.feed(40, 0)
    assert not starts and not cloud.requests
    await feed.feed(20, 3000)
    assert len(starts) == 1
    assert hearing.last_onset["playback"]
    # The stricter gate keeps the onset audio rather than clipping first words.
    assert len(hearing.buffer) >= 20 * 640
    await hearing.close()


async def test_loud_nonspeech_cannot_start_a_turn_even_when_above_volume_threshold():
    cloud, starts = ASR(), []
    async def start(): starts.append(True)
    async def noop(*a): pass
    class Nonspeech:
        def feed(self,pcm): return .01
    hearing = Hearing(cloud, on_start=start, on_partial=noop, on_text=noop, on_error=noop,detector=Nonspeech())
    await Input(hearing).feed(100, 8000, speech=False)
    assert not starts and not cloud.requests
    await hearing.close()


async def test_syllable_gaps_allow_barge_in_but_separate_pulses_do_not_accumulate():
    cloud, starts = ASR(), []
    async def start(): starts.append(True)
    async def noop(*a): pass
    hearing = Hearing(cloud, on_start=start, on_partial=noop, on_text=noop,
                      on_error=noop, is_playing=lambda: True)
    feed = Input(hearing)
    await feed.feed(5)
    await feed.feed(20, 0)
    await feed.feed(5)
    await feed.feed(20, 0)
    assert not starts
    await feed.feed(4)
    await feed.feed(3, 0)
    await feed.feed(4)
    assert len(starts) == 1
    await hearing.close()


async def test_playback_residue_does_not_raise_the_noise_floor():
    cloud = ASR()
    async def noop(*a): pass
    class Nonspeech:
        def feed(self,pcm): return .01
    hearing = Hearing(cloud, on_start=noop, on_partial=noop, on_text=noop,
                      on_error=noop, detector=Nonspeech(), is_playing=lambda: True)
    before = hearing.floor
    await Input(hearing).feed(100, 8000)
    assert hearing.floor == before
    assert not cloud.requests
    await hearing.close()


async def test_final_frames_start_local_voice_preview_when_partial_text_is_unchanged():
    cloud = ASR()
    cloud.is_local = True
    cloud.release.set()
    drafts, partials = [], []

    async def noop(*args):
        pass

    async def on_partial(text):
        partials.append(text)

    hearing = Hearing(cloud, on_start=noop, on_text=noop, on_partial=on_partial, on_error=noop,
                      on_draft=lambda text, origin: drafts.append((text, origin)))
    try:
        hearing.active = True
        hearing.voiced_end = 1280
        hearing.buffer = bytearray(1280)
        hearing.partial_at = 640
        hearing.partial_text = "我想说明两个事情"
        hearing.request_decode()
        await hearing.decode
        assert drafts == [("我想说明两个事情", "local")]
        assert partials == []
    finally:
        await hearing.close()
