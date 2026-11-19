from __future__ import annotations

import json
from pathlib import Path

from app.models import TaskCard
from app.ticket_forecast import generate_ticket_candidates

ROOT = Path(__file__).resolve().parents[1]


def card(name: str) -> TaskCard:
    return TaskCard.model_validate_json((ROOT / "tasks" / name).read_text(encoding="utf-8"))


def test_exclude_by_task_only_affects_named_card(monkeypatch):
    jd = card("jd-qualification.json")
    official = card("official-work-order.json")
    mapping = {jd.title: ["how_to_submit"]}
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE_BY_TASK", json.dumps(mapping, ensure_ascii=False))
    jd_items = generate_ticket_candidates(jd)
    official_items = generate_ticket_candidates(official)
    assert "how_to_submit" not in {item.intent for item in jd_items}
    assert "how_to_submit" in {item.intent for item in official_items}
    assert len(jd_items) == 9


def test_invalid_exclude_json_is_ignored(monkeypatch):
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE_BY_TASK", "{not-json")
    items = generate_ticket_candidates(card("jd-qualification.json"))
    assert len(items) == 10
