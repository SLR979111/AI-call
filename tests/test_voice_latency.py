import asyncio
from array import array

import pytest

from app.brain import Brain, json_lines
from app.models import DEFAULT_CARD, Memory, Say, Update
from app.session import Session
from app.speech_start import first_sound
from app.voice_preview import first_clause, restore_quotes, same_transcript


@pytest.mark.parametrize("draft,final,match", [
    ("已经提交了还有多久", "已经提交了，还有多久？", True),
    ("已经提交了", "已经提交了，但是传错了。", False),
    ("已经提交了", "还没提交。", False),
    ("费用12元", "费用1.2元。", False),
    ("可以提交", "可以提交？", False),
    ("不可以", "不，可以。", False),
])
def test_draft_must_match_complete_intent(draft, final, match):
    assert same_transcript(draft, final) is match


def test_first_sound_retains_consonant_preroll_across_chunks():
    silence = bytes(12000)
    assert first_sound(silence) is None
    assert first_sound(silence + array("h", [500] * 120).tobytes()) == 12000 - 960
    assert first_sound(bytes(24000)) == 0


async def test_prepare_clause_before_metadata_without_publishing_an_event():
    seen = []

    async def stream():
        yield '{"type":"say","text":"缺少商标状态截图，'
        assert seen == ["缺少商标状态截图，"]
        yield '请重新提交。","covers":["reason"]}\n'

    result = [event async for event in json_lines(stream(), seen.append)]
    assert result[0]["covers"] == ["reason"]
    assert first_clause('{"type":"update","text":"不应播出。"}') is None


async def test_changed_draft_cannot_leak_speech_history_or_task_credit():
    emitted = []
    class DraftBrain:
        snapshot = staticmethod(Brain.snapshot)

        async def respond(self, memory, candidates, *, on_first_clause):
            yield "say", Say(text=memory.history[-1]["text"])
    class Cloud:
        async def speech(self, text):
            yield text.encode()

    async def send(event):
        emitted.append(event)
    session = Session(DEFAULT_CARD, Cloud(), DraftBrain(), send, voice=True)
    try:
        session.prepare_voice("已经提交")
        old = session.preview
        await old.task
        assert not session.memory.history and not emitted
        assert session.take_preview("还没提交") is None
        session.prepare_voice("还没提交")
        preview = session.take_preview("还没提交。")
        assert preview
        await preview.task
        def unused():
            raise AssertionError("prepared response should be reused")
        assert [(kind, say.text) async for kind, say in preview.events(unused)] == [("say", "还没提交")]
        assert b"".join([chunk async for chunk in preview.audio(unused)]) == "还没提交".encode()
        assert not session.memory.history and not emitted
    finally:
        await session.close()


def test_replay_requires_a_complete_prior_turn_and_a_standalone_request():
    memory = Memory(DEFAULT_CARD)
    memory.history = [{"role": "assistant", "text": "您收到材料了吗？", "delivery": "complete"},
                      {"role": "user", "text": "不好意思，刚才没听清，您再说一遍。"}]
    assert [line.text for line in memory.requested_replay()] == ["您收到材料了吗？"]
    memory.history[0]["delivery"] = "partial"
    assert not memory.requested_replay()
    memory.history[0]["delivery"] = "complete"
    memory.history[-1]["text"] += "还要补什么？"
    assert not memory.requested_replay()

def test_prepared_observation_uses_a_literal_quote_from_final_transcript():
    event = Update.model_validate({"facts": [{"name": "进度", "value": "已上传",
                                            "quote": "是的我已经上传"}]})
    restored = restore_quotes(event, "是的我已经上传", "是的，我已经上传。")
    assert restored.facts[0].quote == "是的，我已经上传"
    assert event.facts[0].quote == "是的我已经上传"

def test_credit_exact_spoken_checkpoint_even_when_the_model_omits_its_id():
    memory = Memory(DEFAULT_CARD)
    statement = "缺少商标当前状态截图"
    assert memory.delivered_covers(Say(text=statement + "。")) == ["reason"]
    assert memory.delivered_covers(Say(text=statement + "？")) == []
    assert memory.delivered_covers(Say(text="缺少截图。")) == []

async def test_confirmed_audio_hedge_plays_only_one_stream():
    from app.voice_preview import VoicePreview

    preview = VoicePreview("事实", {})
    preview.local = False
    async def alternate():
        yield b"same-voice-first"
        preview.audio_queue.put_nowait(b"late-first")
        preview.audio_queue.put_nowait(None)
        yield b"same-voice-second"
    assert [chunk async for chunk in preview.audio(alternate)] == [b"same-voice-first", b"same-voice-second"]


async def test_stalled_cloud_asr_uses_only_the_complete_local_clip():
    from app.hearing import Hearing

    committed = []
    async def noop(*args): pass
    async def commit(text, end):
        committed.append((text, end))
    hearing = Hearing(None, on_start=noop, on_text=commit, on_partial=noop, on_error=noop,
                      detector=object())
    hearing.active = True
    hearing.quiet = 1100
    hearing.voiced_end = hearing.decode_at = 9600
    hearing.local_final = (9600, "这是最新完整原话")
    hearing.decode = hearing.spawn(asyncio.sleep(30))
    await hearing.finish(hearing.voice_tick, 1234)
    await hearing.close()
    assert committed == [("这是最新完整原话", 1234)]
