import json
import sqlite3

from app.cache_audit import CacheAudit
from app.enhanced_cache import EnhancedASRCache, EnhancedLLMCache
from scripts.backfill_cache import backfill


def test_audit_and_backfill_restore_keys(tmp_path):
    audit_path = tmp_path / "cache-audit.jsonl"
    audit = CacheAudit(audit_path)
    messages = [{"role": "user", "content": "测试"}]
    audit.llm(messages=messages, model="deepseek-flash", temperature=0.2,
              max_tokens=650, response="缓存回复")
    pcm = b"\x01\x00" * 960
    audit.tts(text="缓存回复", voice="stella", pcm=pcm)
    audit.asr(pcm=b"\x02\x00" * 320, text="测试识别")

    cache_path = tmp_path / "cache.sqlite3"
    counts = backfill(audit_path, cache_path)
    assert counts["llm"]["written"] == 1
    assert counts["tts"]["written"] == 1
    assert counts["asr"]["written"] == 1

    llm = EnhancedLLMCache()
    asr = EnhancedASRCache()
    llm_key = llm._make_key(messages, "deepseek-flash", 0.2, 650)
    asr_key = asr._make_key(b"\x02\x00" * 320)
    connection = sqlite3.connect(cache_path)
    try:
        rows = dict(connection.execute(
            "SELECT namespace || ':' || key, value FROM cache_entries"
        ).fetchall())
    finally:
        connection.close()
    assert rows[f"llm:{llm_key}"] == "缓存回复".encode()
    assert rows[f"asr:{asr_key}"] == "测试识别".encode()
    assert rows[f"tts:stella\0缓存回复"] == pcm


def test_dry_run_does_not_write_cache(tmp_path):
    audit_path = tmp_path / "cache-audit.jsonl"
    CacheAudit(audit_path).llm(
        messages=[{"role": "user", "content": "x"}],
        model="deepseek-flash", temperature=0.2, max_tokens=650,
        response="y",
    )
    cache_path = tmp_path / "cache.sqlite3"
    counts = backfill(audit_path, cache_path, dry_run=True)
    assert counts["llm"]["written"] == 1
    assert not cache_path.exists()
