import asyncio

import pytest

from app.brain import Brain
from app.models import DEFAULT_CARD, Memory, Say, Update
from app.session import Session, Unit
from test_core import Cloud, TestBrain, until


class TwoSentences(TestBrain):
    async def respond(self, memory, candidates):
        self.seen.append(Brain.snapshot(memory)["dialogue"][:-1])
        yield "say", Say(text="甲乙丙丁", covers=["reason"])
        yield "say", Say(text="戊己庚辛", covers=["next_step"])
        yield "update", Update()


async def test_second_sentence_streams_before_first_ack_and_cutoff_reaches_next_turn():
    events, brain = [], TwoSentences()
    async def send(event):
        events.append(event)
    session = Session(DEFAULT_CARD, Cloud(), brain, send, voice=True, predictions=False)
    try:
        await session.user_text("请说", source="voice", end_ms=100)
        await until(lambda: sum(isinstance(e, dict) and e["type"] == "utterance_end" for e in events) == 2)
        epoch = session.epoch
        await session.playback_started(epoch, 350, unit_id=1)
        assert session.turn_metrics["voice_to_answer_ms"] == 250
        assert not session.memory.delivered
        await session.playback_progress(epoch, 1, 1920, done=True)
        await session.playback_progress(epoch, 2, 960)
        await session.interrupt("speech", expected_epoch=epoch)
        await session.playback_progress(epoch, 2, 1920, done=True)
        await session.user_text("刚才讲到哪里？")
        await until(lambda: len(brain.seen) == 2)
        assert brain.seen[1][-2] == {"role": "assistant", "text": "甲乙丙丁", "delivery": "complete"}
        assert brain.seen[1][-1] == {"role": "assistant", "text": "戊己", "remaining_text": "庚辛",
                                     "delivery": "partial", "played_ms": 40}
        assert session.memory.delivered == {"reason"}
    finally:
        await session.close()


def test_cutoff_during_streaming_uses_elapsed_time():
    unit = Unit(1, Say(text="甲乙丙丁戊己庚辛"), {})
    unit.total = 18000
    unit.record_cutoff()
    assert unit.entry["delivery"] == "unplayed" and unit.entry["text"] == ""
    unit.played = 12000
    unit.record_cutoff()
    assert unit.entry["text"] == "甲乙"
    assert unit.entry["remaining_text"] == "丙丁戊己庚辛"
    assert unit.entry["played_ms"] == 500
    unit.duration_samples = 96000
    unit.record_cutoff()
    assert unit.entry["text"] == "甲"


async def test_position_question_quotes_only_played_words_without_a_model_request():
    memory = Memory(DEFAULT_CARD, history=[
        {"role": "assistant", "text": "您好，这里", "remaining_text": "是图书室，周六开门。",
         "played_ms": 1400, "delivery": "partial"},
        {"role": "user", "text": "刚才你说到哪里了？"},
    ])
    # The cloud stub implements no chat method. This answer is grounded locally.
    events = [event async for event in Brain(Cloud()).respond(memory, [])]
    assert [(kind, line.text) for kind, line in events] == [("say", "刚才大约说到“您好，这里”。")]
    memory.history[-1]["text"] += "接着告诉我几点开门。"
    assert memory.interrupted_position() == (events[0][1].text, "接着告诉我几点开门。")
    memory.history.extend([{"role": "assistant", "text": "九点。", "delivery": "complete"},
                           {"role": "user", "text": "刚才你说到哪里了？"}])
    assert memory.interrupted_position() is None


class ClosingBrain(TestBrain):
    async def respond(self, memory, candidates):
        yield "say", Say(text="好，您明白就好。")
        yield "update", Update(disposition="complete", confirmation="明白了")


@pytest.mark.parametrize("interrupt", [False, True])
async def test_proactive_goodbye_waits_for_playback_and_can_be_interrupted(interrupt):
    events = []
    async def send(event):
        events.append(event)
    session = Session(DEFAULT_CARD, Cloud(), ClosingBrain(), send, voice=True, predictions=False)
    try:
        session.memory.delivered = {"reason", "next_step"}
        await session.user_text("明白了，没有别的问题了。")
        await until(lambda: sum(isinstance(e, dict) and e["type"] == "utterance_end" for e in events) == 2)
        epoch = session.epoch
        assert session.units[2].say.text == "再见。"
        await session.playback_progress(epoch, 1, 1920, done=True)
        assert not session.ended
        if interrupt:
            await session.interrupt("manual", expected_epoch=epoch)
        await session.playback_progress(epoch, 2, 1920, done=True)
        if not interrupt:
            await until(lambda: session.ended)
        assert sum(isinstance(e, dict) and e["type"] == "hangup" for e in events) == int(not interrupt)
    finally:
        await session.close()


