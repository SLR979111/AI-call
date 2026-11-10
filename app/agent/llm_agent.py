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

POP_SYSTEM_PROMPT = """你是京东 POP 商家资质审核驳回外呼专员。只输出一个 JSON 对象，不要 Markdown。
字段：
- intent: confirm|ask_reason|ask_action|jd_reject|callback|complaint|unclear
- reply: 一句口语化回复（首句尽量 <=20 字）
- slots: {request_id, brand, reject_l1, reject_l2, reject_l3, handling_mode, remediation}
- tool: 可选，callback.create（登记回电任务）
- tool_args: 对象，仅当 tool 存在
- outcome: rejected|jd_rejected|callback_scheduled|transferred|completed
- citation: 可选，知识来源 id

规则：
- 先确认商家/店铺身份，再说明驳回原因和整改动作。
- 原因必须来自知识库；不要编造三级分类或品牌信息。
- 电话场景：说明原因、整改动作、重新提交路径；京麦驳回场景：明确站内驳回、不强制回电。
- 商家要求回电/没听清：登记 callback.create；投诉/转人工：transfer。
- 不承诺“补了就一定通过”；不泄露其他商家信息。
- 单句超过 2000 字符时先概括，不要截断。
- 若系统提供了本单业务上下文，说明原因/整改时必须使用其中的 request_id、brand、reject_l1/l2/l3，不要泛化成“具体审核项”。
"""


_AFFIRMATIVE_MARKERS = ("可以", "方便", "好的", "好", "行", "没问题", "是", "对", "嗯", "负责人", "听明白", "知道")
_FLOW_HINTS = {
    "IDENTITY_CONFIRM": "当前只做身份确认。用户说可以/方便/是/对/好/嗯/负责人时，intent=confirm，reply 用一句简短确认；不要再次复述驳回问题。",
    "CONFIRM_ACTION": "用户说可以/好/对时，intent=confirm；不要重复上一句。",
}


def _is_affirmative(text: str) -> bool:
    """Short affirmative replies must not be sent through unrelated RAG."""
    clean = text.strip()
    return len(clean) <= 16 and any(marker in clean for marker in _AFFIRMATIVE_MARKERS)


