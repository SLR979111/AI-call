"""Cross-restart persistence and key-correctness for the shared cache."""

import json
from pathlib import Path

import pytest

from app.enhanced_cache import EnhancedASRCache, EnhancedLLMCache
from app.forecast import OpeningForecastCache
from app.models import Candidate, Say
from app.persistent_cache import PersistentCache
from app.providers import Cloud, Settings


def test_llm_cache_survives_reopen(tmp_path):
    path = tmp_path / "cache.sqlite3"
    messages = [
        {"role": "system", "content": "task card"},
        {"role": "user", "content": "你好"},
    ]

    disk = PersistentCache(path)
    first = EnhancedLLMCache(store=disk)
    first.put(messages, "deepseek-flash", 0.2, "您好，请讲。")
    disk.close()

    disk = PersistentCache(path)
    second = EnhancedLLMCache(store=disk)
    assert second.get(messages, "deepseek-flash", 0.2) == "您好，请讲。"
    assert second.stats()["disk"]["entries"] == 1
    disk.close()


def test_llm_key_covers_full_history_not_only_recent_messages(tmp_path):
    disk = PersistentCache(tmp_path / "cache.sqlite3")
    cache = EnhancedLLMCache(store=disk)
    history = [{"role": "system", "content": "任务卡 A"}] + [
        {"role": "user", "content": f"第{i}句"} for i in range(8)
    ]
    other = [{"role": "system", "content": "任务卡 B"}] + history[1:]

    cache.put(history, "deepseek-flash", 0.2, "A 的回答", max_tokens=100)
    assert cache.get(other, "deepseek-flash", 0.2) is None
    assert cache.get(history, "deepseek-flash", 0.2, max_tokens=200) is None
    assert cache.get(history, "deepseek-flash", 0.2, max_tokens=100) == "A 的回答"
    disk.close()


def test_asr_cache_survives_reopen_and_hashes_full_audio(tmp_path):
    path = tmp_path / "cache.sqlite3"
    pcm = b"\x01\x00" * 400
    other = b"\x01\x00" * 200 + b"\x02\x00" * 200

    disk = PersistentCache(path)
    first = EnhancedASRCache(store=disk)
    first.put(pcm, "第一段")
    assert first.get(other) is None
    disk.close()

    disk = PersistentCache(path)
    second = EnhancedASRCache(store=disk)
    assert second.get(pcm) == "第一段"
    assert second.get(other) is None
    disk.close()


async def test_tts_cache_survives_reopen_and_is_voice_scoped(tmp_path):
    path = tmp_path / "cache.sqlite3"
    pcm = b"\x11\x00" * 20

    cloud = Cloud(Settings(api_key="test", voice="voice-a"), cache_path=path)
    cloud.remember("好的。", pcm)
    await cloud.close()

    other_voice = Cloud(Settings(api_key="test", voice="voice-b"), cache_path=path)
    assert other_voice.cached("好的。") is None
    await other_voice.close()

    same_voice = Cloud(Settings(api_key="test", voice="voice-a"), cache_path=path)
    assert same_voice.cached("好的。") == pcm
    await same_voice.close()


def test_opening_forecast_cache_survives_reopen(tmp_path):
    path = tmp_path / "cache.sqlite3"
    key = json.dumps({"task": "card", "model": "deepseek-flash"}, ensure_ascii=False)
    item = Candidate(
        id="p0_0", when="用户只是应一声",
        reply=Say(text="好的。"), covers=["opening"],
    )

    disk = PersistentCache(path)
    first = OpeningForecastCache(disk)
    first.store(key, [item], complete=True)
    disk.close()

    disk = PersistentCache(path)
    second = OpeningForecastCache(disk)
    items, complete = second.get(key)
    assert complete is True
    assert [entry.model_dump() for entry in items] == [item.model_dump()]
    disk.close()
