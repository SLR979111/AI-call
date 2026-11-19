from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.context_aware_decision import ContextAwareDecisionEngine
from app.models import TaskCard
from app.ticket_forecast import generate_ticket_candidates
from app.ticket_audit import audit_intents

ROOT = Path("tasks")
FIT = {
    "jd": ["具体缺什么材料？", "只是保存，还没有提交。", "已经提交了，显示审核中。", "品牌方直接给我们公司授权了。", "授权书上的公司名写错了。", "商标状态截图要包含什么？", "我还要补经销商授权吗？", "能加急吗？", "审核通过了吗？", "公司名称不一致", "截图发你了", "我先不补了"],
    "official": ["工单到哪一步了？", "还要补材料吗？", "多久能出结果？", "审核通过了吗？", "催一下", "材料交给谁", "能加急吗", "下一步做什么"],
    "broadband": ["上不了网怎么办？", "网速很慢怎么办？", "路由器怎么重启？", "光猫灯是红的", "网速还是慢", "路由器后台密码忘了"],
    "network": ["300M 多少钱？", "推荐哪个套餐？", "什么时候能装？", "600M 怎么收费", "家里三台设备选哪个", "能预约安装吗"],
    "logistics": ["快递到哪了？", "物流一直没更新", "怎么联系快递员", "能改地址吗", "今天能送到吗"],
    "invoice": ["开专票还是普票", "抬头写错了怎么办", "发票开好了吗", "怎么重开", "能加急开吗"],
}
HELDOUT = {
    "jd": ["还没点提交呢", "已经审核中了", "公司抬头不一致", "商标注册证可以吗", "经销商授权还缺吗", "品牌方自己授权可以吗", "截图能代替注册证吗", "直接授权行吗"],
    "official": ["审核到哪了", "还要补材料", "多久出结果"],
    "broadband": ["连不上网了", "网速有点卡", "光猫重启可以吗", "路由器后台怎么进"],
    "network": ["600M 多少钱", "家里三台设备选哪个", "能预约安装吗"],
    "logistics": ["快递没收到", "物流卡住了", "快递员电话多少", "地址能改吗"],
    "invoice": ["要开专票", "发票抬头不对", "发票状态在哪看", "重新开票"],
}
CARDS = {
    "jd": "jd-qualification.json",
    "official": "official-work-order.json",
    "broadband": "home-broadband-support.json",
    "network": "network-consultation.json",
    "logistics": "logistics-delivery.json",
    "invoice": "after-sales-invoice.json",
}
PROFILE_INTENTS = {
    "jd": ["how_to_submit"],
    "broadband": ["how_to_submit", "timing"],
    "network": ["how_to_submit", "timing"],
    "logistics": ["timing"],
}


def cards():
    return {key: TaskCard.model_validate_json((ROOT / name).read_text(encoding="utf-8"))
            for key, name in CARDS.items()}


def run(arm_cards, mapping: dict[str, list[str]] | None):
    old = os.environ.pop("TICKET_FORECAST_EXCLUDE_BY_TASK", None)
    if mapping:
        os.environ["TICKET_FORECAST_EXCLUDE_BY_TASK"] = json.dumps(mapping, ensure_ascii=False)
    try:
        out = {}
        for key, card in arm_cards.items():
            tickets = len(generate_ticket_candidates(card))
            fit = audit_intents(card, FIT[key])
            held = audit_intents(card, HELDOUT[key])
            out[key] = {
                "tickets": tickets,
                "fit_hits": sum(fit.hits.values()),
                "held_hits": sum(held.hits.values()),
                "fit_turns": len(FIT[key]),
                "held_turns": len(HELDOUT[key]),
            }
        return out
    finally:
        if old is None:
            os.environ.pop("TICKET_FORECAST_EXCLUDE_BY_TASK", None)
        else:
            os.environ["TICKET_FORECAST_EXCLUDE_BY_TASK"] = old


arm_cards = cards()
mapping = {
    arm_cards[key].title: intents
    for key, intents in PROFILE_INTENTS.items()
}
base = run(arm_cards, None)
pruned = run(arm_cards, mapping)
print(json.dumps({"default": base, "pruned": pruned}, ensure_ascii=False, indent=2))
