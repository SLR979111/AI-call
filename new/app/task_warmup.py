"""Background task-card warmup.

After a card is loaded, generate the opening forecast and prepare its audio
through the normal Brain/Cloud path.  Nothing is sent to a browser: this module
only writes the existing forecast/TTS caches so the first real call behaves
like a repeated call.

It is deliberately best-effort: failures are logged and never affect the call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .brain import Brain
from .forecast import OpeningForecastCache
from .models import Memory, TaskCard
from .providers import Cloud

ENABLED_ENV = "TASK_WARMUP_ON_LOAD"
_INFLIGHT: dict[str, asyncio.Task] = {}


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "1").strip().lower() not in {
        "0", "false", "off", "no", "disabled",
    }


def _task_key(card: TaskCard) -> str:
    raw = json.dumps(card.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _log_path(root: Path, key: str) -> Path:
    folder = root / "task-warmup"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return folder / f"{stamp}-{key}.jsonl"


def _write(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


async def warm_task(
    card: TaskCard,
    cloud: Cloud,
    opening_forecasts: OpeningForecastCache | None,
    log_root: Path,
) -> dict:
    """Generate opening forecast + TTS audio without publishing anything."""
    key = _task_key(card)
    path = _log_path(log_root, key)
    started = time.perf_counter()
    _write(path, {"event": "warm_start", "task": card.title, "task_key": key,
                  "ts": datetime.now(timezone.utc).isoformat()})
    try:
        brain = Brain(cloud, opening_forecasts)
        memory = Memory(card)
        snapshot = brain.snapshot(memory)
        snapshot["opening"] = True
        candidates = await brain.predict(snapshot)
        audio_bytes = 0
        prepared = 0
        for candidate in candidates:
            try:
                pcm = await cloud.prefetch(candidate.reply.text)
            except Exception as exc:  # one failed clip must not stop warmup
                _write(path, {"event": "tts_error", "candidate": candidate.id,
                              "error": type(exc).__name__})
                continue
            prepared += 1
            audio_bytes += len(pcm)
            _write(path, {"event": "candidate_ready",
                          "candidate": candidate.id,
                          "intent": candidate.intent,
                          "checkpoint": candidate.checkpoint,
                          "pcm_bytes": len(pcm)})
        summary = {
            "event": "warm_summary",
            "task": card.title,
            "task_key": key,
            "candidates": len(candidates),
            "audio_prepared": prepared,
            "audio_bytes": audio_bytes,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
        _write(path, summary)
        return summary
    except Exception as exc:
        summary = {
            "event": "warm_error",
            "task": card.title,
            "task_key": key,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
        _write(path, summary)
        return summary


def start_task_warmup(
    card: TaskCard,
    cloud: Cloud,
    opening_forecasts: OpeningForecastCache | None,
    log_root: Path,
) -> asyncio.Task | None:
    """Fire-and-forget warmup; one in-flight warmup per task key."""
    if not enabled():
        return None
    key = _task_key(card)
    existing = _INFLIGHT.get(key)
    if existing is not None and not existing.done():
        return existing

    async def runner():
        try:
            await warm_task(card, cloud, opening_forecasts, log_root)
        finally:
            _INFLIGHT.pop(key, None)

    task = asyncio.create_task(runner())
    _INFLIGHT[key] = task
    task.add_done_callback(lambda _t: _INFLIGHT.pop(key, None))
    return task