class LLMAgent:
    """Cloud-LLM agent path.

    The LLM proposes one JSON decision per turn; tools still execute only
    through ToolGateway, and compliance remains in the orchestrator.
    """

    def __init__(self, router: LLMRouter, knowledge_path: Path | None = None,
                 flow_pack: FlowPack | None = None, max_history_turns: int = 10,
                 case_context: dict[str, Any] | None = None) -> None:
        self.router = router
        self.knowledge = KnowledgeBase(knowledge_path)
        self.flow_pack = flow_pack
        self.case_context = dict(case_context or {})
        self.sessions: dict[str, list[dict[str, str]]] = {}  # session_id -> conversation history
        self.flow_engines: dict[str, Any] = {}  # session_id -> FlowEngine instance
        self.max_history_turns = max_history_turns

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        """Initialize session with empty conversation history and Flow Engine."""
        self.sessions[session_id] = []

        # Initialize Flow Engine if flow_pack is provided
        if flow_pack and self.flow_pack:
            from ..flow.pack import FlowEngine
            self.flow_engines[session_id] = FlowEngine(self.flow_pack)
            self.flow_engines[session_id].start()

        return None

    def _system(self, flow_state: str = "", slots: dict[str, Any] | None = None) -> str:
        """Build system prompt with context."""
        if not slots:
            slots = {}

        # Base prompt: choose the scenario-specific contract.
        context = POP_SYSTEM_PROMPT if (self.flow_pack and self.flow_pack.id == "pop_qualification_audit") else SYSTEM_PROMPT

        # Add flow context if available
        if flow_state:
            context += f"\n\n当前流程状态：{flow_state}"

        if slots:
            context += f"\n已知信息：{json.dumps(slots, ensure_ascii=False)}"

        # Real contest context: without these fields the local LLM cannot know
        # which merchant/rejection is being discussed.
        if self.case_context:
            context += "\n本单业务上下文：" + json.dumps(self.case_context, ensure_ascii=False)

        if self.flow_pack:
            states = ", ".join(self.flow_pack.states.keys())
            context += f"\n\n业务流程状态集合：{states}；入口：{self.flow_pack.entry}。"

        return context

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", ""))
        reply_id = req.get("reply_id", "r_dev_1")
        turn_id = req.get("turn_id", "t_0000")
        trace_id = req.get("trace_id")
        session_id = req.get("session_id", "s_dev")
        flow_state = str(req.get("flow_state", ""))
        slots = req.get("slots", {})

        # Retrieve conversation history
        history = self.sessions.setdefault(session_id, [])

        # A short "可以/方便/好" is a state transition, not a knowledge query.
        # Retrieving top-2 docs for it injects unrelated POP documents and makes
        # small models repeat the opening question.
        affirmative = flow_state in {"IDENTITY_CONFIRM", "CONFIRM_ACTION"} and _is_affirmative(text)
        chunks = [] if affirmative else self.knowledge.search(text, top_k=2)
        context = "\n".join(f"[{c.doc_id}] {c.title}: {c.content}" for c in chunks)
        user_prompt = text if not context else f"用户说：{text}\n可能相关的知识：\n{context}"

        # Build messages with history
        msg_list = [{"role": "system", "content": self._system(flow_state, slots)}]
        # Keep only recent history (last N turns = 2N messages)
        recent_history = history[-(self.max_history_turns * 2):]
        msg_list.extend(recent_history)
        msg_list.append({"role": "user", "content": user_prompt})

        # Get available tools for Function Calling
        from ..tools.gateway import ToolGateway
        available_tools = ToolGateway.to_openai_tools()

        # Use streaming response to reduce TTFT
        sentence_id = 1
        accumulated_text = ""
        first_sentence_sent = False
        decision: dict[str, Any] = {}
        tool_calls_from_llm: list[dict[str, Any]] = []
        llm_provider = ""
        llm_model = ""

        async for chunk in self.router.complete_stream(
            msg_list,
            tools=available_tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=400,
            session_id=session_id,
            turn_id=turn_id,
            trace_id=trace_id,
        ):
            cancel.check()

            if chunk.type == "error":
                # Handle LLM errors
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                       "text": "抱歉，我这边暂时没听清，您再说一遍好吗？", "final": True}
                yield {"kind": "end", "reply_id": reply_id, "outcome": "llm_failed",
                       "slots": {}, "result": {"error": chunk.error}}
                return

            if chunk.type == "first_token":
                llm_provider = chunk.provider
                llm_model = chunk.model
                continue

            if chunk.type == "sentence":
                # The model is prompted to return JSON. A streaming boundary
                # can fire inside that JSON; never send a JSON fragment to TTS.
                # Keep the stream itself unchanged and only guard the consumer.
                candidate = chunk.text.strip()
                if candidate.startswith("{") or '"intent"' in candidate:
                    accumulated_text += chunk.text + " "
                    continue
                # Stream sentence immediately to reduce latency
                if not first_sentence_sent:
                    yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                           "text": chunk.text, "final": False}
                    first_sentence_sent = True
                    sentence_id += 1
                accumulated_text += chunk.text + " "
                continue

            if chunk.type == "done":
                accumulated_text = chunk.text or accumulated_text.strip()
                tool_calls_from_llm = chunk.tool_calls or []
                llm_provider = chunk.provider
                llm_model = chunk.model
                break

        # Append user message to history
        history.append({"role": "user", "content": text})

        # Parse decision: prefer Function Calling, fallback to JSON mode
        if tool_calls_from_llm:
            # Function Calling path: LLM returned structured tool calls
            # For simplicity, use first tool call (multi-tool support can be added later)
            first_call = tool_calls_from_llm[0]
            tool_name = ToolGateway.resolve_tool_name(first_call["function"]["name"])
            try:
                tool_args = json.loads(first_call["function"]["arguments"])
            except (TypeError, ValueError):
                tool_args = {}

            decision = {
                "intent": "tool_call",
                "reply": accumulated_text or "我来帮您查询一下。",
                "tool": tool_name,
                "tool_args": tool_args,
                "outcome": "tool_called"
            }
        else:
            # JSON mode fallback (for LLMs without Function Calling)
            decision = parse_json_object(accumulated_text) or {}

        # Small local models often label "可以" as unclear even when the flow
        # state is explicitly waiting for confirmation. Keep the state machine
        # authoritative and prevent the same opening question from repeating.
        if affirmative:
            decision["intent"] = "confirm"
            current_reply = str(decision.get("reply", ""))
            ctx = self.case_context
            if flow_state == "IDENTITY_CONFIRM" and ctx.get("request_id") and ctx.get("reject_l3"):
                # Use the real case instead of a context-free "好的，感谢确认".
                decision["reply"] = (
                    f"好的，{ctx.get('brand', '您')}的申请单{ctx['request_id']}，"
                    f"驳回在{ctx.get('reject_l1', '')}/{ctx.get('reject_l2', '')}，"
                    f"原因是{ctx['reject_l3']}。需要按驳回意见补充或修正材料后重新提交。"
                )
            elif not current_reply or "想跟您确认" in current_reply or "方便现在说明" in current_reply:
                decision["reply"] = "好的，感谢确认。"
        intent = str(decision.get("intent", "other"))
        if intent == "transfer":
            yield {"kind": "transfer", "reply_id": reply_id, "reason": "user_request", "queue": "human"}
            return

        # Apply Flow Engine state transition if available
        engine = self.flow_engines.get(session_id)
        flow_result = None
        if engine:
            flow_result = engine.apply_intent(intent)
            # Update slots in Flow Engine
            for slot_name, slot_value in decision.get("slots", {}).items():
                if slot_name in engine.pack.slots:
                    engine.set_slot(slot_name, slot_value)

        # With a real case context, the LLM's reply carries the concrete
        # request/brand/rejection details. Do not overwrite it with the flow
        # pack's generic "具体审核项" sentence.
        if flow_result and flow_result.get("say") and not self.case_context:
            reply = flow_result["say"]
        else:
            reply = str(decision.get("reply") or (flow_result or {}).get("say") or "好的，我明白了。")

        # Only send reply if we haven't sent it via streaming
        if not first_sentence_sent:
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
        result_payload: dict[str, Any] = {"llm_provider": llm_provider, "model": llm_model}
        if chunks:
            result_payload["citation"] = chunks[0].citation()

        # Include Flow Engine state in result
        if engine:
            result_payload["flow_snapshot"] = engine.snapshot()

        # Append assistant reply to history
        assistant_reply = decision.get("reply", "")
        if tool_result and tool_result.get("ok"):
            data = tool_result.get("data") or {}
            assistant_reply += " " + str(data.get("message") or data.get("ticket_id") or "")
        history.append({"role": "assistant", "content": assistant_reply})

        # Trim history to max turns
        if len(history) > self.max_history_turns * 2:
            self.sessions[session_id] = history[-(self.max_history_turns * 2):]

        yield {"kind": "end", "reply_id": reply_id, "outcome": outcome,
               "slots": decision.get("slots") or {}, "result": result_payload}