def test_new_question_blocks_proactive_goodbye():
    memory = Memory(DEFAULT_CARD, status="complete")
    assert not memory.ready_to_say_goodbye("明白了，我还想问一件事", Say(text="您说。"))
    assert not memory.ready_to_say_goodbye("明白了", Say(text="您还有什么疑问？"))
    assert memory.ready_to_say_goodbye("我明白该怎么做了。", Say(text="好的。"))
    assert not memory.ready_to_say_goodbye("怎么补交", Say(text="好，再见。"))
    memory.questions = ["审核多久"]
    assert not memory.ready_to_say_goodbye("明白了", Say(text="好的。"))


def test_explicit_goodbye_after_confirmation_leaves_room_for_real_followup_questions():
    from app.models import wants_to_end
    assert wants_to_end("明白了，先这样，再见。")
    assert wants_to_end("我马上进会了，先挂了，谢谢。")
    assert not wants_to_end("你先挂了电话，我怎么联系你？")
    assert not wants_to_end("明白了，先这样，再见之前再问一句，周六几点关门？")


@pytest.mark.parametrize(("user", "reply"), [
    ("好再见", "好的，再见！"),
    ("好。", "好，那就先这样，再见。"),
])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_spoken_goodbye_closes_after_audio_even_with_active_update(user, reply, interrupt):
    class SpokenGoodbye(TestBrain):
        async def respond(self, memory, candidates):
            yield "say", Say(text=reply)
            yield "update", Update()

    events = []
    async def send(event):
        events.append(event)
    session = Session(DEFAULT_CARD, Cloud(), SpokenGoodbye(), send, voice=True, predictions=False)
    try:
        await session.user_text(user)
        await until(lambda: any(isinstance(e, dict) and e["type"] == "utterance_end" for e in events))
        epoch = session.epoch
        response = session.response
        assert not session.ended
        if interrupt:
            await session.interrupt("manual", expected_epoch=epoch)
        await session.playback_progress(epoch, 1, 1920, done=True)
        if not interrupt:
            await response
        ends = [e for e in events if isinstance(e, dict) and e["type"] == "hangup"]
        assert [e["reason"] for e in ends] == ([] if interrupt else ["paused"])
        assert session.ended == (not interrupt)
        assert not session.memory.delivered
    finally:
        await session.close()


def test_final_acknowledgement_only_closes_after_complete_instructions():
    user = "好的。"
    last = {"role": "assistant", "text": "结果请等页面通知。", "delivery": "complete"}
    memory = Memory(DEFAULT_CARD, history=[last, {"role": "user", "text": user}])
    assert not memory.closing_reason(user)
    memory.delivered = {p.id for p in DEFAULT_CARD.checkpoints}
    for acknowledgement in (user, "行。", "嗯，好的。", "好再见"):
        assert memory.closing_reason(acknowledgement)
    for other in ("好？", "好的，然后呢", "好，我还要问", "好，先别挂"):
        assert not memory.closing_reason(other)
    memory.last_question = "您已经上传了吗？"
    assert not memory.closing_reason(user)
    memory.last_question = ""
    memory.questions = ["怎么补交"]
    assert not memory.closing_reason(user)
    memory.questions.clear()
    last["delivery"] = "partial"
    assert not memory.closing_reason(user)


async def test_stale_send_waiting_for_transport_is_dropped():
    events = []
    async def send(event):
        events.append(event)

    session = Session(DEFAULT_CARD, Cloud(), TestBrain(), send, voice=True)
    try:
        async with session.send_lock:
            queued = asyncio.create_task(session.send(b"old PCM", epoch=session.epoch))
            await asyncio.sleep(0)
            session.epoch += 1
        await queued
        assert events == []
    finally:
        await session.close()


async def test_full_queue_releases_producer_when_player_fails():
    from app.playback import enqueue_for_playback
    from app.providers import ProviderError

    failed = asyncio.Event()
    async def output():
        await failed.wait()
        raise ProviderError("TTS failed")

    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(object())
    consumer = asyncio.create_task(output())
    producer = asyncio.create_task(enqueue_for_playback(queue, None, consumer))
    await asyncio.sleep(0)
    failed.set()
    with pytest.raises(ProviderError, match="TTS failed"):
        await asyncio.wait_for(producer, 1)
    queue.get_nowait()
    assert queue.empty()
