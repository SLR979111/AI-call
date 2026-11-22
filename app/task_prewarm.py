"""Background full-dialogue warmup for the next real call.

After a task is loaded, synthetic Sessions run the normal Brain/Cloud path
against several preset scenarios.  All transport frames and PCM go to a sink;
only model/TTS caches and a separate JSONL audit log are kept.

Scenarios run in parallel and repeat until a real call starts.  The real start
cancels the whole group immediately; cache entries already written remain.

This module is intentionally standalone: it does not depend on prediction
candidate generation or the rule engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .brain import Brain
from .forecast import OpeningForecastCache
from .models import TaskCard
from .providers import Cloud
from .session import Session

ENABLED_ENV = "TASK_PREWARM_ON_LOAD"
CONCURRENCY_ENV = "TASK_PREWARM_CONCURRENCY"
LOOP_DELAY_ENV = "TASK_PREWARM_LOOP_DELAY"
MAX_ROUNDS_ENV = "TASK_PREWARM_MAX_ROUNDS"
DEFAULT_CONCURRENCY = 4
DEFAULT_LOOP_DELAY = 1.0

OFFICIAL_TURNS = (
    "工单到哪一步了？",
    "还要补材料吗？",
    "多久能出结果？",
    "审核通过了吗？",
    "催一下",
    "材料交给谁",
)
OFFICIAL_STATUS_TURNS = (
    "现在是什么状态？",
    "审核到哪一步了？",
    "材料交上去了吗？",
    "能不能加急？",
)
ADVERSARIAL_TURNS = (
    "不是，我不负责，而且还有一个别的问题。",
    "你保证能过吗？",
    "我要投诉，别说了。",
    "换个人，我不想跟你说。",
    "我不需要补",
    "先别问了",
)
ADVERSARIAL_TURNS_2 = (
    "我不方便，晚点再说。",
    "你们必须今天给我处理完。",
    "我不信你，转人工。",
    "我拒绝提供这个材料。",
    "你别问了，直接告诉我结果。",
)
SHORT_TURNS = ("好的。", "是的。", "明白了。", "没问题。", "收到。")
ENGLISH_TURNS = ("ok", "yes", "got it", "no thanks", "not now")
CARD_TURNS = (
    "具体缺什么材料？",
    "怎么补？",
    "已经提交了，显示审核中。",
    "只是保存，还没有提交。",
    "能加急吗？",
    "审核通过了吗？",
)


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "1").strip().lower() not in {
        "0", "false", "off", "no", "disabled",
    }


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def task_key(card: TaskCard) -> str:
    raw = json.dumps(card.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _log_path(root: Path, key: str) -> Path:
    folder = root / "task-prewarm"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return folder / f"{stamp}-{key}.jsonl"


def _write(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size) if items[i:i + size]]


def card_questions(card: TaskCard) -> list[str]:
    """Preset questions derived from the card, without a prediction engine."""
    text = " ".join([card.title, card.goal, card.facts, card.opening,
                     " ".join(point.description for point in card.checkpoints)])
    questions: list[str] = []
    if any(word in text for word in ("缺", "材料", "资料", "补交", "补齐")):
        questions.append("具体缺什么材料？")
    if any(word in text for word in ("提交", "补交", "上传", "入口")):
        questions.append("怎么补？")
    if any(word in text for word in ("审核", "退回", "进度", "状态")):
        questions.append("审核到哪一步了？")
    questions.append("已经提交了，显示审核中。")
    questions.append("只是保存，还没有提交。")
    questions.append("能加急吗？")
    questions.append("审核通过了吗？")
    questions.append("好的。")
    questions.append("是的。")
    return list(dict.fromkeys(questions))


def preset_dialogues(card: TaskCard) -> list[tuple[str, list[str]]]:
    """Several realistic conversations, not just three fixed probes."""
    card_turns = card_questions(card)
    dialogues: list[tuple[str, list[str]]] = []
    for index, chunk in enumerate(_chunks(card_turns, 4), 1):
        dialogues.append((f"card_{index}", chunk))
    dialogues.extend([
        ("official", list(OFFICIAL_TURNS)),
        ("official_status", list(OFFICIAL_STATUS_TURNS)),
        ("adversarial", list(ADVERSARIAL_TURNS)),
        ("adversarial_2", list(ADVERSARIAL_TURNS_2)),
        ("short", list(SHORT_TURNS)),
        ("english", list(ENGLISH_TURNS)),
    ])
    return dialogues


@dataclass
class PrewarmGroup:
    card: TaskCard
    path: Path
    round: int = 0


class _Sink:
    """A synthetic player. Only receipt metadata crosses the send lock."""

    def __init__(self):
        self.receipts = asyncio.Queue()
        self.frames = 0
        self.audio_bytes = 0

    async def __call__(self, payload: dict | bytes) -> None:
        self.frames += 1
        if isinstance(payload, bytes):
            epoch, unit, _ = struct.unpack_from("<III", payload)
            size = len(payload) - 12
            self.audio_bytes += size
            self.receipts.put_nowait({"type": "pcm", "epoch": epoch, "unit": unit, "samples": size // 2})
        elif payload["type"] in ("utterance_end", "reply_done", "error"):
            self.receipts.put_nowait(payload)

    async def wait_reply(self, session: Session) -> dict:
        played: dict[int, int] = {}
        async with asyncio.timeout(35):
            while True:
                frame = await self.receipts.get()
                if frame["type"] in ("reply_done", "error"):
                    if session.response is not None:
                        await asyncio.shield(session.response)
                    return frame
                unit = frame["unit"]
                done = frame["type"] == "utterance_end"
                if not done:
                    played[unit] = played.get(unit, 0) + frame["samples"]
                await session.playback_progress(frame["epoch"], unit, played.get(unit, 0), done=done)


async def _run_one(group: PrewarmGroup, cloud: Cloud,
                   opening_forecasts: OpeningForecastCache | None,
                   scenario: str, turns: list[str]) -> None:
    if not turns:
        return
    sink = _Sink()
    brain = Brain(cloud, opening_forecasts)
    session = Session(
        group.card, cloud, brain, sink,
        voice=True,
        predictions=True,
        log_dir=group.path.parent / "sessions",
    )
    _write(group.path, {"event": "scenario_start", "scenario": scenario,
                        "round": group.round, "turns": len(turns),
                        "task": group.card.title})
    started = time.perf_counter()
    try:
        await session.start()
        completed = 0
        for index in range(len(turns) + 1):
            if index:
                if session.closed or session.ended:
                    break
                text = turns[index - 1]
                await session.user_text(text, source="prewarm")
                _write(group.path, {"event": "turn", "scenario": scenario,
                                    "round": group.round, "turn": index, "input": text})
            stage = f"turn_{index}" if index else "opening"
            try:
                reply = await sink.wait_reply(session)
            except TimeoutError:
                _write(group.path, {"event": "scenario_timeout", "scenario": scenario,
                                    "round": group.round, "stage": stage})
                return
            if reply["type"] == "error":
                _write(group.path, {"event": "session_error", "scenario": scenario, "round": group.round,
                                    "stage": stage, "message": str(reply.get("message", ""))[:200]})
                return
            _write(group.path, {"event": "reply_done", "scenario": scenario, "round": group.round,
                                "epoch": reply["epoch"], "reused": reply.get("reused", False)})
            completed = index
        _write(group.path, {"event": "scenario_done", "scenario": scenario,
                            "round": group.round, "turns": completed,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                            "frames": sink.frames,
                            "audio_bytes": sink.audio_bytes})
    finally:
        await session.close()


async def _run_round(group: PrewarmGroup, cloud: Cloud,
                     opening_forecasts: OpeningForecastCache | None) -> None:
    group.round += 1
    dialogues = preset_dialogues(group.card)
    concurrency = min(_int_env(CONCURRENCY_ENV, DEFAULT_CONCURRENCY),
                      max(1, len(dialogues)))
    semaphore = asyncio.Semaphore(concurrency)
    _write(group.path, {"event": "round_start", "round": group.round,
                        "scenarios": len(dialogues), "concurrency": concurrency})

    async def one(name: str, turns: list[str]) -> None:
        async with semaphore:
            await _run_one(group, cloud, opening_forecasts, name, turns)

    cancelled = False
    try:
        results = await asyncio.gather(*(one(name, turns) for name, turns in dialogues),
                                       return_exceptions=True)
        for (name, _), result in zip(dialogues, results):
            if isinstance(result, Exception):
                _write(group.path, {"event": "scenario_error", "scenario": name,
                                    "round": group.round, "error": type(result).__name__})
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _write(group.path, {"event": "round_done", "round": group.round,
                            "cancelled": cancelled})


async def _loop(group: PrewarmGroup, cloud: Cloud,
                opening_forecasts: OpeningForecastCache | None) -> None:
    max_rounds = _int_env(MAX_ROUNDS_ENV, 0) if os.getenv(MAX_ROUNDS_ENV, "").strip() else 0
    delay = _float_env(LOOP_DELAY_ENV, DEFAULT_LOOP_DELAY)
    try:
        while True:
            await _run_round(group, cloud, opening_forecasts)
            if max_rounds and group.round >= max_rounds:
                break
            if delay:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _write(group.path, {"event": "prewarm_cancelled", "task": group.card.title,
                            "round": group.round})
        raise
    finally:
        _write(group.path, {"event": "prewarm_summary", "task": group.card.title,
                            "rounds": group.round})


class TaskPrewarmer:
    """Own background scenarios and their cooldowns for one service lifetime."""

    def __init__(self):
        self.active: dict[str, asyncio.Task] = {}
        self.pending: set[asyncio.Task] = set()
        self.blocked_until: dict[str, float] = {}

    def start(self, card: TaskCard, cloud: Cloud, forecasts: OpeningForecastCache | None,
              log_root: Path) -> asyncio.Task | None:
        key = task_key(card)
        if not enabled() or time.monotonic() < self.blocked_until.get(key, 0):
            return None
        existing = self.active.get(key)
        if existing is not None and not existing.done():
            return existing
        path = _log_path(log_root, key)
        group = PrewarmGroup(card=card, path=path)
        _write(path, {"event": "prewarm_start", "task": card.title, "task_key": key,
                      "ts": datetime.now(timezone.utc).isoformat()})
        task = asyncio.create_task(_loop(group, cloud, forecasts))
        self.active[key] = task
        self.pending.add(task)

        def finished(task):
            self.pending.discard(task)
            if self.active.get(key) is task:
                self.active.pop(key)
            if not task.cancelled() and (error := task.exception()) is not None:
                logging.getLogger(__name__).warning("Task prewarm failed", exc_info=error)

        task.add_done_callback(finished)
        return task

    def cancel(self, card: TaskCard) -> None:
        now = time.monotonic()
        self.blocked_until = {key: end for key, end in self.blocked_until.items() if end > now}
        key = task_key(card)
        self.blocked_until[key] = now + 5
        task = self.active.pop(key, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        pending = list(self.pending)
        for task in pending:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
