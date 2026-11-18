"""Offline checks for the experimental PREDICTION_AUDIO_LIMIT budget."""

import asyncio

from app.models import DEFAULT_CARD, Candidate, Say, Update
from app.session import (
    PREDICTION_AUDIO_LIMIT_ENV,
    Session,
    prediction_audio_limit_from_env,
)


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def candidate(index: int) -> Candidate:
    return Candidate(
        id=f"p{index}",
        when=f"用户短答 {index}",
        reply=Say(text=f"这是第{index}条预测回复。"),
    )


class FakeCloud:
    def __init__(self, *, fail_texts: tuple[str, ...] = ()):
        self.prefetch_calls: list[str] = []
        self.speech_calls: list[str] = []
        self.fail_texts = set(fail_texts)

    async def prefetch(self, text):
        self.prefetch_calls.append(text)
        if text in self.fail_texts:
            return None
        await asyncio.sleep(0)
        return b"\x01\x00" * 960

    async def speech(self, text):
        self.speech_calls.append(text)
        yield b"\x02\x00" * 480
        yield b"\x02\x00" * 480

    def cached(self, text):
        return None


class ScriptedBrain:
    """Emit one candidate per batch, in a stable order."""

    def __init__(self, items):
        self.items = items
        self.snapshot = staticmethod(lambda memory: {
            "task": memory.card.model_dump(), "state": memory.view(), "dialogue": []})

    async def predict_batches(self, snapshot):
        for item in self.items:
            await asyncio.sleep(0)
            yield [item]


class RecordingSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[dict] = []

    def log(self, event, **fields):
        self.events.append({"event": event, **fields})

    def events_named(self, name):
        return [event for event in self.events if event["event"] == name]


def build_session(brain, cloud, *, voice=True):
    events = []

    async def send(payload):
        events.append(payload)

    session = RecordingSession(DEFAULT_CARD, cloud, brain, send, voice=voice,
                               predictions=False)
    return session, events


def test_limit_parsing_keeps_the_production_default(monkeypatch):
    monkeypatch.delenv(PREDICTION_AUDIO_LIMIT_ENV, raising=False)
    assert prediction_audio_limit_from_env() is None
    for raw in ("", "   "):
        monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, raw)
        assert prediction_audio_limit_from_env() is None
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "0")
    assert prediction_audio_limit_from_env() == 0
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "3")
    assert prediction_audio_limit_from_env() == 3
    for raw in ("-1", "many"):
        monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, raw)
        assert prediction_audio_limit_from_env() is None


