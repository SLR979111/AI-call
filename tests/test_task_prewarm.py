import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import DEFAULT_CARD, Say
from app.playback import Unit
from app.speech_start import first_sentence
from app.task_prewarm import TaskPrewarmer


@pytest.mark.parametrize("opening, first", [
    ("您好，我是工单客服。请问现在方便吗？", "您好，我是工单客服。"),
    ("您好！", "您好！"), ("现在方便吗", "现在方便吗"),
    ("“您好！”然后呢？", "“您好！”"), ("Hello. Can we talk?", "Hello."),
    ("版本 1.5 可以使用。需要说明吗？", "版本 1.5 可以使用。"),
])
def test_only_first_sentence_is_prepared(opening, first):
    assert first_sentence(opening) == first


async def test_shared_opening_audio_survives_abandoned_wait_and_plays_with_live_suffix(monkeypatch):
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    started, release = asyncio.Event(), asyncio.Event()
    first, suffix = "您好，我是图书室客服。", "您想了解开放时间吗？"
    audio, warmed, live = {}, [], []

    class Voice:
        cached = staticmethod(audio.get)

        async def warm(self, text):
            warmed.append(text)
            started.set()
            await release.wait()
            audio[text] = text.encode("utf-16-le")
            return audio[text]

        async def speech(self, text):
            live.append(text)
            yield text.encode("utf-16-le")

    owner, voice = TaskPrewarmer(), Voice()
    opening = first + suffix
    assert (await owner.prepare(opening, voice))["status"] == "preparing"
    await asyncio.wait_for(started.wait(), 2)
    waiter = asyncio.create_task(owner.prepare(opening, voice, wait=True))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert (await owner.prepare(first + "另一个问题。", voice))["status"] == "preparing"
    release.set()
    assert (await owner.prepare(opening, voice, wait=True))["status"] == "ready"
    assert await owner.prepare(opening, voice) == {"started": False, "status": "ready"}
    assert warmed == [first]
    assert not live

    unit = Unit(1, Say(text=opening), {}, is_opening=True)
    await unit.synthesize(voice, None, None, None, None)
    pcm = bytearray()
    while (chunk := await unit.queue.get()) is not None:
        pcm.extend(chunk)
    assert pcm.decode("utf-16-le") == opening
    assert live == [suffix]
    await owner.close()


async def test_disabled_failed_and_explicit_retry(monkeypatch):
    class Voice:
        cached = staticmethod(lambda _: None)
        warm = AsyncMock(side_effect=[None, b"pcm"])
    owner = TaskPrewarmer()
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "0")
    assert (await owner.prepare("您好", Voice()))["status"] == "disabled"
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    for retry, status in [(False, "failed"), (False, "failed"), (True, "ready")]:
        assert (await owner.prepare("您好", Voice(), wait=True, retry=retry))["status"] == status
    assert Voice.warm.await_count == 2
    await owner.close()


async def test_shutdown_cancels_opening_job(monkeypatch):
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Voice:
        cached = staticmethod(lambda _: None)
        async def warm(self, text):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    owner = TaskPrewarmer()
    await owner.prepare("您好。", Voice())
    await asyncio.wait_for(entered.wait(), 2)
    await owner.close()
    assert stopped.is_set() and not owner.active


def test_http_prepare_only_synthesizes_confirmed_first_sentence(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test")
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)
    plan = AsyncMock(side_effect=AssertionError("preparation ran a planner"))
    session = AsyncMock(side_effect=AssertionError("preparation started a dialogue"))
    monkeypatch.setattr(main, "generate_phone_file", plan)
    monkeypatch.setattr(main.Session, "start", session)
    warm = AsyncMock(return_value=b"pcm")
    monkeypatch.setattr(main.Cloud, "warm", warm)
    with TestClient(main.app) as client:
        doc = main.compile_phone(DEFAULT_CARD).model_dump()
        doc["basic_response"]["opening"] = "您好，新版工单客服。您现在方便吗？"
        resp = client.post("/api/warm", json={"phone_file": doc, "wait": True})
        assert resp.json()["status"] == "ready"
        warm.assert_awaited_once_with("您好，新版工单客服。")
        plan.assert_not_called()
        session.assert_not_called()
