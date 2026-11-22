import json

import httpx
import pytest

from app.local_provider import LocalProvider, MODEL
from app.providers import ProviderError


async def test_local_dialogue_uses_only_loopback_and_no_cloud_credentials():
    provider = LocalProvider(None)
    await provider.client.aclose()
    calls = []
    def handle(request):
        calls.append(request.url.path)
        assert request.url.host == "127.0.0.1" and "authorization" not in request.headers
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"general.architecture": "qwen3"}})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        assert MODEL.encode() in request.content
        assert json.loads(request.content)["think"] is False
        return httpx.Response(200, content='{"message":{"content":"您好"},"done":true}\n'.encode())

    provider.client = httpx.AsyncClient(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handle))
    try:
        await provider.ready(voice=False)
        assert "".join([s async for s in provider.chat([{"role":"user","content":"你好"}])]) == "您好"
        assert calls == ["/api/show", "/api/generate", "/api/chat"]
    finally:
        await provider.close()


async def test_remote_ollama_model_is_rejected_before_dialogue():
    provider = LocalProvider(None)
    await provider.client.aclose()
    def handle(request):
        assert request.url.path == "/api/show"
        return httpx.Response(200, json={"remote_host": "remote", "model_info": {"general.architecture":"qwen3"}})
    provider.client = httpx.AsyncClient(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ProviderError, match="本机"):
            await provider.ready(voice=False)
    finally:
        await provider.close()


async def test_offline_speaker_gets_the_actual_caller_in_the_last_user_message():
    provider = LocalProvider(None)
    await provider.client.aclose()
    from app.brain import Brain
    from app.models import DEFAULT_CARD, Memory

    memory = Memory(DEFAULT_CARD)
    memory.history = [{"role": "assistant", "text": DEFAULT_CARD.opening, "delivery": "complete"},
                      {"role": "user", "text": "我只补了截图"}]
    def handle(request):
        body = json.loads(request.content)
        if "text" in body["format"]["properties"]:
            assert body["messages"][-1] == {"role": "user", "content": "我只补了截图"}
            assert body["format"]["additionalProperties"] is False
            reply = {"text": "请从原入口重新提交。", "covers": []}
        else:
            reply = {"facts": [], "questions": [], "disposition": "active"}
        return httpx.Response(200, content=(json.dumps({"message": {"content": json.dumps(reply)}}) + "\n").encode())

    provider.client = httpx.AsyncClient(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handle))
    try:
        events = [e async for e in provider.respond_local(Brain.snapshot(memory), [])]
        assert [kind for kind, _ in events] == ["say", "update"]
        assert events[0][1].text == "请从原入口重新提交。"
    finally:
        await provider.close()


def test_voice_install_preserves_nested_dictionary_paths_and_reuses_cache(tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile
    from app.local_asr import install_model, model_dir

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:bz2") as tar:
        for name in ("voice/model", "voice/data/nested/phones", "voice/data/index", "voice/unneeded"):
            payload = name.encode()
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
    raw = data.getvalue()
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(raw))
    args = ("tts", "voice", "models", hashlib.sha256(raw).hexdigest(), ("model", "data/nested/phones", "data/index"))
    install_model(*args)
    root = model_dir("tts", "voice")
    assert (root / "data/nested/phones").read_bytes() == b"voice/data/nested/phones"
    assert not (root / "unneeded").exists()
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: pytest.fail("unexpected download"))
    install_model(*args)


async def test_melo_speech_preserves_duration_and_filters_frequencies_above_call_bandwidth():
    from types import SimpleNamespace
    import numpy as np

    rate = 44100
    seconds = np.arange(rate, dtype=np.float32) / rate
    wave = 0.3 * (np.sin(2 * np.pi * 600 * seconds) + np.sin(2 * np.pi * 18000 * seconds))
    provider = LocalProvider(None)
    provider.engine = SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(samples=wave, sample_rate=rate))
    try:
        pcm = b"".join([chunk async for chunk in provider.speech("您好。")])
        samples = np.frombuffer(pcm, dtype="<i2")
        assert len(samples) == 24000
        spectrum = abs(np.fft.rfft(samples))
        # A naive 44.1 to 24 kHz conversion would alias 18 kHz to 6 kHz.
        assert spectrum[6000] < spectrum[600] * 0.01
        assert provider.cached("您好。") == pcm
    finally:
        await provider.close()


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_voice_websocket_uses_transcriber_for_selected_mode(mode, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)
    monkeypatch.setattr(main.LocalProvider, "ready", AsyncMock())
    local_asr = AsyncMock(return_value="local")
    cloud_asr = AsyncMock(return_value="cloud")
    prewarm = AsyncMock()
    monkeypatch.setattr(main.LocalProvider, "transcribe", local_asr)
    monkeypatch.setattr(main.Cloud, "transcribe", cloud_asr)
    monkeypatch.setattr(main.Cloud, "prewarm", prewarm)
    monkeypatch.setattr(main.Session, "start", AsyncMock())

    async def decode(hearing, pcm):
        await hearing.on_partial(await hearing.transcriber(pcm))

    monkeypatch.setattr(main.Hearing, "feed", decode)
    with TestClient(main.app) as client:
        assert len(client.get("/api/config").json()["task_packs"]) == 11
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "voice": True, "audio_protocol": 2, "mode": mode})
            ws.send_bytes(bytes(640))
            assert ws.receive_json()["text"] == mode
            ws.send_json({"type": "stop"})
    if mode == "local":
        cloud_asr.assert_not_called()
        prewarm.assert_not_called()
    else:
        local_asr.assert_not_called()
        prewarm.assert_awaited_once()

def test_invalid_interrupt_applies_no_receipts_and_socket_recovers(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)
    monkeypatch.setattr(main.LocalProvider, "ready", AsyncMock())
    monkeypatch.setattr(main.Session, "start", AsyncMock())
    progress = AsyncMock()
    reset = Mock()
    monkeypatch.setattr(main.Session, "playback_progress", progress)
    monkeypatch.setattr(main.Hearing, "reset", reset)
    with TestClient(main.app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start", "voice": True, "audio_protocol": 2, "mode": "local"})
        ws.send_json({"type": "interrupt", "source": "manual", "epoch": 1,
                      "progress": [{"unit": 1, "samples": 120}, {}]})
        assert ws.receive_json()["type"] == "error"
        progress.assert_not_awaited()
        ws.send_json({"type": "text", "text": 1})
        assert ws.receive_json()["type"] == "error"
        reset.assert_not_called()
        ws.send_text('{"type":"played","epoch":1e999,"unit":1,"samples":120}')
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "played", "epoch": 1, "unit": 1, "samples": 120})
        ws.send_json({"type": "stop"})
    progress.assert_awaited_once_with(1, 1, 120, done=False)
