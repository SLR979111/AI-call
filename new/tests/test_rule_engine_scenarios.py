from __future__ import annotations

from pathlib import Path

from app.context_aware_decision import ContextAwareDecisionEngine
from app.models import Candidate, TaskCard

ROOT = Path(__file__).resolve().parents[1]


def candidate(cid: str, when: str, examples: list[str], text: str) -> Candidate:
    return Candidate.model_validate({
        "id": cid,
        "when": when,
        "examples": examples,
        "reply": {"text": text, "covers": [], "question": ""},
    })


def card(name: str) -> TaskCard:
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
            ("您好，这里是资料核对说明。", None),
            ("具体缺什么材料？", "auth"),
            ("嗯，方便的。", None),
            ("是的。", None),
            ("好的。", None),
            ("只是保存，还没有提交。", "submit"),
            ("已经提交了，显示审核中。", "submit"),
            ("品牌方直接给我们公司授权了。", "auth"),
            ("授权书上的公司名写错了。", "name"),
            ("商标状态截图要包含什么？", "mark"),
            ("我还要补经销商授权吗？", "auth"),
            ("不是，我不负责，而且还有一个别的问题。", None),
            ("ok", None),
            ("稍等，我忙，晚点再说。", None),
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
            ("具体缺什么材料？", None),
            ("上不了网怎么办？", "offline"),
            ("网速很慢怎么办？", "slow"),
            ("路由器怎么重启？", "router"),
            ("好的，谢谢。", None),
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
            ("300M 多少钱？", "price"),
            ("推荐哪个套餐？", "recommend"),
            ("什么时候能装？", "install"),
            ("好的。", None),
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
            ("工单到哪一步了？", "progress"),
            ("还要补材料吗？", "next"),
            ("多久能出结果？", "limit"),
            ("好的，知道了。", None),
        ],
    ),
    "english": (
        card("jd-qualification.json"),
        [
            candidate("confirm", "用户确认继续、愿意听", ["ok", "yes"], "好，那我继续说。"),
        ],
        [
            ("ok", "confirm"),
            ("yes", "confirm"),
            ("no", None),
            ("not now", None),
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
            ("不是，我不负责，而且还有一个别的问题。", None),
            ("你保证能过吗？", None),
            ("我要投诉，别说了。", None),
            ("换个人，我不想跟你说。", None),
            ("我没有授权，也不想补。", None),
            ("yes", None),
        ],
    ),
}


def test_rule_engine_scenarios():
    engine = ContextAwareDecisionEngine()
    results = []
    for name, (task_card, candidates, cases) in SCENARIOS.items():
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "", task_card)
            got = selected.id if decision == "reuse" and selected else None
            results.append((name, text, expected, got))
    failures = [(name, text, expected, got) for name, text, expected, got in results if expected != got]
    assert not failures, failures
