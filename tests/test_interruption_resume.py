import asyncio
import math

import pytest

from app import session as session_module
from app.continuation import heard_question, hesitation, pickup
from app.models import DEFAULT_CARD, Say, Update
from app.session import Session
from test_core import Cloud, TestBrain, until
from test_hearing import Hearing, Input

OPENING = "我看过您的资料了。目前还缺一张清晰的照片，请您重新上传。"
HEARD = "我看过您的资料了。目前还缺"
PICKUP = "是这样的，目前还缺一张清晰的照片，请您重新上传。"


class RecordingCloud(Cloud):
    def __init__(self):
        super().__init__()
        self.synthesized = []

    async def speech(self, text):
        self.synthesized.append(text)
        async for packet in super().speech(text):
            yield packet


async def speaking(cloud, events, opening=OPENING):
    async def send(event):
        events.append(event)

    card = DEFAULT_CARD.model_copy(update={"opening": opening})
    session = Session(card, cloud, TestBrain(), send, voice=True, predictions=False)
    await session.start()
    await until(lambda: session.units and session.units[1].sent == 1920)
    return session


def frames(events, kind):
    return [e for e in events if isinstance(e, dict) and e["type"] == kind]


@pytest.mark.parametrize(("word", "question", "ignored"), [
    ("呃……", False, True), ("嗯。", False, True), ("（咳嗽声）", False, True),
    ("嗯", True, False), ("嗯？", False, False), ("不", False, False),
    ("停", False, False), ("好", False, False), ("呃，我想问", False, False),
])
def test_only_unambiguous_hesitations_are_ignored(word, question, ignored):
    assert hesitation(word, answering_question=question) is ignored


def test_pickup_keeps_conditions_questions_and_checkpoint_evidence():
    line = Say(text=OPENING, covers=["reason"])
    resumed = pickup(line, HEARD)
    assert resumed.text == PICKUP and resumed.covers == []
    conditional = Say(text="关于审核。如果材料齐全，才能开始审核。")
    assert pickup(conditional, "关于审核。如果材料齐全，才能开").text == "如果材料齐全，才能开始审核。"
    amount = Say(text="金额是10,000元。")
    assert pickup(amount, "金额是10,").text == amount.text
    question = Say(text="我看过资料了。您方便补交吗？", question="您方便补交吗？")
    assert pickup(question, "我看过资料了。您方便").text == question.question
    assert not heard_question(question, "我看过资料")
    assert heard_question(question, "我看过资料了。您方便")


@pytest.mark.parametrize("asr_text", ["", "呃……", "不用了"])
async def test_voice_barge_in_renders_immediately_but_yields_to_unfinished_asr(monkeypatch, asr_text):
    monkeypatch.setattr(session_module, "RESUME_DELAY", .08)
    events, errors = [], []
    cloud = RecordingCloud()
    session = await speaking(cloud, events)
    old = session.epoch
    await session.playback_progress(old, 1, math.ceil(1920 * len(HEARD) / len(OPENING)))
    release = asyncio.Event()

    async def decode(pcm):
        await release.wait()
        return asr_text

    async def on_error(message):
        errors.append(message)

    hearing = Hearing(cloud, on_start=lambda: session.interrupt("speech"),
                      on_text=lambda text, end: session.user_text(text, source="voice", end_ms=end),
                      on_partial=lambda _: asyncio.sleep(0), on_error=on_error,
                      is_playing=lambda: session.phase == "speaking", transcriber=decode,
                      ignores_hesitation=session.ignores_hesitation)
    session.wait_for_input_end = hearing.wait_for_input_end
    try:
        feed = Input(hearing)
        await feed.feed(12, playback=True)
        await until(lambda: PICKUP in cloud.synthesized)
        assert session.units[1].entry["text"] == HEARD
        await feed.feed(40, value=0, speech=False)
        await asyncio.sleep(.11)
        assert len(frames(events, "reply_start")) == 1
        release.set()
        await until(lambda: len(frames(events, "utterance")) == 2)
        if asr_text == "不用了":
            assert frames(events, "utterance")[-1]["text"] != PICKUP
            assert frames(events, "transcript")[-1]["text"] == asr_text
        else:
            assert frames(events, "utterance")[-1]["text"] == PICKUP
            assert not frames(events, "transcript")
            assert not errors
            assert cloud.synthesized.count(PICKUP) == 1
            await until(lambda: session.units[1].sent == 1920)
            await session.playback_progress(session.epoch, 1, 1920, done=True)
            await until(lambda: session.reply_completed)
            assert session.memory.history[-1]["delivery"] == "complete"
    finally:
        release.set()
        await hearing.close()
        await session.close()


