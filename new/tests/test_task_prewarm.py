from __future__ import annotations

from pathlib import Path

from app.models import TaskCard
from app.task_prewarm import ADVERSARIAL_TURNS, OFFICIAL_TURNS, preset_dialogues

ROOT = Path(__file__).resolve().parents[1]


def test_preset_dialogues_cover_card_official_and_adversarial():
    card = TaskCard.model_validate_json(
        (ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8")
    )
    dialogues = dict(preset_dialogues(card))
    assert set(dialogues) == {"card", "official", "adversarial"}
    assert dialogues["official"] == list(OFFICIAL_TURNS)
    assert dialogues["adversarial"] == list(ADVERSARIAL_TURNS)
    assert dialogues["card"]
    assert "已经提交了，显示审核中。" in dialogues["card"]
