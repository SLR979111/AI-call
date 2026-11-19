"""Background full-dialogue warmup for the next real call.

This is deliberately not a UI feature.  After a task card is loaded (or when
the page asks the server to warm it), a synthetic Session runs the same normal
Brain/Cloud path against preset turns.  All transport frames and PCM are
discarded by a sink; only the existing model/TTS caches and a separate JSONL
audit log are written.

When the real call starts, ``cancel_task_prewarm`` interrupts this work
immediately.  Whatever was prepared before that point remains in the caches.
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
from .decision_model import DecisionModel
from .forecast import OpeningForecastCache
from .models import Memory, TaskCard
from .providers import Cloud
from .session import Session
from .ticket_forecast import generate_ticket_candidates

ENABLED_ENV = "TASK_PREWARM_ON_LOAD"
PREWARM_TASKS: dict[str, asyncio.Task] = {}
PREWARM_SESSIONS: dict[str, Session] = {}
FOREGROUND_BLOCK: dict[str, float] = {}

OFFICIAL_TURNS = (
    "工单到哪一步了？",
    "还要补材料吗？",
    "多久能出结果？",
    "审核通过了吗？",
    "催一下",
    "材料交给谁",
)

ADVERSARIAL_TURNS = (
    "不是，我不负责，而且还有一个别的问题。",
    "你保证能过吗？",
    "我要投诉，别说了。",
    "换个人，我不想跟你说。",
    "我不需要补",
    "先别问了",
)


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "1").strip().lower() not in {
        "0", "false", "off", "no", "disabled",
    }


def task_key(card: TaskCard) -> str:
    raw = json.dumps(card.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _log_path(root: Path, key: str) -> Path:
    folder = root / "task-prewarm"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return folder / f"{stamp}-{key}.jsonl"


def _write(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def preset_dialogues(card: TaskCard) -> list[tuple[str, list[str]]]:
    """Same card, several realistic ways a caller can traverse it."""
    ticket_turns: list[str] = []
    for candidate in generate_ticket_candidates(card):
        ticket_turns.extend(candidate.examples)
    # Add short answers only if the card itself produced ticket candidates.
    if ticket_turns:
        ticket_turns.extend(["好的。", "是的。", "已经提交了，显示审核中。"])
    card_turns = list(dict.fromkeys(ticket_turns))
    if len(card_turns) > 8:
        card_turns = card_turns[:6] + card_turns[-3:]
    return [
        ("card", card_turns),
        ("official", list(OFFICIAL_TURNS)),
        ("adversarial", list(ADVERSARIAL_TURNS)),
    ]


class _Sink:
    """Transport sink: frames are counted and discarded, never sent."""

    def __init__(self, log_path: Path, scenario: str):
        self.log_path = log_path
        self.scenario = scenario
        self.replies: list[int] = []
        self.frames = 0
        self.audio_bytes = 0

    async def __call__(self, payload: dict | bytes) -> None:
        self.frames += 1
        if isinstance(payload, bytes):
            self.audio_bytes += len(payload)
            return
        if payload.get("type") == "reply_done":
            epoch = int(payload.get("epoch", 0))
            self.replies.append(epoch)
            _write(self.log_path, {"event": "reply_done", "scenario": self.scenario,
                                   "epoch": epoch,
                                   "reused": payload.get("reused", False)})
        elif payload.get("type") == "error":
            _write(self.log_path, {"event": "session_error", "scenario": self.scenario,
                                   "message": str(payload.get("message", ""))[:200]})


async def _wait_reply(sink: _Sink, after: int, timeout: float = 35.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(epoch > after for epoch in sink.replies):
            return True
        await asyncio.sleep(0.05)
    return False


async def _run_one(card: TaskCard, cloud: Cloud,
                   opening_forecasts: OpeningForecastCache | None,
                   log_path: Path, scenario: str, turns: list[str],
                   session_key: str) -> None:
    if not turns:
        _write(log_path, {"event": "scenario_skipped", "scenario": scenario,
                          "reason": "no preset turns"})
        return
    sink = _Sink(log_path, scenario)
    brain = Brain(
        cloud, opening_forecasts,
        ticket_forecasts=True,
        decision_model=DecisionModel(cloud),
    )
    session = Session(
        card, cloud, brain, sink,
        voice=True,
        predictions=True,
        log_dir=log_path.parent / "sessions",
        on_hangup=lambda: None,
    )
    PREWARM_SESSIONS[session_key] = session
    _write(log_path, {"event": "scenario_start", "scenario": scenario,
                      "turns": len(turns), "task": card.title})
    started = time.perf_counter()
    try:
        await session.start()
        if not await _wait_reply(sink, 0):
            _write(log_path, {"event": "scenario_timeout", "scenario": scenario,
                              "stage": "opening"})
            return
        last = max(sink.replies)
        for index, text in enumerate(turns, 1):
            if session.closed or session.ended:
                break
            before = last
            await session.user_text(text, source="prewarm")
            _write(log_path, {"event": "turn", "scenario": scenario,
                              "turn": index, "input": text})
            if not await _wait_reply(sink, before):
                _write(log_path, {"event": "scenario_timeout", "scenario": scenario,
                                  "stage": f"turn_{index}"})
                break
            last = max(sink.replies)
        _write(log_path, {"event": "scenario_done", "scenario": scenario,
                          "turns": len(turns),
                          "elapsed_ms": round((time.perf_counter() - started) * 1000),
                          "frames": sink.frames,
                          "audio_bytes": sink.audio_bytes})
    finally:
        try:
            await session.close()
        except Exception:
            pass
        PREWARM_SESSIONS.pop(session_key, None)


async def warm_task(card: TaskCard, cloud: Cloud,
                    opening_forecasts: OpeningForecastCache | None,
                    log_root: Path) -> dict:
    key = task_key(card)
    path = _log_path(log_root, key)
    started = time.perf_counter()
    _write(path, {"event": "prewarm_start", "task": card.title, "task_key": key,
                  "ts": datetime.now(timezone.utc).isoformat()})
    try:
        for name, turns in preset_dialogues(card):
            await _run_one(card, cloud, opening_forecasts, path, name, turns, key)
        summary = {"event": "prewarm_summary", "task": card.title, "task_key": key,
                   "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        _write(path, summary)
        return summary
    except asyncio.CancelledError:
        _write(path, {"event": "prewarm_cancelled", "task": card.title,
                      "task_key": key,
                      "elapsed_ms": round((time.perf_counter() - started) * 1000)})
        raise
    except Exception as exc:
        summary = {"event": "prewarm_error", "task": card.title, "task_key": key,
                   "error": type(exc).__name__, "message": str(exc)[:300],
                   "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        _write(path, summary)
        return summary


def start_task_prewarm(card: TaskCard, cloud: Cloud,
                       opening_forecasts: OpeningForecastCache | None,
                       log_root: Path) -> asyncio.Task | None:
    if not enabled():
        return None
    key = task_key(card)
    if time.monotonic() < FOREGROUND_BLOCK.get(key, 0.0):
        return None
    existing = PREWARM_TASKS.get(key)
    if existing is not None and not existing.done():
        return existing

    async def runner():
        try:
            await warm_task(card, cloud, opening_forecasts, log_root)
        finally:
            PREWARM_TASKS.pop(key, None)

    task = asyncio.create_task(runner())
    PREWARM_TASKS[key] = task
    task.add_done_callback(lambda _t: PREWARM_TASKS.pop(key, None))
    return task


def cancel_task_prewarm(card_or_key: TaskCard | str, reason: str = "foreground") -> bool:
    key = card_or_key if isinstance(card_or_key, str) else task_key(card_or_key)
    # The browser may have already issued /api/warm before the real start
    # event reaches the server. Block any late prewarm for a short window.
    FOREGROUND_BLOCK[key] = time.monotonic() + 5.0
    task = PREWARM_TASKS.get(key)
    session = PREWARM_SESSIONS.get(key)
    if task is None and session is None:
        return False
    if session is not None and not session.closed:
        # Cancel without waiting: this is called from the real-call start path.
        asyncio.create_task(session.close())
    if task is not None and not task.done():
        task.cancel()
    return True
