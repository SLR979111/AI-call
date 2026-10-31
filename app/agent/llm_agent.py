from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..flow.pack import FlowPack
from ..llm.router import LLMRouter, messages, parse_json_object
from ..rag.knowledge import KnowledgeBase

SYSTEM_PROMPT = """你是电话外呼客服数字员工。只输出一个 JSON 对象，不要 Markdown。
字段：
- intent: confirm|deny|unclear|order_query|reschedule|faq|transfer|other
- reply: 要对用户说的一句话（首句尽量 <=20 字，口语化）
- slots: 对象，已知的订单号/时间等
- tool: 可选，order.query|order.reschedule|ticket.create|lead.query
- tool_args: 对象，仅当 tool 存在
- outcome: rescheduled|answered|transferred|completed|tool_failed
- citation: 可选，知识来源 id

规则：
- 不承诺无法保证的结果，不使用“保证/一定/绝对/百分百”。
- 用户要求人工/投诉时，intent=transfer。
- 信息不足时先追问，不要编造订单信息。
"""


class LLMAgent:
    """Cloud-LLM agent path.

    The LLM proposes one JSON decision per turn; tools still execute only
    through ToolGateway, and compliance remains in the orchestrator.
    """

    def __init__(self, router: LLMRouter, knowledge_path: Path | None = None,
                 flow_pack: FlowPack | None = None) -> None:
        self.router = router
        self.knowledge = KnowledgeBase(knowledge_path)
        self.flow_pack = flow_pack

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        return None

    def _system(self) -> str:
        if not self.flow_pack:
            return SYSTEM_PROMPT
        states = ", ".join(self.flow_pack.states.keys())
        return SYSTEM_PROMPT + f"\n当前业务流程状态集合：{states}；入口：{self.flow_pack.entry}。"

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", ""))
        reply_id = req.get("reply_id", "r_dev_1")
        turn_id = req.get("turn_id", "t_0000")
        trace_id = req.get("trace_id")
        session_id = req.get("session_id", "s_dev")
        chunks = self.knowledge.search(text, top_k=2)
        context = "\n".join(f"[{c.doc_id}] {c.title}: {c.content}" for c in chunks)
        user_prompt = text if not context else f"用户说：{text}\n可能相关的知识：\n{context}"

        result = await self.router.complete(
            messages(self._system(), user_prompt),
            temperature=0.2,
            max_tokens=400,
            response_format={"type": "json_object"},
            session_id=session_id,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        cancel.check()
        if not result.ok:
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": "抱歉，我这边暂时没听清，您再说一遍好吗？", "final": True}
            yield {"kind": "end", "reply_id": reply_id, "outcome": "llm_failed",
                   "slots": {}, "result": {"error": result.error}}
            return

        decision = parse_json_object(result.text) or {}
        intent = str(decision.get("intent", "other"))
        if intent == "transfer":
            yield {"kind": "transfer", "reply_id": reply_id, "reason": "user_request", "queue": "human"}
            return

        sentence_id = 1
        reply = str(decision.get("reply") or "好的，我明白了。")
        yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
               "text": reply, "final": decision.get("tool") is None}
        sentence_id += 1
        cancel.check()

        tool = decision.get("tool")
        tool_result: dict[str, Any] | None = None
        if tool:
            args = decision.get("tool_args") or {}
            tool_result = await tools.invoke(
                str(tool), args, session_id=session_id, turn_id=turn_id, trace_id=trace_id,
            )
            cancel.check()
            if tool_result.get("ok"):
                data = tool_result.get("data") or {}
                summary = str(data.get("message") or data.get("ticket_id") or "已经为您处理好了。")
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                       "text": summary, "final": True}
                sentence_id += 1
            else:
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                       "text": "抱歉，业务系统暂时没有响应，我先帮您登记，稍后人工跟进。",
                       "final": True}
                sentence_id += 1
                await tools.invoke("ticket.create",
                                   {"summary": "LLM tool failed, manual follow-up"},
                                   session_id=session_id, turn_id=turn_id, trace_id=trace_id)

        outcome = str(decision.get("outcome") or ("tool_failed" if tool and not (tool_result or {}).get("ok")
                                                  else "completed"))
        result_payload: dict[str, Any] = {"llm_provider": result.provider, "model": result.model}
        if chunks:
            result_payload["citation"] = chunks[0].citation()
        yield {"kind": "end", "reply_id": reply_id, "outcome": outcome,
               "slots": decision.get("slots") or {}, "result": result_payload}
