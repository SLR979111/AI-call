#!/usr/bin/env python3
"""Real-service cache audit/backfill smoke test.

Uses the configured provider credentials; makes a small number of calls.
Two text passes exercise the LLM cache; two speech calls exercise TTS.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import Brain
from app.models import DEFAULT_CARD
from app.providers import Cloud, Settings
from app.session import Session
from scripts.backfill_cache import backfill


async def text_pass(cloud: Cloud, text: str):
    events = []

    async def send(event):
        if isinstance(event, dict):
            events.append(event)

    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=False,
                      predictions=False)
    try:
        await session.start()
        await asyncio.wait_for(session.response, 40)
        await session.user_text(text)
        await asyncio.wait_for(session.response, 40)
        return {
            "text": text,
            "first_text_ms": session.turn_metrics.get("first_text_ms"),
            "llm_cache_hit": cloud.last_llm_cache_hit,
            "assistant": [e.get("text") for e in events if e.get("type") == "utterance"],
            "errors": [e for e in events if e.get("type") == "error"],
        }
    finally:
        await session.close()


async def main():
    settings = Settings.load()
    cache = ROOT / "runtime" / "cache-audit-live.sqlite3"
    fresh = ROOT / "runtime" / "cache-audit-backfill.sqlite3"
    audit = ROOT / "runtime" / "cache-audit-live.jsonl"
    for path in (cache, fresh, audit):
        if path.exists():
            path.unlink()
    shutil.rmtree(ROOT / "runtime" / "cache-audit-pcm", ignore_errors=True)

    report = {"passes": [], "tts": [], "backfill": None}
    cloud = Cloud(settings, cache_path=cache, cache_audit_path=audit)
    try:
        report["passes"].append(await text_pass(cloud, "我想补材料，应该从哪里开始？"))
        report["passes"].append(await text_pass(cloud, "截图要包含哪些信息？"))
        try:
            pcm1 = await cloud.prefetch("您好，这里是资料核对说明。")
            hit1 = cloud.last_tts_cache_hit
            pcm2 = await cloud.prefetch("您好，这里是资料核对说明。")
            hit2 = cloud.last_tts_cache_hit
            report["tts"] = [{"bytes": len(pcm1), "hit": hit1},
                             {"bytes": len(pcm2), "hit": hit2}]
        except Exception as exc:  # noqa: BLE001 - report provider failures
            report["tts"] = [{"error": type(exc).__name__, "detail": str(exc)[:200]}]
    finally:
        await cloud.close()

    if not audit.exists():
        report["backfill"] = {"skipped": "audit missing; no successful provider calls"}
    else:
        counts = backfill(audit, fresh)
        report["backfill"] = counts
        restored = Cloud(settings, cache_path=fresh, cache_audit_path="")
        try:
            report["passes"].append(await text_pass(restored, "我想补材料，应该从哪里开始？"))
        finally:
            await restored.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not any(turn["errors"] for turn in report["passes"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
