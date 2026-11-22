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


CASES = [
    ("jd", [
        candidate("auth", "用户问授权材料、经销商授权、品牌方直接授权", ["还要补经销商授权吗"], "缺的是青禾给远杉的授权。"),
        candidate("name", "用户问授权书公司名、名称不一致", ["授权书公司名写错了"], "授权书公司名要和店铺主体一致。"),
        candidate("mark", "用户问商标状态截图", ["商标状态截图要包含什么"], "截图要看清商标名称、注册号和当前状态。"),
        candidate("submit", "用户说已提交、只是保存、审核中", ["已经提交了"], "只保存不算提交，需要点提交。"),
    ], [
        ("我先不补了", None),
        ("授权书重新上传了", "name"),
        ("公司名称不一致", "name"),
        ("能加急吗", None),
        ("品牌方直接授权可以吗", "auth"),
        ("截图发你了", "mark"),
    ]),
    ("broadband", [
        candidate("offline", "用户说网络不能用、断网、上不了网", ["上不了网"], "先看光猫电源和指示灯。"),
        candidate("slow", "用户说网速慢、卡顿", ["网速很慢"], "先确认是一台还是多台设备慢。"),
        candidate("router", "用户问路由器、重启、后台", ["路由器怎么重启"], "路由器可以按电源重启。"),
    ], [
        ("光猫灯是红的", "offline"),
        ("网速还是慢", "slow"),
        ("路由器后台密码忘了", "router"),
    ]),
    ("official", [
        candidate("progress", "用户问工单进度、审核中", ["工单到哪一步了"], "工单当前在审核中。"),
        candidate("next", "用户问补充材料、下一步", ["还要补材料吗"], "按页面提示补充材料或等待。"),
        candidate("limit", "用户问时间、加急", ["多久能出结果"], "具体时间以实际审核为准。"),
    ], [
        ("审核通过了吗", None),
        ("催一下", None),
        ("材料交给谁", "next"),
    ]),
    ("adversarial", [
        candidate("auth", "用户问授权材料", ["还要补经销商授权吗"], "缺的是授权。"),
        candidate("submit", "用户说已提交", ["已经提交了"], "只保存不算提交。"),
    ], [
        ("不补", None),
        ("你保证", None),
        ("我不方便", None),
        ("我不想听", None),
    ]),
]


def test_round2_heldout():
    engine = ContextAwareDecisionEngine()
    failures = []
    for name, candidates, cases in CASES:
        task_card = card(
            "jd-qualification.json" if name in ("jd", "adversarial")
            else "home-broadband-support.json" if name == "broadband"
            else "official-work-order.json"
        )
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "", task_card)
            got = selected.id if decision == "reuse" and selected else None
            if got != expected:
                failures.append((name, text, expected, got))
    assert not failures, failures
