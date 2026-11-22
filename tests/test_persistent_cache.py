"""Cross-restart persistence and key-correctness for the shared cache."""

import json
import sqlite3
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


def test_failed_trim_rolls_back_write_for_other_connections(tmp_path):
    path = tmp_path / "cache.sqlite3"
    disk = PersistentCache(path)
    try:
        disk.put("tts", "old", b"A", max_entries=1, max_bytes=20)
        with sqlite3.connect(path) as other:
            other.execute("CREATE TRIGGER reject_trim BEFORE DELETE ON cache_entries "
                          "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
                disk.put("tts", "new", b"B", max_entries=1, max_bytes=20)
            assert other.execute("SELECT key FROM cache_entries").fetchall() == [("old",)]
    finally:
        disk.close()


def test_replacing_full_text_cache_preserves_other_entries():
    cache = EnhancedASRCache(max_entries=2, max_size_mb=8 / (1024 * 1024))
    cache.put(b"a" * 100, "aa")
    cache.put(b"b" * 100, "bb")
    cache.put(b"b" * 100, "ccc")
    assert cache.get(b"a" * 100) == "aa"
    assert cache.stats()["evictions"] == 0
    assert cache.get(b"b" * 100) == "ccc"


async def test_tts_disk_survives_ram_eviction_and_counts_each_hit_once(tmp_path, monkeypatch):
    from app import providers
    monkeypatch.setattr(providers, "TTS_MEMORY_ENTRIES", 1)
    monkeypatch.setattr(providers, "TTS_MEMORY_BYTES", 4)
    path = tmp_path / "cache.sqlite3"
    cloud = Cloud(Settings(api_key="test"), cache_path=path)
    try:
        cloud.remember("one", b"12")
        cloud.remember("two", b"34")
        cloud.remember("long", b"123456")
        assert cloud.cached("one") == b"12"
        assert cloud.cached("long") == b"123456"
        assert list(cloud.cache.values()) == [b"12"]
        records = cloud.persistent.load("tts", max_entries=3)
        assert {r.value: r.access_count for r in records} == {b"12": 2, b"34": 1, b"123456": 2}
    finally:
        await cloud.close()
    reopened = Cloud(Settings(api_key="test"), cache_path=path)
    try:
        assert len(reopened.cache) == 1 and reopened.cache_bytes <= 4
        assert reopened.cached("two") == b"34"
    finally:
        await reopened.close()


async def test_live_cache_write_failure_preserves_ram_and_recovers(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cloud = Cloud(Settings(api_key="test"), cache_path=path)
    try:
        with sqlite3.connect(path) as other:
            other.execute("CREATE TRIGGER no_write BEFORE INSERT ON cache_entries "
                          "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        cloud.remember("one", b"12")
        assert cloud.cached("one") == b"12"
        with sqlite3.connect(path) as other:
            other.execute("DROP TRIGGER no_write")
        cloud.remember("two", b"34")
        assert cloud.persistent.stats("tts")["entries"] == 1
    finally:
        await cloud.close()


async def test_unusable_cache_path_still_allows_calls(tmp_path):
    cloud = Cloud(Settings(api_key="test"), cache_path=tmp_path)
    try:
        cloud.remember("one", b"12")
        assert cloud.persistent is None and cloud.cached("one") == b"12"
    finally:
        await cloud.close()
