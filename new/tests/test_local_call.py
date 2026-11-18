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
