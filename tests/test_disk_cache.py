"""持久化缓存：LLM 与 TTS 的磁盘命中都不再走云端/推理。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.cache import DiskCache, cache_key, reset_caches
from app.cache.persistent import llm_cache, tts_cache
from app.llm.router import LLMRouter, StreamChunk


def test_cache_key_is_stable_and_order_insensitive():
    assert cache_key("a", 1, None) == cache_key("a", 1, None)
    assert cache_key("a", {"x": 1, "y": 2}) == cache_key("a", {"y": 2, "x": 1})
    assert cache_key("a", 1) != cache_key("a", 2)


def test_disk_cache_roundtrip_and_prune(tmp_path):
    store = DiskCache(tmp_path, max_mb=1)
    store.put_bytes("pcm", cache_key("k1"), b"\x00\x01" * 100)
    assert store.get_bytes("pcm", cache_key("k1")) == b"\x00\x01" * 100
    store.put_json("llm", cache_key("k2"), {"text": "您好"})
    assert store.get_json("llm", cache_key("k2")) == {"text": "您好"}
    assert store.get_bytes("pcm", cache_key("nope")) is None
    assert store.stats()["entries"] == 2


def test_llm_cache_hit_skips_provider(tmp_path, monkeypatch):
    reset_caches()
    monkeypatch.setenv("DISK_CACHE_DIR", str(tmp_path))
    store = llm_cache()
    router = LLMRouter(primary=_provider(), bus=None, transport=_NeverTransport())
    key = cache_key("llm-v1", "deepseek-flash", [{"role": "user", "content": "hi"}], 0.3, 256, None)
    store.put_json("llm", key, {"text": "您好，我帮您查一下。", "provider": "deepseek", "model": "deepseek-flash"})

    chunks = asyncio.run(_collect(router, [{"role": "user", "content": "hi"}], tools=None, max_tokens=256))
    assert chunks[-1].type == "done"
    assert chunks[-1].text == "您好，我帮您查一下。"
    assert any(c.type == "sentence" for c in chunks)
    reset_caches()


def test_llm_cache_ignored_when_tools_are_requested(tmp_path, monkeypatch):
    reset_caches()
    monkeypatch.setenv("DISK_CACHE_DIR", str(tmp_path))
    llm_cache().put_json("llm", cache_key("llm-v1", "deepseek-flash", [{"role": "user", "content": "hi"}], 0.3, 256, None),
                         {"text": "cached", "provider": "deepseek", "model": "deepseek-flash"})
    router = LLMRouter(primary=_provider(), bus=None, transport=_NeverTransport())
    chunks = asyncio.run(_collect(router, [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))
    assert chunks[-1].type == "error"
    reset_caches()


def _provider():
    from app.llm.router import ProviderConfig
    return ProviderConfig(name="deepseek", model="deepseek-flash", base_url="https://api.deepseek.com/v1",
                          api_key="sk-test", price_per_1k_cny=0.001)


async def _collect(router, messages, tools=None, max_tokens=512):
    out = []
    async for chunk in router.complete_stream(messages, tools=tools, max_tokens=max_tokens):
        out.append(chunk)
    return out


class _NeverTransport:
    """Any real HTTP call is a test failure."""

    async def handle_async_request(self, request):  # pragma: no cover
        raise AssertionError("cache miss must not reach the network in these tests")
