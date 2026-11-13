"""Deterministic POP stage presets.

Common normal/abnormal merchant replies must not be left to a free-form LLM:
the agent should advance one stage, never re-ask a confirmed field, and handle
wrong contact / refusal / transfer / abuse safely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    reply: str
    intent: str | None = None
    outcome: str = "completed"
    close: bool = False


def compact(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？]", "", text or "")


def classify(text: str) -> str:
    t = compact(text)
    if not t:
        return "empty"
    if any(x in t for x in ("骗子", "骚扰", "别再打", "不要打", "烦死", "举报")):
        return "abuse"
    if any(x in t for x in ("投诉", "转人工", "人工客服", "找人工", "客服经理")):
        return "transfer"
    if any(x in t for x in ("谢谢", "再见", "拜拜", "知道了", "明白了")):
        return "closing"
    if any(x in t for x in ("忙", "没空", "晚点", "稍后", "等会", "发短信")):
        return "busy"
    if any(x in t for x in ("不用", "不需要", "不想", "没兴趣", "不补", "不提交", "拒绝")):
        return "negative"
    if any(x in t for x in ("为什么", "什么意思", "没听懂", "再说一遍", "怎么", "什么")):
        return "confused"
    if any(x in t for x in ("已经提交", "补过了", "提交了", "更新了", "已经补")):
        return "already"
    if t in {"好", "好的", "行", "可以", "方便", "对", "是的", "嗯", "好呀"}:
        return "affirmative"
    return "other"


def preset_for(stage: str, text: str, ctx: dict, *, verified: bool) -> Preset | None:
    kind = classify(text)
    reason = str(ctx.get("reject_l3") or "资质材料")
    action = str(ctx.get("remediation") or "按驳回意见更新材料后重新提交")

    # Safety / control intents apply in every stage.
    if kind == "abuse":
        return Preset("好的，如您暂时不需要，我们不再打扰，再见。", outcome="closing", close=True)
    if kind == "transfer":
        return Preset("好的，我帮您转接人工审核专员，请稍等。", outcome="transferred")
    if kind == "busy":
        return Preset("好的，我稍后方便时再联系您，再见。", outcome="closing", close=True)
    if kind == "negative":
        return Preset("好的，如您暂时不需要，我们不再打扰，再见。", outcome="closing", close=True)

    if stage == "IDENTITY_CONFIRM":
        if kind == "confused":
            return Preset("抱歉，我需要先确认一下，请问您是店铺负责人吗？",
                          outcome="pending_identity")
        return None

    if stage == "REASON":
        if not verified:
            return Preset("抱歉，我需要先确认一下，请问您是店铺负责人吗？",
                          outcome="pending_identity")
        return Preset(f"原因是{reason}。{action}。", intent="confirm")

    if stage == "REMEDIATION":
        if not verified:
            return Preset("抱歉，我需要先确认一下，请问您是店铺负责人吗？",
                          outcome="pending_identity")
        return Preset(f"请更新{reason}，{action}，提交后我们会再次审核。", intent="confirm")

    if stage == "CONFIRM_ACTION":
        if kind in {"confused", "other"}:
            return Preset(f"已确认材料问题，请{action}。是否现在按这个方式处理？",
                          outcome="pending_confirm")
        if kind == "already":
            return Preset("好的，您已提交，我会按本次材料继续跟进。", outcome="completed")
        if kind == "affirmative":
            return Preset("好的，我为您登记处理结果，请按驳回意见更新材料后重新提交。",
                          intent="confirm", outcome="completed")

    if stage == "WRITEBACK":
        return Preset("已为您登记处理结果，审核一般 1–3 个工作日处理。",
                      intent="confirm", outcome="completed")

    if stage in {"CLOSE", "END"} or kind == "closing":
        return Preset("好的，祝您生意兴隆，再见。", outcome="closing", close=True)

    return None
