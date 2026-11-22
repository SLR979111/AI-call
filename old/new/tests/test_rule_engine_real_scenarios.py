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


def card(name):
    return TaskCard.model_validate_json((ROOT / "tasks" / name).read_text(encoding="utf-8"))


def test_logistics_and_invoice_scenarios():
    engine = ContextAwareDecisionEngine()
    scenarios = [
        (
            card("logistics-delivery.json"),
            [
                candidate("status", "用户问快递到哪了、物流状态", ["快递到哪了", "物流怎么没更新"], "当前能看到最近的运单记录。"),
                candidate("contact", "用户问快递员、客服、联系电话", ["怎么联系快递员", "客服电话多少"], "可以联系承运快递员或官方客服。"),
                candidate("address", "用户问改地址、改派", ["能改地址吗", "地址写错了"], "改地址需要联系承运方，不能保证成功。"),
            ],
            [
                ("快递到哪了？", "status"),
                ("物流一直没更新", "status"),
                ("怎么联系快递员", "contact"),
                ("能改地址吗", "address"),
                ("今天能送到吗", None),
            ],
        ),
        (
            card("after-sales-invoice.json"),
            [
                candidate("type", "用户问普通发票、专用发票、类型", ["开专票还是普票", "发票类型怎么选"], "普通发票和专用发票需要的资料不同。"),
                candidate("header", "用户问发票抬头、公司名称", ["抬头写错了", "抬头怎么填"], "抬头要跟主体信息一致。"),
                candidate("status", "用户问开票状态、重开", ["发票开好了吗", "怎么重开"], "具体开票时间以页面提示为准。"),
            ],
            [
                ("开专票还是普票", "type"),
                ("抬头写错了怎么办", "header"),
                ("发票开好了吗", "status"),
                ("怎么重开", "status"),
                ("能加急开吗", None),
            ],
        ),
    ]
    failures = []
    for task_card, candidates, cases in scenarios:
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "", task_card)
            got = selected.id if decision == "reuse" and selected else None
            if got != expected:
                failures.append((task_card.title, text, expected, got))
    assert not failures, failures
