"""Generate a phone guidance document from a normalized ticket."""
from __future__ import annotations

import json
import re
from typing import Any

from .models import GuidanceDocument, GuidanceScript, TicketInput

FORBIDDEN = (
    "保证通过", "包过", "百分百", "一定通过", "必过",
    "今天一定", "明天一定", "加急一定", "加急保证", "加急处理",
    "已帮您提交", "已转人工", "已为您登记",
    "京东商家资质审核客服", "京东官方审核人员",
)

PROMPT = """你是电话外呼指导生成器。根据工单只输出一个 JSON 对象，不要 Markdown，不要解释。
字段：
- title: 简短标题
- reason: 驳回原因，必须来自工单
- materials: 需要补或修正的材料数组
- steps: 操作步骤数组
- script: {opening, reason, action, closing}，口语、短句、像电话客服
- forbidden: 禁止承诺数组
- checkpoints: 需要确认的要点 id 数组

硬约束：
1. 不得承诺通过、加急或审核时限；官方页面的“预计一个工作日内审核通过”只能作为参考，不能变成保证
2. 不得编造工单没有的入口、网址、电话、时限
3. 不得建议涂改、修图或绕过审核
4. 没有信息就直说无法确认，不要补空事实
5. 只做沟通说明，不声称已登记、已提交、已转人工
6. 不得自称京东官方或京东审核人员；必须说明自己是协助核对资料的第三方客服
7. 不得把本次案例的授权链假设说成京东对所有商家的统一规定
"""


def _first_nonempty(*values: str) -> str:
    return next((value.strip() for value in values if value and value.strip()), "")


def _reason(ticket: TicketInput) -> str:
    parts = [_first_nonempty(ticket.reject_l1, ""), _first_nonempty(ticket.reject_l2, ""),
             _first_nonempty(ticket.reject_l3, "")]
    return " / ".join(part for part in parts if part) or "资质材料不符合本次申请要求"


def _materials(ticket: TicketInput, reason: str) -> list[str]:
    if ticket.materials:
        return list(ticket.materials)
    if ticket.remediation:
        return [ticket.remediation]
    if "商标" in reason:
        return ["清晰的商标当前状态截图，能看清商标名称、注册号和状态"]
    if "授权" in reason:
        return ["按本次驳回意见补齐或修正授权文件"]
    return ["按本次书面驳回意见补充或修正材料"]


def _steps(ticket: TicketInput) -> list[str]:
    if ticket.steps:
        return list(ticket.steps)
    return ["打开京麦本次被驳回的原申请", "按书面驳回意见补充或修正材料", "在原申请入口重新提交并等待复审"]


def _forbidden_used(doc: GuidanceDocument) -> bool:
    text = " ".join([
        doc.title, doc.reason, " ".join(doc.materials), " ".join(doc.steps),
        doc.script.opening, doc.script.reason, doc.script.action, doc.script.closing,
    ])
    return any(term in text for term in FORBIDDEN)


def template_document(ticket: TicketInput) -> GuidanceDocument:
    reason = _reason(ticket)
    materials = _materials(ticket, reason)
    steps = _steps(ticket)
    action = ticket.remediation or "按本次书面驳回意见补充或修正材料"
    doc = GuidanceDocument.new(
        ticket_id=ticket.ticket_id,
        request_id=ticket.request_id,
        brand=ticket.brand,
        title=f"{ticket.brand or '店铺'}资质退回电话指导",
        reason=reason,
        materials=materials,
        steps=steps,
        script=GuidanceScript(
            opening="您好，我是协助核对京东店铺资料的第三方客服，不是京东审核人员。想跟您说明这次资料退回的原因和下一步。",
            reason=f"本次退回原因是{reason}。",
            action=f"您需要{action}，补好后按原申请提示重新提交。",
            closing="结果以复审为准。您清楚需要补什么、怎么提交了吗？",
        ),
        forbidden=list(FORBIDDEN),
        checkpoints=["reason", "materials", "steps", "submission"],
        source="template",
    )
    if _forbidden_used(doc):
        # A template must never promise approval; fail closed instead of returning it.
        raise ValueError("guidance contains forbidden approval promise")
    return doc


def llm_messages(ticket: TicketInput) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(ticket.model_dump(), ensure_ascii=False)},
    ]


def parse_llm_document(ticket: TicketInput, text: str) -> GuidanceDocument:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    data: Any = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM guidance is not an object")
    script = data.get("script") or {}
    if not isinstance(script, dict):
        raise ValueError("script is not an object")
    doc = GuidanceDocument.new(
        ticket_id=ticket.ticket_id,
        request_id=ticket.request_id,
        brand=ticket.brand,
        title=str(data.get("title") or f"{ticket.brand or '店铺'}资质退回电话指导"),
        reason=str(data.get("reason") or _reason(ticket)),
        materials=[str(x) for x in (data.get("materials") or _materials(ticket, _reason(ticket)))],
        steps=[str(x) for x in (data.get("steps") or _steps(ticket))],
        script=GuidanceScript(
            opening=str(script.get("opening") or "您好，我是协助核对京东店铺资料的第三方客服，不是京东审核人员。"),
            reason=str(script.get("reason") or f"本次退回原因是{_reason(ticket)}。"),
            action=str(script.get("action") or "请按本次书面驳回意见补充或修正材料后重新提交。"),
            closing=str(script.get("closing") or "结果以复审为准。您清楚了吗？"),
        ),
        forbidden=[str(x) for x in (data.get("forbidden") or FORBIDDEN)],
        checkpoints=[str(x) for x in (data.get("checkpoints") or ["reason", "materials", "steps", "submission"])],
        source="llm",
    )
    if _forbidden_used(doc):
        raise ValueError("LLM guidance contains forbidden approval promise")
    return doc
