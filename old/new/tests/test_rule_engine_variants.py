from __future__ import annotations

from pathlib import Path

from app.context_aware_decision import ContextAwareDecisionEngine
from app.models import Candidate, TaskCard

ROOT = Path(__file__).resolve().parents[1]


def candidate(cid, when, examples, text):
    return Candidate.model_validate({
        "id": cid, "when": when, "examples": examples,
        "reply": {"text": text, "covers": [], "question": ""},
    })


def make():
    card = TaskCard.model_validate_json((ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8"))
    candidates = [
        candidate("auth", "用户问授权材料、经销商授权、品牌方直接授权",
                  ["还要补经销商授权吗", "品牌方直接授权行不行"], "缺的是青禾给远杉的授权，您补这一段。"),
        candidate("name", "用户问授权书公司名、名称不一致",
                  ["授权书公司名写错了"], "授权书公司名要和店铺主体一致，需要改过来重新上传。"),
        candidate("mark", "用户问商标状态截图、截图包含什么",
                  ["商标状态截图要包含什么"], "截图要看清商标名称、注册号和当前状态，注册证不能直接代替。"),
        candidate("submit", "用户说已提交、只是保存、审核中",
                  ["已经提交了，显示审核中", "只是保存，还没有提交"], "只保存不算提交，需要点提交。"),
    ]
    return card, candidates


# Same work order, different surface forms. This is the offline fitting loop:
# add a variant batch, see which lexical evidence fails, add a generic synonym
# or a ticket phrase, then keep a held-out batch unseen.
ROUNDS = [
    [
        ("只是保存了还没提交", "submit"),
        ("保存了，没点提交", "submit"),
        ("已经重新提交了", "submit"),
        ("审核中了", "submit"),
        ("授权书公司名不一致", "name"),
        ("名称写错了", "name"),
        ("商标截图看不清", "mark"),
        ("截图要拍什么", "mark"),
    ],
    [
        ("还要经销商授权吗", "auth"),
        ("品牌方直接授权行吗", "auth"),
        ("直接授权可以吗", "auth"),
        ("授权到底缺哪一段", "auth"),
        ("还要补材料吗", "auth"),
        ("具体缺什么", "auth"),
        ("已经补交了吗", "submit"),
        ("审核要多久", "submit"),
        ("能看清吗", "mark"),
    ],
]


def test_repeated_work_order_variants():
    engine = ContextAwareDecisionEngine()
    card, candidates = make()
    failures = []
    for round_number, cases in enumerate(ROUNDS, 1):
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "这次资料被退回了，您想先了解哪一部分？", card)
            got = selected.id if decision == "reuse" and selected else None
            if got != expected:
                failures.append((round_number, text, expected, got))
    assert not failures, failures
