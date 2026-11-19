from __future__ import annotations

import asyncio
from pathlib import Path

from app.brain import Brain
from app.models import Candidate, Say, TaskCard
from app.task_warmup import warm_task

ROOT = Path(__file__).resolve().parents[1]


class FakeCloud:
    async def prefetch(self, text: str) -> bytes:
        return b"pcm" * 32


def test_warm_task_writes_forecast_and_audio(monkeypatch, tmp_path):
    card = TaskCard.model_validate_json(
        (ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8")
    )
    candidate = Candidate(
        id="t_missing_material",
        when="用户问缺什么材料",
        examples=["具体缺什么材料？"],
        reply=Say(text="缺的是授权材料。"),
        checkpoint="authorization",
        intent="missing_material",
        polarity="question",
    )

    async def fake_predict(self, snapshot):
        assert snapshot["opening"] is True
        return [candidate]

    monkeypatch.setattr(Brain, "predict", fake_predict)
    result = asyncio.run(warm_task(card, FakeCloud(), None, tmp_path))
    assert result["candidates"] == 1
    assert result["audio_prepared"] == 1
    assert result["audio_bytes"] > 0
    logs = list((tmp_path / "task-warmup").glob("*.jsonl"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert '"event": "warm_summary"' in text
    assert '"candidate": "t_missing_material"' in text
