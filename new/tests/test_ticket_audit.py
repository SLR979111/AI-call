from __future__ import annotations

from pathlib import Path

from app.ticket_audit import audit_intents, read_turns, suggest_exclusions

ROOT = Path(__file__).resolve().parents[1]


def jd_card():
    from app.models import TaskCard
    return TaskCard.model_validate_json(
        (ROOT / "tasks" / "jd-qualification.json").read_text(encoding="utf-8"))


def test_audit_counts_ticket_intent_selections_over_multiple_turns():
    report = audit_intents(jd_card(), [
        "缺什么材料？",
        "已经提交了。",
        "授权书上的公司名写错了。",
        "今天天气不错。",
    ])
    assert report.turns == 4
    assert report.tickets == 11
    assert report.hits["missing_material"] == 1
    assert report.hits["submitted"] == 1
    assert report.hits["name_mismatch"] == 1
    assert sum(report.hits.values()) == 3
    assert report.per_turn[0]["intent"] == "missing_material"
    assert report.per_turn[-1]["intent"] is None
    assert len(report.selected_ids) == 3


def test_exclusions_need_zero_hits_in_both_fit_and_heldout():
    fit = audit_intents(jd_card(), ["缺什么材料？"])
    heldout = audit_intents(jd_card(), ["已经提交了。"])
    suggested = suggest_exclusions(fit, heldout)

    # Intents that matched in either set are never suggested for removal.
    assert "missing_material" not in suggested
    assert "submitted" not in suggested
    # An intent unselected in both sets is the only kind of candidate.
    assert "timing" in suggested
    for intent in suggested:
        assert fit.hits[intent] == 0
        assert heldout.hits[intent] == 0


def test_read_turns_skips_blank_lines_and_comments(tmp_path):
    path = tmp_path / "turns.txt"
    path.write_text("\ufeff缺什么材料？\n\n# 留出集\n已经提交了。\n  \n", encoding="utf-8")
    assert read_turns(path) == ["缺什么材料？", "已经提交了。"]
