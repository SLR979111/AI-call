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


SCENARIOS = {
    "jd": (
        card("jd-qualification.json"),
        [
            candidate("auth", "用户问授权材料、经销商授权、品牌方直接授权",
                      ["还要补经销商授权吗", "品牌方直接授权行不行"], "缺的是青禾给远杉的授权，您补这一段。"),
            candidate("name", "用户问授权书公司名、名称不一致",
                      ["授权书公司名写错了"], "授权书公司名要和店铺主体一致，需要改过来重新上传。"),
            candidate("mark", "用户问商标状态截图、截图包含什么",
                      ["商标状态截图要包含什么"], "截图要看清商标名称、注册号和当前状态，注册证不能直接代替。"),
            candidate("submit", "用户说已提交、只是保存、审核中",
                      ["已经提交了，显示审核中", "只是保存，还没有提交"], "只保存不算提交，需要点提交。"),
        ],
        [
            ("还没点提交呢", "submit"),
            ("已经审核中了", "submit"),
            ("公司抬头不一致", "name"),
            ("商标注册证可以吗", "mark"),
            ("经销商授权还缺吗", "auth"),
            ("品牌方自己授权可以吗", "auth"),
            ("截图能代替注册证吗", "mark"),
            ("直接授权行吗", "auth"),
        ],
    ),
    "broadband": (
        card("home-broadband-support.json"),
        [
            candidate("offline", "用户说网络不能用、断网、上不了网",
                      ["上不了网", "网断了"], "先看一下光猫的电源和指示灯。"),
            candidate("slow", "用户说网速慢、卡顿",
                      ["网速很慢", "一直卡"], "先确认是一台设备还是多台设备都慢。"),
            candidate("router", "用户问路由器、重启、后台",
                      ["路由器怎么重启", "后台怎么进"], "路由器可以按电源重启，别急着恢复出厂。"),
        ],
        [
            ("连不上网了", "offline"),
            ("网速有点卡", "slow"),
            ("光猫重启可以吗", "router"),
            ("路由器后台怎么进", "router"),
        ],
    ),
    "network": (
        card("network-consultation.json"),
        [
            candidate("price", "用户问套餐价格、多少钱",
                      ["300M 多少钱", "600M 怎么收费"], "300M 是 99 元每月，600M 是 129 元每月。"),
            candidate("recommend", "用户问推荐、选哪个套餐",
                      ["推荐哪个", "哪个适合我"], "看设备数量和预算，300M 适合一般家庭。"),
            candidate("install", "用户问安装、预约、覆盖",
                      ["什么时候能装", "能不能预约"], "安装通常要 2-3 个工作日，具体以预约结果为准。"),
        ],
        [
            ("600M 多少钱", "price"),
            ("家里三台设备选哪个", "recommend"),
            ("能预约安装吗", "install"),
        ],
    ),
    "official": (
        card("official-work-order.json"),
        [
            candidate("progress", "用户问工单进度、审核中、到哪一步",
                      ["工单到哪一步了", "还在审核吗"], "工单当前在审核中。"),
            candidate("next", "用户问补充材料、下一步",
                      ["还要补材料吗", "下一步做什么"], "按页面提示补充材料或等待。"),
            candidate("limit", "用户问多久出结果、能不能加急",
                      ["多久能出结果", "能加急吗"], "具体时间以实际审核为准，不能保证加急。"),
        ],
        [
            ("审核到哪了", "progress"),
            ("还要补材料", "next"),
            ("多久出结果", "limit"),
        ],
    ),
    "adversarial": (
        card("jd-qualification.json"),
        [
            candidate("auth", "用户问授权材料、经销商授权",
                      ["还要补经销商授权吗"], "缺的是青禾给远杉的授权。"),
            candidate("submit", "用户说已提交、审核中",
                      ["已经提交了，显示审核中"], "只保存不算提交，需要点提交。"),
        ],
        [
            ("我不想补", None),
            ("你保证能过", None),
            ("先别问了", None),
            ("换个人", None),
        ],
    ),
}


def test_heldout_rule_scenarios():
    engine = ContextAwareDecisionEngine()
    failures = []
    for name, (task_card, candidates, cases) in SCENARIOS.items():
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "", task_card)
            got = selected.id if decision == "reuse" and selected else None
            if got != expected:
                failures.append((name, text, expected, got))
    assert not failures, failures
