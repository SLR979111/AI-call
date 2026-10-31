from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..rag.knowledge import KnowledgeBase


class FakeAgent:
    """Rule-based Agent for the skeleton.

    It exposes the same interface the real Agent will use, so swapping in
    LLM/Flow/RAG later does not change the orchestrator.
    """

    def __init__(self, knowledge_path: Path | None = None, pause_ms: int = 200) -> None:
        if knowledge_path is None:
            candidate = Path(__file__).resolve().parents[2] / "knowledge" / "faq.json"
            knowledge_path = candidate if candidate.exists() else None
        self.knowledge = KnowledgeBase(knowledge_path)
        self.pause_s = max(0.0, pause_ms / 1000.0)

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        return None

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", ""))
        reply_id = req.get("reply_id", "r_dev_1")
        turn_id = req.get("turn_id", "t_0000")
        trace_id = req.get("trace_id")
        session_id = req.get("session_id", "s_dev")

        async def pause() -> None:
            await asyncio.sleep(self.pause_s)
            cancel.check()

        if any(word in text for word in ("转人工", "找人工", "投诉")):
            await pause()
            yield {"kind": "transfer", "reply_id": reply_id, "reason": "user_request", "queue": "human"}
            return

        if any(word in text for word in ("权益", "退款", "多久", "保修", "政策", "录音", "AI")):
            await pause()
            chunks = self.knowledge.search(text, top_k=1)
            if chunks:
                top = chunks[0]
                await pause()
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                       "text": f"根据知识库《{top.title}》：{top.content}", "final": True}
                await pause()
                yield {"kind": "end", "reply_id": reply_id, "outcome": "answered",
                       "slots": {}, "result": {"citation": top.citation()}}
                return

        if any(word in text for word in ("订单", "改约", "改到", "改吧", "配送")):
            await pause()
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": "可以的，我先帮您看一下订单。", "final": False}
            await pause()
            res = await tools.invoke(
                "order.query", {"order_id": "A12345678"},
                session_id=session_id, turn_id=turn_id, trace_id=trace_id,
            )
            if not res.get("ok"):
                await pause()
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 2,
                       "text": "抱歉，订单系统暂时查不到，我先帮您登记一个工单。", "final": False}
                await tools.invoke(
                    "ticket.create", {"summary": "订单查询失败，需人工跟进"},
                    session_id=session_id, turn_id=turn_id, trace_id=trace_id,
                )
                await pause()
                yield {"kind": "end", "reply_id": reply_id, "outcome": "tool_failed",
                       "slots": {"order_id": "A12345678"}, "result": {"ticket": "created"}}
                return
            data = res.get("data") or {}
            await pause()
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 2,
                   "text": f"订单目前显示{data.get('promised_at', '明天上午')}送达。", "final": False}
            await pause()
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 3,
                   "text": "可以帮您改到明天下午 3 点，您看方便吗？", "final": True}
            await pause()
            yield {"kind": "end", "reply_id": reply_id, "outcome": "rescheduled",
                   "slots": {"order_id": "A12345678"},
                   "result": {"new_time": "2026-11-01T15:00", "ticket": "pending"}}
            return

        await pause()
        yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
               "text": "好的，我明白了。", "final": False}
        await pause()
        yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 2,
               "text": "您方便再说一下具体需求吗？", "final": True}
        await pause()
        yield {"kind": "end", "reply_id": reply_id, "outcome": "completed", "slots": {}, "result": {}}
