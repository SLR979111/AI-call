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

def test_relative_and_historical_pcm_paths_restore_after_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    log = Path("runtime/audit.jsonl")
    audit = CacheAudit(log)
    audit.tts(text="一", voice="v", pcm=b"1234")
    original = json.loads(log.read_text(encoding="utf-8"))
    historical = dict(original, text="二", pcm_file=str(Path("runtime") / original["pcm_file"]))
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(historical) + "\n")
    Path("runtime").rename("moved")
    result = backfill(Path("moved/audit.jsonl"), Path("restored.sqlite"))
    assert result["tts"]["written"] == 2


def test_unwritable_audit_and_interrupted_pcm_write(tmp_path, monkeypatch):
    from app import cache_audit

    blocked = tmp_path / "file"
    blocked.touch()
    unavailable = CacheAudit(blocked / "audit.jsonl")
    assert not unavailable.enabled
    log = tmp_path / "available" / "audit.jsonl"
    audit = CacheAudit(log)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(cache_audit.os, "replace", fail)
    audit.tts(text="语音", voice="v", pcm=b"1234")
    assert not log.exists()
    assert not list(audit.pcm_dir.iterdir())
