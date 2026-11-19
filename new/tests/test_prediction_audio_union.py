"""The LLM hit and the TTS hit are separate: keep both.

A forecast is worth publishing as soon as its text exists. The audio is warmed
afterwards, off the publish path, and written through to the persistent cache so
a later turn that reuses the sentence still starts from PCM even when the
forecast that predicted it is long gone.
"""

from __future__ import annotations

import asyncio

import httpx

from app.models import DEFAULT_CARD, Candidate, Say
from app.providers import Cloud, Settings
from app.session import Session

TEXT = "方便的，您请讲。"
PCM = b"\x01\x00" * 960


def candidate(candidate_id: str, reply: str) -> Candidate:
    return Candidate(id=candidate_id, when="是的", examples=[], reply=Say(text=reply))


class Forecast:
    async def predict(self, snapshot):
        return [candidate("c1", TEXT)]


class SlowCloud:
    """Publishes text immediately while the synthesis is still running."""

    def __init__(self):
        self.started = asyncio.Event()

    async def prefetch(self, text):
        self.started.set()
        await asyncio.Event().wait()

    def cached(self, text):
        return None

    async def speech(self, text):  # pragma: no cover - never reached
        yield b""


async def test_text_is_published_before_its_audio_finishes():
    cloud = SlowCloud()
    session = Session(DEFAULT_CARD, cloud, Forecast(), _send, voice=True)
    try:
        await session._predict(0, {})
        await asyncio.wait_for(cloud.started.wait(), 1)
        # The candidate is usable for reuse even though no PCM exists yet.
        assert [item.id for item in session.candidates] == ["c1"]
        assert session.prediction_audio == {}
        assert session.audio_warm
    finally:
        await session.close()


async def test_warmed_audio_is_reused_by_a_later_session(tmp_path):
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    cache_path = str(tmp_path / "cache.sqlite3")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = Cloud(Settings(api_key="test"), client, cache_path=cache_path)
        session = Session(DEFAULT_CARD, cloud, Forecast(), _send, voice=True)
        try:
            await session._predict(0, {})
            await session.settle_audio()
            assert session.prediction_audio[TEXT] == PCM
        finally:
            await session.close()

        # A second process (a fresh Cloud on the same disk cache) reuses the
        # audio the first conversation predicted, with no provider request.
        replay = Cloud(Settings(api_key="test"), client, cache_path=cache_path)
        assert replay.cached(TEXT) == PCM
        assert len(requests) == 1
        await replay.close()


async def test_reuse_after_forecast_cancel_still_finds_cached_audio(tmp_path):
    """Cancelling the forecast must not throw away the synthesis it started."""
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    cache_path = str(tmp_path / "cache.sqlite3")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = Cloud(Settings(api_key="test"), client, cache_path=cache_path)
        session = Session(DEFAULT_CARD, cloud, Forecast(), _send, voice=True)
        try:
            task = session.spawn(session._predict(0, {}))
            await _wait_for(lambda: bool(session.audio_warm), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # The turn moved on; the warm-up keeps running and lands on disk.
            await asyncio.wait_for(_drain_until(lambda: cloud.cached(TEXT) == PCM), 2)
            assert cloud.cached(TEXT) == PCM
        finally:
            release.set()
            await session.close()

        replay = Cloud(Settings(api_key="test"), client, cache_path=cache_path)
        assert replay.cached(TEXT) == PCM
        await replay.close()


async def _send(payload):
    pass


async def _wait_for(predicate, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError()
        await asyncio.sleep(0)


async def _drain_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)
