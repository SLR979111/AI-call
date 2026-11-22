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


JD_CANDIDATES = [
    candidate("auth", "用户问授权材料、经销商授权、品牌方直接授权",
              ["还要补经销商授权吗", "品牌方直接授权行不行"], "缺的是青禾给远杉的授权，您补这一段。"),
    candidate("name", "用户问授权书公司名、名称不一致",
              ["授权书公司名写错了"], "授权书公司名要和店铺主体一致，需要改过来重新上传。"),
    candidate("mark", "用户问商标状态截图、截图包含什么",
              ["商标状态截图要包含什么"], "截图要看清商标名称、注册号和当前状态，注册证不能直接代替。"),
    candidate("submit", "用户说已提交、只是保存、审核中",
              ["已经提交了，显示审核中", "只是保存，还没有提交"], "只保存不算提交，需要点提交。"),
]

BROADBAND_CANDIDATES = [
    candidate("offline", "用户说网络不能用、断网、上不了网",
              ["上不了网", "网断了"], "先看一下光猫的电源和指示灯。"),
    candidate("slow", "用户说网速慢、卡顿",
              ["网速很慢", "一直卡"], "先确认是一台设备还是多台设备都慢。"),
    candidate("router", "用户问路由器、重启、后台",
              ["路由器怎么重启", "后台怎么进"], "路由器可以按电源重启，别急着恢复出厂。"),
]

NETWORK_CANDIDATES = [
    candidate("price", "用户问套餐价格、多少钱",
              ["300M 多少钱", "600M 怎么收费"], "300M 是 99 元每月，600M 是 129 元每月。"),
    candidate("recommend", "用户问推荐、选哪个套餐",
              ["推荐哪个", "哪个适合我"], "看设备数量和预算，300M 适合一般家庭。"),
    candidate("install", "用户问安装、预约、覆盖",
              ["什么时候能装", "能不能预约"], "安装通常要 2-3 个工作日，具体以预约结果为准。"),
]

OFFICIAL_CANDIDATES = [
    candidate("progress", "用户问工单进度、审核中、到哪一步",
              ["工单到哪一步了", "还在审核吗"], "工单当前在审核中。"),
    candidate("next", "用户问补充材料、下一步",
              ["还要补材料吗", "下一步做什么"], "按页面提示补充材料或等待。"),
    candidate("limit", "用户问多久出结果、能不能加急",
              ["多久能出结果", "能加急吗"], "具体时间以实际审核为准，不能保证加急。"),
]

CONFIRM_CANDIDATES = [
    candidate("confirm", "用户确认继续、愿意听", ["ok", "yes"], "好，那我继续说。"),
]

# Held-out round 3. These sentences were written after the algorithm changes in
# this branch and are grouped by the failure mode they cover. They are never
# used to add one-off keywords to the rule table.
SCENARIOS = {
    "jd": (card("jd-qualification.json"), JD_CANDIDATES, [
        # New questions the prepared replies do not answer.
        ("提交入口在哪里", None),
        ("材料要传到哪个页面", None),
        ("为什么被退回来了", None),
        ("能开专票吗", None),
        ("你们几点上班", None),
        # Result chasing, guarantees, complaints, staff changes and refusals.
        ("帮我催一下审核", None),
        ("能不能今天就出结果", None),
        ("你能保证这次一定过吗", None),
        ("我要去投诉你们", None),
        ("换一个懂的人来", None),
        ("让你们主管回电话", None),
        ("这个我不弄了", None),
        ("先放着吧，不着急", None),
        ("别再问了", None),
        # Corrections of earlier wording.
        ("刚才口误，是保存不是提交", None),
        ("我改口，先不办了", None),
        ("前面那句话我收回", None),
        # Chinese ellipsis and synonyms that should still select one branch.
        ("公司全称对不上", "name"),
        ("商标照片拍糊了", "mark"),
        ("我点过提交了", "submit"),
        ("授权是品牌方直接给的", "auth"),
        ("还差哪个材料", "auth"),
        ("名称写错了", "name"),
        ("截图要拍哪些内容", "mark"),
        ("已经补交上去了", "submit"),
    ]),
    "english": (card("jd-qualification.json"), CONFIRM_CANDIDATES, [
        ("okay", "confirm"),
        ("got it", "confirm"),
        ("understood", "confirm"),
        ("sounds good", "confirm"),
        ("no thanks", None),
        ("not now", None),
        ("not right now", None),
        ("busy right now", None),
        ("nope", None),
    ]),
    "broadband": (card("home-broadband-support.json"), BROADBAND_CANDIDATES, [
        ("突然没网了", "offline"),
        ("网络连不上了", "offline"),
        ("刷视频一直缓冲", "slow"),
        ("网速慢得很", "slow"),
        ("路由器怎么进后台", "router"),
        ("光猫重启以后还是连不上", "offline"),
        # The offline reply explicitly asks to check the indicator light, so a
        # red-light observation is topically covered; asking for a repair or
        # dispatch is not.
        ("光猫的灯是红的", "offline"),
        ("能不能派师傅来看看", None),
        ("你们维修怎么收费", None),
        ("不用了", None),
        ("先这样吧", None),
    ]),
    "network": (card("network-consultation.json"), NETWORK_CANDIDATES, [
        ("600M 套餐怎么卖", "price"),
        ("选哪个更值", "recommend"),
        ("装宽带要几天", "install"),
        ("你们能上门安装吗", "install"),
        ("300M和600M差多少钱", "price"),
        ("推荐一个便宜点的套餐", "recommend"),
        ("300M 一年多少钱", "price"),
        # Out-of-scope questions, not a prepared reply.
        ("要不要签合约", None),
        ("有没有便宜点的", None),
    ]),
    "official": (card("official-work-order.json"), OFFICIAL_CANDIDATES, [
        ("工单有进展吗", "progress"),
        ("审核到哪个环节了", "progress"),
        ("还需要补充什么", "next"),
        ("材料要交到哪里", "next"),
        ("要等几个工作日", "limit"),
        ("能不能加急处理", "limit"),
        ("多久能批下来", "limit"),
        ("还要等几天", "limit"),
        ("审核什么时候结束", "limit"),
        ("进度到哪了", "progress"),
        # Result chasing, guarantee, escalation, refusal and change of mind.
        ("帮我催催", None),
        ("你们能保证今天通过吗", None),
        ("我要找你们领导", None),
        ("算了吧不弄了", None),
        ("我改主意了", None),
        ("这笔钱什么时候退", None),
    ]),
    "adversarial": (card("jd-qualification.json"), JD_CANDIDATES, [
        ("你先保证，不然免谈", None),
        ("我要投诉到12315", None),
        ("换个人跟我说", None),
        ("别再打来了", None),
        ("我不需要补", None),
        ("随便问问而已", None),
        ("我说错了，刚才不算", None),
        ("算了不麻烦你了", None),
        ("我不想听了", None),
        ("你们这是骗人", None),
    ]),
}


def test_round3_heldout():
    engine = ContextAwareDecisionEngine()
    failures = []
    for name, (task_card, candidates, cases) in SCENARIOS.items():
        for text, expected in cases:
            decision, selected = engine.should_reuse(text, candidates, "", task_card)
            got = selected.id if decision == "reuse" and selected else None
            if got != expected:
                failures.append((name, text, expected, got))
    assert not failures, failures