async def test_interrupting_repeat_prompt_does_not_resume_it():
    events = []

    async def send(event):
        events.append(event)

    session = Session(DEFAULT_CARD, RecordingCloud(), TestBrain(), send, voice=True, predictions=False)
    try:
        session.request_repeat(100.)
        await until(lambda: any(isinstance(event, bytes) for event in events))
        epoch = session.epoch
        await session.interrupt("speech")
        assert frames(events, "cancel")[-1]["epoch"] == epoch
        assert session.resume_task is None
    finally:
        await session.close()


async def test_close_or_new_input_discards_speculative_audio(monkeypatch):
    monkeypatch.setattr(session_module, "RESUME_DELAY", .05)
    for action in ("close", "text"):
        cloud, events = RecordingCloud(), []
        session = await speaking(cloud, events)
        await session.playback_progress(session.epoch, 1, math.ceil(1920 * len(HEARD) / len(OPENING)))
        await session.interrupt("manual", expected_epoch=session.epoch)
        await until(lambda: PICKUP in cloud.synthesized)
        if action == "close":
            await session.close()
        else:
            await session.user_text("我要问的是审核进度")
        await asyncio.sleep(.09)
        assert PICKUP not in [e["text"] for e in frames(events, "utterance")]
        await session.close()
        assert not session.jobs


async def test_second_interruption_resumes_only_latest_unfinished_units(monkeypatch):
    monkeypatch.setattr(session_module, "RESUME_DELAY", .04)
    class TwoLines(TestBrain):
        async def respond(self, memory, candidates):
            yield "say", Say(text=OPENING)
            yield "say", Say(text="请在周五之前提交。", covers=["next_step"])
            yield "update", Update()
    events = []
    async def send(event):
        events.append(event)
    session = Session(DEFAULT_CARD, RecordingCloud(), TwoLines(), send, voice=True, predictions=False)
    try:
        await session.user_text("需要什么？")
        await until(lambda: len(frames(events, "utterance_end")) == 2)
        await session.playback_progress(session.epoch, 1, math.ceil(1920 * len(HEARD) / len(OPENING)))
        await session.interrupt("speech")
        await until(lambda: len(frames(events, "utterance_end")) == 4)
        await session.playback_progress(session.epoch, 1, 1920, done=True)
        await session.playback_progress(session.epoch, 2, 480)
        await session.interrupt("speech")
        await until(lambda: len(frames(events, "utterance_end")) == 5)
        assert frames(events, "utterance")[-1]["text"] == "请在周五之前提交。"
        assert "next_step" not in session.memory.delivered
        await session.playback_progress(session.epoch, 1, 1920, done=True)
        assert "next_step" in session.memory.delivered
    finally:
        await session.close()


async def test_interrupt_during_bridge_prepares_unanswered_question(monkeypatch):
    monkeypatch.setattr(session_module, "RESUME_DELAY", .06)
    class SlowFirstAnswer(TestBrain):
        async def respond(self, memory, candidates, *, on_first_clause=None):
            self.seen.append([entry["text"] for entry in memory.history if entry["role"] == "user"])
            if len(self.seen) == 1:
                await asyncio.Future()
            yield "say", Say(text="需要营业执照的照片。")
            yield "update", Update()

    events, brain, cloud = [], SlowFirstAnswer(), RecordingCloud()
    async def send(event):
        events.append(event)
    session = Session(DEFAULT_CARD, cloud, brain, send, voice=True, predictions=False)
    try:
        session.bridge_audio = bytes(1920)
        session.bridge_delay = .005
        await session.user_text("缺哪种资料？")
        await until(lambda: frames(events, "utterance_end"))
        await session.interrupt("speech")
        await until(lambda: "需要营业执照的照片。" in cloud.synthesized)
        await until(lambda: len(frames(events, "utterance_end")) == 2)
        assert frames(events, "utterance")[-1]["text"] == "需要营业执照的照片。"
        assert brain.seen == [["缺哪种资料？"], ["缺哪种资料？"]]
        assert len([e for e in session.memory.history if e["role"] == "user"]) == 1
    finally:
        await session.close()
