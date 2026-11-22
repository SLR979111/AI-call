import json
from pathlib import Path

import pytest

from app.agent.pop_competition import normalize_answer
from app.agent.pop_confirmation import NEEDS_ACTION_HELP, explicit_understanding
from app.agent.pop_phone_style import load_phone_style
from app.eval.pop_closure import load_cases, replay, score
from app.rsi.pop_closure import load_policy, propose, release


@pytest.mark.parametrize(("utterance", "expected"), [
    ("我听明白了，稍后按要求去补交。", True),
    ("清楚了，回头我会按原申请的要求重新提交。", True),
    ("我知道怎么补材料了。", True),
    ("嗯，我在听。", False),
    ("我听懂了，但还能用另一份吗？", False),
    ("听懂了，先别替我记录。", False),
    ("下次听懂再说。", False),
])
def test_explicit_understanding_evidence(utterance, expected):
    assert explicit_understanding(normalize_answer(utterance)) is expected


def test_failure_groups_propose_only_allowlisted_changes():
    cases = [{"failure_labels": ["missed_confirmation", "unrelated", "optout_not_honored"]}]
    flags = [row["enabled"] for row in propose(cases)]
    assert ["grounded_confirmation", "explicit_stop"] not in flags
    assert ["explicit_stop", "grounded_confirmation"] in flags
    forged = {**release([]), "enabled": ["grounded_confirmation"]}
    with pytest.raises(ValueError):
        load_policy(forged)


@pytest.mark.parametrize(("utterance", "match"), [
    ("不知道", True),
    ("我不知道。", True),
    ("我还不知道接下来怎么改。", True),
    ("我没听明白。", True),
    ("不知道能不能过。", False),
    ("我不知道，先别记录。", False),
])
def test_short_negative_answer_preserves_specific_questions_and_stop_requests(utterance, match):
    assert bool(NEEDS_ACTION_HELP.fullmatch(normalize_answer(utterance))) is match


async def test_short_negative_explains_the_current_action_before_recording(tmp_path):
    root = Path(__file__).resolve().parents[1]
    style = load_phone_style(root / "eval/pop/phone_style.v1.json")
    case = {"id": "short-negative", "family": "certificate", "doc": "061", "turns": [
        ["我是这家店的负责人。", "pending_need"],
        ["现在方便。", "pending_result"],
        ["不知道", "pending_result"],
        ["我已经听明白整改办法了。", "pop_contact_recorded", "returned"],
    ]}

    async def unexpected_model(*args):
        pytest.fail(f"Unambiguous turn reached the classifier: {args[0]}")

    policy = load_policy(release(["explicit_identity", "grounded_confirmation"]))
    result = await replay(case, 1, policy, None, tmp_path, phone_style=style, turn_intent=unexpected_model)
    assert result["passed"]
    assert result["record_count"] == 1
    trace = json.loads(Path(result["trace"]).read_text(encoding="utf-8"))
    reply = trace["turns"][3]
    assert reply["sentences"] == [
        "请核对并改正填写的法人证件信息后重新提交。",
        "您打开京麦，进入这笔原申请，就能看到书面意见。",
        "您清楚接下来怎么改了吗？",
    ]


async def test_repeated_negative_moves_to_one_concrete_step(tmp_path):
    case = {"id": "repeated-negative", "family": "certificate", "doc": "053", "turns": [
        ["我是负责人。", "pending_need"],
        ["现在方便。", "pending_result"],
        ["我不知道。", "pending_result"],
        ["还是不知道。", "pending_result"],
        ["不用继续了，先别记录。", "safe_exit"],
    ]}
    policy = load_policy(release(["explicit_identity", "grounded_confirmation", "explicit_stop"]))
    result = await replay(case, 1, policy, None, tmp_path)
    assert result["passed"]
    assert result["record_count"] == 0
    trace = json.loads(Path(result["trace"]).read_text(encoding="utf-8"))
    assert trace["turns"][4]["sentences"] == ["我们先找这笔申请。您现在能打开京麦吗？"]


async def test_closure_scorer_requires_persisted_receipt_and_correct_fields(tmp_path):
    cases = load_cases(Path(__file__).resolve().parents[1] / "eval/pop/closure_cases.v1.json")
    case = next(row for row in cases["dev"] if row["id"] == "dev-company-commit")
    verdict = await replay(case, 3, load_policy(release(["grounded_confirmation"])), None, tmp_path)
    assert verdict["passed"]
    trace = json.loads(Path(verdict["trace"]).read_text(encoding="utf-8"))
    events = [json.loads(line) for line in Path(verdict["events"]).read_text(encoding="utf-8").splitlines()]
    missing = score(case, trace["context"], trace["turns"], events, [])
    assert {"database_matches_receipts", "persisted_receipt"} <= set(missing["failure_labels"])
    wrong = {**trace["context"], "request_id": "Q999000"}
    mismatch = score(case, wrong, trace["turns"], events, trace["records"])
    assert mismatch["severe_cases"] == 1
    assert "write_fields" in mismatch["failure_labels"]
