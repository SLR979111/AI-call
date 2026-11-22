"""Ordering between a reused reply's state update and the next forecast, and
carrying prepared forecasts across a speech onset."""

import asyncio
import struct

from app.brain import Brain
from app.models import DEFAULT_CARD, Candidate, Observation, Say, Update
from app.session import Session
from test_core import until


def candidate(id: str = "c1", *, when: str = "用户用好的表示明白，且没有提出新问题",
              text: str = "那您先补截图。", examples=("好的。",)) -> Candidate:
    return Candidate(id=id, when=when, examples=list(examples),
                     reply=Say(text=text, covers=["reason"]))


class RecordingBrain:
    snapshot = staticmethod(Brain.snapshot)

    def __init__(self):
        self.seen = []

    async def respond(self, memory, candidates):
        self.seen.append(candidates)
        yield "say", Say(text="请补充商标状态截图。")
        yield "update", Update()


async def test_reuse_update_is_merged_into_next_forecast_snapshot():
    snapshots = []
    update_started, release = asyncio.Event(), asyncio.Event()

    class Cloud:
        async def prefetch(self, text):
            return b"\x01\x00" * 96

        async def speech(self, text):
            raise AssertionError("the prepared reply must not call TTS")
            yield b""

        async def chat(self, messages, **kwargs):
            raise AssertionError("no model call expected")
            yield ""

    class ReuseBrain:
        snapshot = staticmethod(Brain.snapshot)

        async def respond(self, memory, candidates):
            assert candidates
            yield "reuse", candidates[0].reply
            yield "update_async", candidates[0]

        async def update_for_reuse(self, memory, selected):
            update_started.set()
            await release.wait()
            return Update(facts=[Observation(name="意愿", value="明白", quote="好的")],
                          questions=["还缺截图吗？"])

        async def predict(self, snapshot):
            snapshots.append(snapshot)
            return []

    async def send(event):
        pass

    reply = Say(text="那您先补截图。", covers=["reason"])
    session = Session(DEFAULT_CARD, Cloud(), ReuseBrain(), send, voice=False)
    session.candidates = [candidate(text=reply.text)]
    session.prediction_epoch = session.epoch
    session.reply_completed = True
    try:
        await session.user_text("好的")
        await asyncio.wait_for(update_started.wait(), 1)
        # The next-turn snapshot must not be taken before the update lands.
        assert snapshots == []
        release.set()
        await until(lambda: bool(snapshots))
        state = snapshots[0]["state"]
        assert state["questions"] == ["还缺截图吗？"]
        assert state["facts"]["意愿"]["value"] == "明白"
        assert snapshots[0]["pending_reply"][0]["text"] == "那您先补截图。"
        # Live memory is still committed only after playback, exactly once.
        await until(lambda: session.memory.questions == ["还缺截图吗？"])
        assert session.memory.facts["意愿"]["value"] == "明白"
        await until(lambda: session.response is not None and session.response.done())
        await until(lambda: session.forecast is not None and session.forecast.done())
    finally:
        await session.close()
        await asyncio.sleep(0)
    assert not session.jobs


async def test_short_answer_after_speech_interrupt_reuses_staged_audio():
    pcm = b"\x01\x00" * 96
    packets = []
    sent = asyncio.Event()

    class Cloud:
        def __init__(self):
            self.speech_calls = 0

        async def prefetch(self, text):
            return pcm

        async def speech(self, text):
            self.speech_calls += 1
            yield pcm

        async def chat(self, messages, **kwargs):
            yield '{"type":"update"}\n'

    async def send(event):
        if isinstance(event, bytes):
            packets.append(event)
            sent.set()

    cloud = Cloud()
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=True,
                      predictions=False)
    prepared = candidate()
    session.memory.history = [{"role": "assistant", "text": "您清楚吗？",
                               "delivery": "complete"}]
    session.candidates = [prepared]
    session.prediction_audio = {prepared.reply.text: pcm}
    session.prediction_epoch = session.epoch
    session.response = session.spawn(asyncio.Event().wait())
    try:
        await session.interrupt("speech")
        assert session.staged_candidates == [prepared]
        assert session.staged_audio == {prepared.reply.text: pcm}
        # The interrupted turn's own eligibility is gone.
        assert not session.candidates
        assert session.prediction_epoch == -1
        assert not session.reply_completed

        await session.user_text("好的")
        await asyncio.wait_for(sent.wait(), 1)
        assert session.turn_metrics["reused"] is True
        assert session.turn_metrics["staged_reuse"] is True
        assert session.turn_metrics["prediction_audio_hit"] is True
        assert cloud.speech_calls == 0
        assert packets[0][12:] == pcm
        # Staging is consumed by exactly one user turn.
        assert not session.staged_candidates
        assert not session.staged_audio
    finally:
        await session.close()
    assert not session.jobs


