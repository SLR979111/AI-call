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
    assert {"official", "official_status", "adversarial", "adversarial_2",
            "short", "english"} <= set(dialogues)
    assert dialogues["official"] == list(OFFICIAL_TURNS)
    assert dialogues["adversarial"] == list(ADVERSARIAL_TURNS)
    assert any(name.startswith("card_") for name in dialogues)
    assert any(turns for name, turns in dialogues.items() if name.startswith("card_"))
    assert len(dialogues) >= 8
