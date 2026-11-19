"""Background full-dialogue warmup for the next real call.

After a task is loaded, synthetic Sessions run the normal Brain/Cloud path
against several preset scenarios.  All transport frames and PCM go to a sink;
only model/TTS caches and a separate JSONL audit log are kept.

Scenarios run in parallel and repeat until a real call starts.  The real start
cancels the whole group immediately; cache entries already written remain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .brain import Brain
from .decision_model import DecisionModel
from .forecast import OpeningForecastCache
from .models import TaskCard
from .providers import Cloud
from .session import Session
from .ticket_forecast import generate_ticket_candidates

ENABLED_ENV = "TASK_PREWARM_ON_LOAD"
CONCURRENCY_ENV = "TASK_PREWARM_CONCURRENCY"
LOOP_DELAY_ENV = "TASK_PREWARM_LOOP_DELAY"
MAX_ROUNDS_ENV = "TASK_PREWARM_MAX_ROUNDS"
DEFAULT_CONCURRENCY = 4
DEFAULT_LOOP_DELAY = 1.0

PREWARM_GROUPS: dict[str, "PrewarmGroup"] = {}
FOREGROUND_BLOCK: dict[str, float] = {}

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
    return value if value >= 0 else default


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


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size) if items[i:i + size]]


def preset_dialogues(card: TaskCard) -> list[tuple[str, list[str]]]:
    """Several realistic conversations, not just three fixed probes."""
    ticket_turns: list[str] = []
    for candidate in generate_ticket_candidates(card):
        ticket_turns.extend(candidate.examples)
    if ticket_turns:
        ticket_turns.extend(SHORT_TURNS)
    card_turns = list(dict.fromkeys(ticket_turns))
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
    key: str
    card: TaskCard
    path: Path
    tasks: set[asyncio.Task] = field(default_factory=set)
    sessions: set[Session] = field(default_factory=set)
    cancelled: bool = False
    round: int = 0


class _Sink:
    """Transport sink: frames are counted and discarded, never sent."""

    def __init__(self, log_path: Path, scenario: str, round_number: int):
        self.log_path = log_path
        self.scenario = scenario
        self.round = round_number
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
                                   "round": self.round, "epoch": epoch,
                                   "reused": payload.get("reused", False)})
        elif payload.get("type") == "error":
            _write(self.log_path, {"event": "session_error", "scenario": self.scenario,
                                   "round": self.round,
                                   "message": str(payload.get("message", ""))[:200]})


async def _wait_reply(sink: _Sink, after: int, timeout: float = 35.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(epoch > after for epoch in sink.replies):
            return True
        await asyncio.sleep(0.05)
    return False


async def _run_one(group: PrewarmGroup, cloud: Cloud,
                   opening_forecasts: OpeningForecastCache | None,
                   scenario: str, turns: list[str]) -> None:
    if not turns or group.cancelled:
        return
    sink = _Sink(group.path, scenario, group.round)
    brain = Brain(
        cloud, opening_forecasts,
        ticket_forecasts=True,
        decision_model=DecisionModel(cloud),
    )
    session = Session(
        group.card, cloud, brain, sink,
        voice=True,
        predictions=True,
        log_dir=group.path.parent / "sessions",
        on_hangup=lambda: None,
    )
    group.sessions.add(session)
    _write(group.path, {"event": "scenario_start", "scenario": scenario,
                        "round": group.round, "turns": len(turns),
                        "task": group.card.title})
    started = time.perf_counter()
    try:
        await session.start()
        if not await _wait_reply(sink, 0):
            _write(group.path, {"event": "scenario_timeout", "scenario": scenario,
                                "round": group.round, "stage": "opening"})
            return
        last = max(sink.replies)
        for index, text in enumerate(turns, 1):
            if group.cancelled or session.closed or session.ended:
                break
            before = last
            await session.user_text(text, source="prewarm")
            _write(group.path, {"event": "turn", "scenario": scenario,
                                "round": group.round, "turn": index,
                                "input": text})
            if not await _wait_reply(sink, before):
                _write(group.path, {"event": "scenario_timeout", "scenario": scenario,
                                    "round": group.round,
                                    "stage": f"turn_{index}"})
                break
            last = max(sink.replies)
        _write(group.path, {"event": "scenario_done", "scenario": scenario,
                            "round": group.round, "turns": len(turns),
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                            "frames": sink.frames,
                            "audio_bytes": sink.audio_bytes})
    finally:
        try:
            await session.close()
        except Exception:
            pass
        group.sessions.discard(session)


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
            if not group.cancelled:
                await _run_one(group, cloud, opening_forecasts, name, turns)

    tasks = [asyncio.create_task(one(name, turns)) for name, turns in dialogues]
    group.tasks.update(tasks)
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        group.tasks.difference_update(tasks)
        _write(group.path, {"event": "round_done", "round": group.round,
                            "cancelled": group.cancelled})


async def _loop(group: PrewarmGroup, cloud: Cloud,
                opening_forecasts: OpeningForecastCache | None) -> None:
    max_rounds = _int_env(MAX_ROUNDS_ENV, 0) if os.getenv(MAX_ROUNDS_ENV, "").strip() else 0
    delay = _float_env(LOOP_DELAY_ENV, DEFAULT_LOOP_DELAY)
    try:
        while not group.cancelled:
            await _run_round(group, cloud, opening_forecasts)
            if max_rounds and group.round >= max_rounds:
                break
            if not group.cancelled and delay:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _write(group.path, {"event": "prewarm_cancelled", "task": group.card.title,
                            "round": group.round})
        raise
    finally:
        _write(group.path, {"event": "prewarm_summary", "task": group.card.title,
                            "rounds": group.round})


def start_task_prewarm(card: TaskCard, cloud: Cloud,
                       opening_forecasts: OpeningForecastCache | None,
                       log_root: Path) -> asyncio.Task | None:
    if not enabled():
        return None
    key = task_key(card)
    if time.monotonic() < FOREGROUND_BLOCK.get(key, 0.0):
        return None
    existing = PREWARM_GROUPS.get(key)
    if existing is not None and any(not task.done() for task in existing.tasks):
        return next(task for task in existing.tasks if not task.done())

    path = _log_path(log_root, key)
    group = PrewarmGroup(key=key, card=card, path=path)
    _write(path, {"event": "prewarm_start", "task": card.title, "task_key": key,
                  "ts": datetime.now(timezone.utc).isoformat()})
    task = asyncio.create_task(_loop(group, cloud, opening_forecasts))
    group.tasks.add(task)
    PREWARM_GROUPS[key] = group

    def done(_task):
        PREWARM_GROUPS.pop(key, None)

    task.add_done_callback(done)
    return task


def cancel_task_prewarm(card_or_key: TaskCard | str, reason: str = "foreground") -> bool:
    key = card_or_key if isinstance(card_or_key, str) else task_key(card_or_key)
    # The browser may have already issued /api/warm before the real start
    # event reaches the server. Block any late prewarm for a short window.
    FOREGROUND_BLOCK[key] = time.monotonic() + 5.0
    group = PREWARM_GROUPS.pop(key, None)
    if group is None:
        return False
    group.cancelled = True
    for task in list(group.tasks):
        task.cancel()
    for session in list(group.sessions):
        if not session.closed:
            asyncio.create_task(session.close())
    return True