async def test_short_answer_without_staged_audio_is_not_reused():
    pcm = b"\x01\x00" * 96
    chats = []

    class Cloud:
        async def prefetch(self, text):
            return pcm

        async def speech(self, text):
            yield pcm

        async def chat(self, messages, **kwargs):
            chats.append(messages)
            yield '{"type":"say","text":"请补充截图。","covers":[]}\n'
            yield '{"type":"update"}\n'

    async def send(event):
        pass

    cloud = Cloud()
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=True,
                      predictions=False)
    prepared = candidate()
    session.memory.history = [{"role": "assistant", "text": "您清楚吗？",
                               "delivery": "complete"}]
    session.candidates = [prepared]
    session.prediction_epoch = session.epoch
    # Voice candidates without prepared PCM are not reusable.
    session.prediction_audio = {}
    session.response = session.spawn(asyncio.Event().wait())
    try:
        await session.interrupt("speech")
        assert not session.staged_candidates
        await session.user_text("好的")
        assert session.turn_metrics["staged_reuse"] is False
        assert session.turn_metrics["reused"] is False
        assert session.turn_metrics["candidates_available"] == 0
        await until(lambda: bool(chats))  # full model response, not a reuse
    finally:
        await session.close()
    assert not session.jobs


async def test_staged_candidates_expire_at_the_next_turn_boundary():
    brain = RecordingBrain()

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, None, brain, send, voice=False,
                      predictions=False)
    prepared = candidate()
    session.candidates = [prepared]
    session.prediction_epoch = session.epoch
    session.response = session.spawn(asyncio.Event().wait())
    try:
        await session.interrupt("speech")
        assert session.staged_candidates == [prepared]
        # Another interrupt starts a new turn; old staging must not survive.
        session.response = session.spawn(asyncio.Event().wait())
        await session.interrupt("manual")
        assert not session.staged_candidates
        await session.user_text("好的")
        await until(lambda: bool(brain.seen))
        assert brain.seen == [[]]
        assert session.turn_metrics["candidates_available"] == 0
        assert session.turn_metrics["staged_reuse"] is False
    finally:
        await session.close()
    assert not session.jobs


async def test_staged_candidates_need_content_rule_confirmation():
    chats = []

    class Cloud:
        async def prefetch(self, text):
            return b"\x01\x00" * 96

        async def speech(self, text):
            yield b"\x01\x00" * 96

        async def chat(self, messages, **kwargs):
            chats.append(messages)
            yield '{"type":"say","text":"请补充截图。","covers":[]}\n'
            yield '{"type":"update"}\n'

    async def send(event):
        pass

    cloud = Cloud()
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=False,
                      predictions=False)
    prepared = candidate(when="用户明确说已经提交，且没有提出新问题",
                         text="好的，收到。", examples=("已经提交了。",))
    session.memory.history = [{"role": "assistant", "text": "您清楚吗？",
                               "delivery": "complete"}]
    session.candidates = [prepared]
    session.prediction_epoch = session.epoch
    session.response = session.spawn(asyncio.Event().wait())
    try:
        await session.interrupt("speech")
        assert session.staged_candidates == [prepared]
        # "好的" does not meet the staged forecast's condition, so the model
        # must answer fresh instead of resurrecting the interrupted candidate.
        await session.user_text("好的")
        await until(lambda: session.response is not None and session.response.done())
        assert session.turn_metrics["staged_reuse"] is False
        assert session.turn_metrics["reused"] is False
        assert session.turn_metrics["candidates_available"] == 0
        assert len(chats) == 1
    finally:
        await session.close()
    assert not session.jobs


async def test_unplayed_reply_cannot_reuse_live_candidates():
    brain = RecordingBrain()

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, None, brain, send, voice=False,
                      predictions=False)
    prepared = candidate()
    session.candidates = [prepared]
    session.prediction_epoch = session.epoch
    session.reply_completed = False
    try:
        await session.user_text("好的")
        await until(lambda: bool(brain.seen))
        assert brain.seen == [[]]
        assert session.turn_metrics["candidates_available"] == 0
        assert session.turn_metrics["staged_reuse"] is False
    finally:
        await session.close()
    assert not session.jobs