async def test_unset_limit_still_prepares_audio_for_every_candidate(monkeypatch):
    monkeypatch.delenv(PREDICTION_AUDIO_LIMIT_ENV, raising=False)
    items = [candidate(i) for i in range(5)]
    cloud = FakeCloud()
    session, _ = build_session(ScriptedBrain(items), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        assert [item.id for item in session.candidates] == [f"p{i}" for i in range(5)]
        assert list(session.prediction_audio) == [item.reply.text for item in items]
        assert cloud.prefetch_calls == [item.reply.text for item in items]
        ready = session.events_named("prediction_ready")
        assert len(ready) == 5
        assert {event["audio_status"] for event in ready} == {"preparing"}
        assert len(session.events_named("prediction_audio_ready")) == 5
        assert not session.events_named("prediction_audio_skipped")
    finally:
        await session.close()


async def test_limit_prepares_only_the_leading_candidates_but_publishes_all_text(monkeypatch):
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "2")
    items = [candidate(i) for i in range(5)]
    cloud = FakeCloud()
    session, _ = build_session(ScriptedBrain(items), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        assert [item.id for item in session.candidates] == [f"p{i}" for i in range(5)]
        assert list(session.prediction_audio) == [item.reply.text for item in items[:2]]
        assert cloud.prefetch_calls == [item.reply.text for item in items[:2]]

        ready = session.events_named("prediction_ready")
        status = {event["candidate_id"]: event["audio_status"] for event in ready}
        assert [status[f"p{i}"] for i in range(5)] == [
            "preparing", "preparing", "limit_skipped", "limit_skipped", "limit_skipped"]
        skipped = session.events_named("prediction_audio_skipped")
        assert [event["candidate_id"] for event in skipped] == ["p2", "p3", "p4"]
        assert all(event["limit"] == 2 for event in skipped)
        assert session.events_named("prediction_audio_limit")[0]["limit"] == 2
    finally:
        await session.close()


async def test_budgeted_candidate_without_audio_keeps_text_but_has_no_audio(monkeypatch):
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "2")
    items = [candidate(i) for i in range(5)]
    cloud = FakeCloud(fail_texts=(items[0].reply.text,))
    session, _ = build_session(ScriptedBrain(items), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        # Text is published before audio. A failed prefetch leaves the text
        # candidate available and falls back to ordinary speech when selected.
        assert [item.id for item in session.candidates] == ["p0", "p1", "p2", "p3", "p4"]
        assert list(session.prediction_audio) == [items[1].reply.text]
        assert session.events_named("prediction_audio_miss")[0]["candidate_id"] == "p0"
    finally:
        await session.close()


async def test_zero_limit_publishes_every_candidate_without_audio(monkeypatch):
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "0")
    items = [candidate(i) for i in range(3)]
    cloud = FakeCloud()
    session, _ = build_session(ScriptedBrain(items), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        assert [item.id for item in session.candidates] == ["p0", "p1", "p2"]
        assert session.prediction_audio == {}
        assert cloud.prefetch_calls == []
        status = {event["candidate_id"]: event["audio_status"]
                  for event in session.events_named("prediction_ready")}
        assert set(status.values()) == {"limit_skipped"}
    finally:
        await session.close()


class ReuseTextBrain:
    def __init__(self, item):
        self.item = item

    async def respond(self, memory, candidates):
        assert self.item in candidates
        yield "reuse", self.item.reply
        yield "update_async", self.item

    async def update_for_reuse(self, memory, selected):
        return Update()


class BlockingCloud(FakeCloud):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prefetch(self, text):
        self.prefetch_calls.append(text)
        self.started.set()
        await self.release.wait()
        return b"\x01\x00" * 960


async def test_forecast_promise_is_dropped_before_playout(monkeypatch):
    monkeypatch.delenv(PREDICTION_AUDIO_LIMIT_ENV, raising=False)
    bad = candidate(0)
    bad.reply.text = "您放心，保证通过。"
    good = candidate(1)
    cloud = FakeCloud()
    session, _ = build_session(ScriptedBrain([bad, good]), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        assert [item.id for item in session.candidates] == ["p1"]
        assert session.events_named("prediction_compliance_drop")[0]["candidate_id"] == "p0"
        assert list(session.prediction_audio) == [good.reply.text]
    finally:
        await session.close()


async def test_text_candidate_is_published_before_audio_is_ready(monkeypatch):
    monkeypatch.delenv(PREDICTION_AUDIO_LIMIT_ENV, raising=False)
    items = [candidate(0)]
    cloud = BlockingCloud()
    session, _ = build_session(ScriptedBrain(items), cloud)
    try:
        task = asyncio.create_task(session._predict(0, session.brain.snapshot(session.memory)))
        await asyncio.wait_for(cloud.started.wait(), timeout=1)
        # The text is visible while the audio task is still blocked.
        assert [item.id for item in session.candidates] == ["p0"]
        assert session.prediction_audio == {}
        ready = session.events_named("prediction_ready")
        assert ready and ready[0]["audio_status"] == "preparing"
        cloud.release.set()
        await task
        assert list(session.prediction_audio) == [items[0].reply.text]
    finally:
        cloud.release.set()
        await session.close()


async def test_selecting_a_text_only_candidate_falls_back_to_tts(monkeypatch):
    monkeypatch.setenv(PREDICTION_AUDIO_LIMIT_ENV, "1")
    items = [candidate(i) for i in range(3)]
    cloud = FakeCloud()
    session, events = build_session(ScriptedBrain(items), cloud)
    try:
        await session._predict(0, session.brain.snapshot(session.memory))
        text_only = items[1]
        assert text_only.reply.text not in session.prediction_audio
        assert text_only in session.candidates

        session.brain = ReuseTextBrain(text_only)
        session.reply_completed = True
        session.prediction_epoch = 0
        await session.user_text("已经提交了。")
        await until(lambda: any(isinstance(e, dict) and e.get("type") == "utterance_end"
                                for e in events))
        unit = session.units[1]
        await session.playback_progress(session.epoch, 1, unit.total, done=True)
        await until(lambda: session.reply_completed)

        assert cloud.speech_calls == [text_only.reply.text]
        assert not [event for event in session.events if event["event"] == "reply_error"]
        audio = session.events_named("audio_source")
        assert audio[0]["source"] == "tts"
        assert session.turn_metrics["prediction_audio_hit"] is False
        assert any(isinstance(e, dict) and e.get("type") == "reply_done"
                   for e in events)
    finally:
        await session.close()
