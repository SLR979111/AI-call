import asyncio
import json
import struct

import pytest

from app.brain import Brain, json_lines
from app.models import DEFAULT_CARD, Candidate, Memory, Say, Update
from app.providers import ProviderError
from app.session import Session


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


class Cloud:
    def __init__(self):
        self.cancelled = 0
        self.release = asyncio.Event()
        self.block_audio = False

    async def speech(self, text):
        try:
            yield b'\x01\x00' * 960
            if self.block_audio:
                await self.release.wait()
            yield b'\x01\x00' * 960
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def prefetch(self, text):
        pass


class TestBrain:
    __test__ = False
    snapshot = staticmethod(Brain.snapshot)

    def __init__(self):
        self.seen = []
        self.forecast_release = asyncio.Event()

    async def respond(self, memory, candidates):
        self.seen.append(candidates)
        yield "say", Say(text="缺少商标当前状态截图。", covers=["reason"])
        yield "update", Update()

    async def predict(self, snapshot):
        await self.forecast_release.wait()
        return [Candidate(id="c1", when="询问缺少什么", reply=Say(text="需要商标状态截图。", covers=["reason"]))]


def test_completion_needs_previously_delivered_information_and_explicit_confirmation():
    memory = Memory(DEFAULT_CARD)
    update = Update(disposition="complete", confirmation="明白了")
    memory.apply(update, "明白了", 1, set())
    assert memory.status == "active"
    memory.apply(update, "我没说这句话", 2, {"reason", "next_step"})
    assert memory.status == "active"
    memory.apply(Update(disposition="complete", confirmation="好的"), "好的", 2, {"reason", "next_step"})
    assert memory.status == "active"
    memory.apply(update, "明白了，我去补", 3, {"reason", "next_step"})
    assert memory.status == "complete"


def test_unquoted_facts_and_stop_claims_are_not_committed():
    memory = Memory(DEFAULT_CARD)
    update = Update.model_validate({"facts":[{"name":"意愿","value":"拒绝","quote":"不要再联系"}],
                                    "disposition":"declined","disposition_quote":"不要再联系"})
    memory.apply(update, "我想知道原因", 1, set())
    assert memory.facts == {}
    assert memory.status == "active"


async def test_forecast_cannot_block_direct_reply_or_mutate_memory():
    brain, cloud, events = TestBrain(), Cloud(), []
    async def send(event): events.append(event)
    session = Session(DEFAULT_CARD, cloud, brain, send, voice=False)
    await session.start()
    await until(lambda: session.reply_completed)
    assert not brain.forecast_release.is_set()
    await session.user_text("为什么要补？")
    await until(lambda: session.reply_completed)
    assert brain.seen == [[]]
    assert "reason" in session.memory.delivered
    assert session.memory.facts == {}
    await session.close()
    assert not session.jobs


async def test_cancel_discards_late_audio_and_does_not_claim_delivery():
    brain, cloud, events = TestBrain(), Cloud(), []
    cloud.block_audio = True
    async def send(event): events.append(event)
    session = Session(DEFAULT_CARD, cloud, brain, send, voice=True, predictions=False)
    await session.user_text("为什么？")
    await until(lambda: any(isinstance(e,bytes) for e in events))
    epoch = session.epoch
    await session.playback_progress(epoch, 1, 500)
    await session.interrupt("speech", expected_epoch=epoch)
    count = len([e for e in events if isinstance(e,bytes)])
    cloud.release.set()
    await asyncio.sleep(.03)
    await session.playback_progress(epoch, 1, 1920, done=True)
    assert len([e for e in events if isinstance(e,bytes)]) == count
    assert session.memory.delivered == set()
    assert session.memory.history[-1]["delivery"] == "partial"
    assert not session.reply_completed
    await session.close()


async def test_only_complete_valid_playback_ack_commits_coverage():
    brain, cloud, events = TestBrain(), Cloud(), []
    async def send(event): events.append(event)
    session = Session(DEFAULT_CARD, cloud, brain, send, voice=True, predictions=False)
    await session.user_text("为什么？")
    await until(lambda: any(isinstance(e,dict) and e.get("type")=="utterance_end" for e in events))
    epoch = session.epoch
    await session.playback_progress(epoch, 1, 999999, done=True)
    await session.playback_progress(epoch, 1, 960, done=True)
    assert not session.memory.delivered
    await session.playback_progress(epoch, 1, 1920, done=True)
    await until(lambda: session.reply_completed)
    assert session.memory.delivered == {"reason"}
    await session.interrupt("late", expected_epoch=epoch-1)
    assert session.epoch == epoch
    await session.close()


async def test_partial_playback_invalidates_even_ready_forecasts():
    brain, cloud, events = TestBrain(), Cloud(), []
    brain.forecast_release.set()
    cloud.block_audio = True
    async def send(event): events.append(event)
    session = Session(DEFAULT_CARD, cloud, brain, send, voice=True)
    await session.start()
    await until(lambda: session.candidates)
    await session.user_text("等等，我还没听完")
    await until(lambda: bool(brain.seen))
    assert brain.seen[-1] == []
    await session.close()


async def test_stream_parser_handles_split_lines_and_final_line():
    async def stream():
        for part in ['{"type":', '"say","text":"好。"}\n{"type":"up', 'date"}']:
            yield part
    assert [e async for e in json_lines(stream())] == [
        {"type":"say","text":"好。"}, {"type":"update"}]


async def test_invalid_reuse_is_an_error_not_the_first_candidate():
    class BadCloud:
        async def chat(self, messages):
            yield '{"type":"reuse","id":"not-present"}\n'
    with pytest.raises(ProviderError):
        _ = [e async for e in Brain(BadCloud()).respond(Memory(DEFAULT_CARD), [])]


async def test_a_question_not_in_the_spoken_text_is_not_recorded_as_asked():
    class PhantomQuestion:
        async def chat(self, messages):
            yield '{"type":"say","text":"需要补一张截图。","covers":[],"question":"清楚了吗？"}\n'
            yield '{"type":"update"}\n'
    events = [e async for e in Brain(PhantomQuestion()).respond(Memory(DEFAULT_CARD), [])]
    assert events[0][1].question == ""


async def test_speech_arriving_during_transcript_send_cannot_start_an_obsolete_reply():
    gate, entered = asyncio.Event(), asyncio.Event()
    brain = TestBrain()
    async def send(event):
        if isinstance(event, dict) and event.get("type") == "transcript":
            entered.set()
            await gate.wait()
    session = Session(DEFAULT_CARD, Cloud(), brain, send, voice=False, predictions=False)
    user_task = asyncio.create_task(session.user_text("先说第一件事"))
    await entered.wait()
    cancellation = asyncio.create_task(session.interrupt("speech"))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(user_task, cancellation)
    assert brain.seen == []
    await session.close()
