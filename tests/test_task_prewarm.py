from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import task_prewarm
from app.models import DEFAULT_CARD, TaskCard
from test_core import Cloud, TestBrain
from app.task_prewarm import ADVERSARIAL_TURNS, OFFICIAL_TURNS, preset_dialogues

ROOT = Path(__file__).resolve().parents[1]


def test_preset_dialogues_cover_card_official_and_adversarial():
    card = TaskCard.model_validate_json(
        (ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8")
    )
    dialogues = dict(preset_dialogues(card))
    assert {"official", "official_status", "adversarial", "adversarial_2",
            "short", "english"} <= set(dialogues)
    assert dialogues["official"] == list(OFFICIAL_TURNS)
    assert dialogues["adversarial"] == list(ADVERSARIAL_TURNS)
    assert any(name.startswith("card_") for name in dialogues)
    assert any(turns for name, turns in dialogues.items() if name.startswith("card_"))
    assert len(dialogues) >= 8

async def test_synthetic_player_finishes_long_opening_and_followup(monkeypatch, tmp_path):
    class LongVoice(Cloud):
        async def speech(self, text):
            for _ in range(20):
                yield bytes(1920)

    monkeypatch.setattr(task_prewarm, "Brain", lambda *_: TestBrain())
    log = tmp_path / "warm.jsonl"
    group = task_prewarm.PrewarmGroup(DEFAULT_CARD, log)
    await asyncio.wait_for(task_prewarm._run_one(
        group, LongVoice(), None, "case", ["具体缺什么？"]), timeout=2)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["epoch"] for e in events if e["event"] == "reply_done"] == [1, 2]
    assert events[-1]["event"] == "scenario_done"
    assert events[-1]["turns"] == 1
    assert events[-1]["audio_bytes"] >= 2 * 38400

async def test_shutdown_drains_cancelled_prewarm_and_preserves_replacement(monkeypatch, tmp_path):
    owner = task_prewarm.TaskPrewarmer()
    entered = asyncio.Queue()
    releasing = asyncio.Event()

    async def loop(group, cloud, forecasts):
        await entered.put(True)
        try:
            await asyncio.Future()
        finally:
            await releasing.wait()

    monkeypatch.setattr(task_prewarm, "_loop", loop)
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    def start():
        return owner.start(DEFAULT_CARD, Cloud(), None, tmp_path)

    original = start()
    assert start() is original
    await entered.get()
    owner.cancel(DEFAULT_CARD)
    assert start() is None
    owner.blocked_until.clear()
    replacement = start()
    await entered.get()
    releasing.set()
    await asyncio.gather(original, return_exceptions=True)
    assert start() is replacement
    await owner.close()
    assert replacement.cancelled()
    assert not owner.pending and not owner.active
