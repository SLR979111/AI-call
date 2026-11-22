#!/usr/bin/env python3
"""Rebuild the SQLite cache from the provider cache audit.

The audit is written by ``app/cache_audit.py`` at the provider boundary.
It is intentionally separate from the session JSONL: session logs do not
contain the full model messages, model parameters, or raw PCM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.enhanced_cache import EnhancedASRCache, EnhancedLLMCache
from app.persistent_cache import PersistentCache

LLM_MAX_ENTRIES = 500
LLM_MAX_BYTES = 10_000_000
ASR_MAX_ENTRIES = 200
ASR_MAX_BYTES = 20_000_000
TTS_MAX_ENTRIES = 48
TTS_MAX_BYTES = 12_000_000


def _record_exists(cache: Path, namespace: str, key: str) -> bool:
    if not cache.exists():
        return False
    connection = sqlite3.connect(str(cache))
    try:
        row = connection.execute(
            "SELECT 1 FROM cache_entries WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _pcm_path(audit_path: Path, record: dict) -> Path | None:
    raw = record.get("pcm_file")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = audit_path.parent / path
    if path.is_file() or path.parent.name != "cache-audit-pcm":
        return path
    # Older writers included the original runtime directory in the stored path.
    return audit_path.parent / "cache-audit-pcm" / path.name


def backfill(audit_path: Path, cache_path: Path, *, dry_run: bool = False) -> dict:
    llm = EnhancedLLMCache()
    asr = EnhancedASRCache()
    store = None if dry_run else PersistentCache(cache_path)
    counts = {name: {"read": 0, "written": 0, "already": 0, "skipped": 0}
              for name in ("llm", "asr", "tts")}
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if not isinstance(kind, str) or kind not in counts:
            continue
        counts[kind]["read"] += 1
        if kind == "llm":
            messages = record.get("messages")
            response = record.get("response")
            model = record.get("model")
            if not isinstance(messages, list) or not isinstance(response, str) or not model:
                counts[kind]["skipped"] += 1
                continue
            key = llm._make_key(messages, str(model),
                                float(record.get("temperature", 0.2)),
                                int(record.get("max_tokens", 650)))
            if _record_exists(cache_path, "llm", key):
                counts[kind]["already"] += 1
                continue
            if not dry_run:
                store.put("llm", key, response.encode("utf-8"),
                          max_entries=LLM_MAX_ENTRIES, max_bytes=LLM_MAX_BYTES)
            counts[kind]["written"] += 1
        elif kind == "tts":
            text = record.get("text")
            voice = record.get("voice")
            pcm_path = _pcm_path(audit_path, record)
            if not isinstance(text, str) or not voice or pcm_path is None or not pcm_path.exists():
                counts[kind]["skipped"] += 1
                continue
            pcm = pcm_path.read_bytes()
            key = f"{voice}\0{text}"
            if _record_exists(cache_path, "tts", key):
                counts[kind]["already"] += 1
                continue
            if not dry_run:
                store.put("tts", key, pcm,
                          max_entries=TTS_MAX_ENTRIES, max_bytes=TTS_MAX_BYTES)
            counts[kind]["written"] += 1
        else:
            pcm_path = _pcm_path(audit_path, record)
            text = record.get("text")
            if pcm_path is None or not pcm_path.exists() or not isinstance(text, str):
                counts[kind]["skipped"] += 1
                continue
            pcm = pcm_path.read_bytes()
            key = hashlib.sha256(pcm).hexdigest()[:32]
            if _record_exists(cache_path, "asr", key):
                counts[kind]["already"] += 1
                continue
            if not dry_run:
                store.put("asr", key, text.encode("utf-8"),
                          max_entries=ASR_MAX_ENTRIES, max_bytes=ASR_MAX_BYTES)
            counts[kind]["written"] += 1
    if store is not None:
        store.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=Path("runtime/cache-audit.jsonl"))
    parser.add_argument("--cache", type=Path, default=Path("runtime/cache.sqlite3"))
    parser.add_argument("--dry-run", action="store_true",
                        help="compute keys and report without writing")
    args = parser.parse_args()
    if not args.audit.exists():
        print(f"audit not found: {args.audit}")
        return 1
    counts = backfill(args.audit, args.cache, dry_run=args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "counts": counts},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
