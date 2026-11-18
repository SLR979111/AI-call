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
