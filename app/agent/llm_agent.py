from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..flow.pack import FlowPack
from ..llm.router import LLMRouter, messages, parse_json_object
from ..rag.knowledge import KnowledgeBase
from .reply_stream import ReplyExtractor

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


PROACTIVE_RULES = """
你是主动外呼的客服，不是被动问答助手。
每一轮都必须主动让商家知道：
1. 为什么联系他；
2. 当前结论/驳回原因；
3. 需要他具体做什么（补什么材料、怎么操作）；
4. 下一步是什么。
不要问“您有什么问题”，不要等商家来问；不要只回“好的/您说”。
每轮只推进一个明确动作，语气礼貌、直接、像真人客服。
"""


class LLMAgent:
    """Cloud-LLM agent path.

    The LLM proposes one JSON decision per turn; tools still execute only
    through ToolGateway, and compliance remains in the orchestrator.
    """

    opens_outbound = True

    def __init__(self, router: LLMRouter, knowledge_path: Path | None = None,
                 flow_pack: FlowPack | None = None, max_history_turns: int = 10,
                 case_context: dict[str, Any] | None = None,
                 stream_mode: str | None = None) -> None:
        self.router = router
        self.knowledge = KnowledgeBase(knowledge_path)
        self.flow_pack = flow_pack
        self.case_context = dict(case_context or {})
        # buffered: parse the complete JSON before speaking (safe default).
        # reply: emit only the natural-language `reply` field from the stream.
        self.stream_mode = (stream_mode or os.getenv("LLM_STREAM_MODE", "buffered")).strip().lower()
        if self.stream_mode not in {"buffered", "reply"}:
            raise ValueError("LLM_STREAM_MODE must be buffered or reply")
        self.sessions: dict[str, list[dict[str, str]]] = {}  # session_id -> conversation history
        self.flow_engines: dict[str, Any] = {}  # session_id -> FlowEngine instance
        self.fillers_spoken: dict[str, set[str]] = {}
        self.opened_sessions: set[str] = set()
        self.identity_verified: set[str] = set()
        self.max_history_turns = max_history_turns
        self.max_tokens = max(32, int(os.getenv("LLM_MAX_TOKENS", "160")))
        self.stage_wait = os.getenv("LLM_STAGE_WAIT", "亲，请您稍等。").strip()
        self._fillers = {
            "IDENTITY_CONFIRM": os.getenv("LLM_FILLER_IDENTITY", "好的，我明白了。"),
            "CONFIRM_ACTION": os.getenv("LLM_FILLER_CONFIRM", "好的，我帮您确认一下。"),
            "OFFER": os.getenv("LLM_FILLER_OFFER", "好的，我帮您看一下这笔申请。"),
            "RESULT_CONFIRM": os.getenv("LLM_FILLER_RESULT", "好的，我帮您确认处理结果。"),
            "DEFAULT": os.getenv("LLM_FILLER_DEFAULT", "好的，我收到您的信息了。"),
        }

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        """Initialize session with empty conversation history and Flow Engine."""
        self.sessions[session_id] = []
        self.fillers_spoken[session_id] = set()
        self.opened_sessions.discard(session_id)
        self.identity_verified.discard(session_id)

        # Initialize Flow Engine if flow_pack is provided
        if flow_pack and self.flow_pack:
            from ..flow.pack import FlowEngine
            self.flow_engines[session_id] = FlowEngine(self.flow_pack)
            self.flow_engines[session_id].start()

        return None

    def _system(self, flow_state: str = "", slots: dict[str, Any] | None = None,
                case_context: dict[str, Any] | None = None) -> str:
        """Build system prompt with context."""
        if not slots:
            slots = {}
        ctx = self.case_context if case_context is None else case_context

        # Base prompt: choose the scenario-specific contract.
        context = POP_SYSTEM_PROMPT if (self.flow_pack and self.flow_pack.id == "pop_qualification_audit") else SYSTEM_PROMPT

        # Add flow context if available
        if flow_state:
            context += f"\n\n当前流程状态：{flow_state}"

        if slots:
            context += f"\n已知信息：{json.dumps(slots, ensure_ascii=False)}"

        # Real contest context: without these fields the local LLM cannot know
        # which merchant/rejection is being discussed.
        if ctx:
            context += "\n本单业务上下文：" + json.dumps(ctx, ensure_ascii=False)

        if self.flow_pack:
            states = ", ".join(self.flow_pack.states.keys())
            context += f"\n\n业务流程状态集合：{states}；入口：{self.flow_pack.entry}。"

        return context

    def _identity_script(self) -> str:
        ctx = self.case_context
        reason = str(ctx.get("reject_l3") or "资质材料")
        return f"您的申请被驳回，原因是{reason}。请更新有效资质后重新提交。"

    @staticmethod
    def _is_closing(text: str) -> bool:
        compact = re.sub(r"[\s，,。.!！?？]", "", text or "")
        if not compact or "?" in text or "？" in text:
            return False
        return compact in {
            "谢谢", "好的谢谢", "好谢谢", "谢谢再见", "好的谢谢再见",
            "再见", "拜拜", "知道了", "明白了", "不用了",
        }

    @staticmethod
    def _limit_reply(reply: str, limit: int = 50) -> str:
        if len(reply) <= limit:
            return reply
        cut = reply[:limit]
        for index in range(len(cut) - 1, 0, -1):
            if cut[index] in "。！？!?；;":
                return cut[:index + 1]
        return cut.rstrip("，,、 ") + "。"

    @staticmethod
    def _strip_repeated_identity(reply: str) -> str:
        """Remove a second self-introduction / identity question."""
        cleaned = re.sub(
            r"(?:您好[，,]?)?我是[^。！？\n]{0,24}(?:客服|专员|助手)[。！？]?",
            "", reply,
        )
        cleaned = re.sub(
            r"请问您是[^。！？\n]{0,30}负责人吗[？?]?", "", cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or "好的，我明白了。"

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", ""))
        reply_id = req.get("reply_id", "r_dev_1")
        turn_id = req.get("turn_id", "t_0000")
        trace_id = req.get("trace_id")
        session_id = req.get("session_id", "s_dev")
        flow_state = str(req.get("flow_state", ""))
        slots = req.get("slots", {})

        if not text.strip():
            # Outbound opening: the bridge calls handle_text("") once after the
            # media track is published. Do not wait for the user to speak first.
            self.opened_sessions.add(session_id)
            yield {"kind": "stage", "stage": "IDENTITY_CONFIRM"}
            brand = str(self.case_context.get("brand") or "您")
            greeting = (
                "您好，我是京东商家资质审核AI客服。本次通话会录音。"
                "有一笔店铺资质审核需要和负责人确认，请问您是店铺负责人吗？"
            )
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": greeting, "final": True}
            yield {"kind": "end", "reply_id": reply_id, "outcome": "pending_identity",
                   "slots": {}, "result": {}}
            return

        # Retrieve conversation history
        history = self.sessions.setdefault(session_id, [])
        has_prior = session_id in self.opened_sessions or bool(history)
        verified = session_id in self.identity_verified
        safe_context = self.case_context if verified else {}

        if self._is_closing(text):
            closing = "好的，祝您生意兴隆，再见。"
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": closing})
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": closing, "final": True, "filler": False}
            yield {"kind": "end", "reply_id": reply_id, "outcome": "completed",
                   "slots": dict(slots), "result": {"llm_provider": "rule", "model": "closing"}}
            return

        # A short "可以/方便/好" is a state transition, not a knowledge query.
        # Retrieving top-2 docs for it injects unrelated POP documents and makes
        # small models repeat the opening question.
        affirmative = flow_state in {"IDENTITY_CONFIRM", "CONFIRM_ACTION"} and _is_affirmative(text)
        if affirmative and flow_state == "IDENTITY_CONFIRM":
            # Identity is now confirmed. Only from this point may the case
            # details be disclosed. Use a deterministic first script so the
            # formal reply does not wait for LLM generation.
            self.identity_verified.add(session_id)
            engine = self.flow_engines.get(session_id)
            flow_result = engine.apply_intent("confirm") if engine else None
            script = self._identity_script()
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": script})
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": script, "final": True, "filler": False}
            result = {"llm_provider": "rule", "model": "pop-identity-script"}
            if engine:
                result["flow_snapshot"] = engine.snapshot()
            yield {"kind": "end", "reply_id": reply_id, "outcome": "completed",
                   "slots": dict(engine.slots) if engine else {}, "result": result}
            return
        chunks = [] if affirmative else self.knowledge.search(text, top_k=2)
        context = "\n".join(f"[{c.doc_id}] {c.title}: {c.content}" for c in chunks)
        user_prompt = text if not context else f"用户说：{text}\n可能相关的知识：\n{context}"
        needs_callback = bool(re.search(
            r"回电|回拨|电话联系|登记|转人工|人工客服|callback", text
        ))
        plain_mode = os.getenv("LLM_PLAIN_REPLY", "0") == "1" and not needs_callback

        # Build messages with history. Ordinary turns use plain text: there is
        # no JSON field to finish before the first spoken sentence.
        if plain_mode:
            system_prompt = (
                "你是京东POP商家资质审核外呼AI客服。第一句先给结论，不超过15个汉字；"
                "第二句说明需要商家做什么，总回复不超过40字。只回口语化中文，"
                "不要JSON、Markdown、解释或自我介绍。不要重复已经确认过的身份。"
                + PROACTIVE_RULES
                + f"\n当前阶段：{flow_state or 'OPEN'}"
            )
            if slots:
                system_prompt += f"\n已知信息：{json.dumps(slots, ensure_ascii=False)}"
            if self.case_context:
                system_prompt += "\n本单业务上下文：" + json.dumps(safe_context, ensure_ascii=False)
        else:
            system_prompt = self._system(flow_state, slots, case_context=safe_context) + PROACTIVE_RULES + (
                "\n输出 JSON 时字段顺序必须是 reply 在前，先写 reply，再写 intent/tool。"
            )
        msg_list = [{"role": "system", "content": system_prompt}]
        # Keep only recent history (last N turns = 2N messages)
        recent_history = history[-(self.max_history_turns * 2):]
        msg_list.extend(recent_history)
        msg_list.append({"role": "user", "content": user_prompt})

        # Get available tools for Function Calling. Keep the schema small for
        # latency; the POP turn only needs callback registration.
        from ..tools.gateway import ToolGateway
        raw_allow = os.getenv("LLM_TOOL_ALLOWLIST", "dynamic").strip()
        if raw_allow.lower() in {"", "none", "off"}:
            allow = set()
        elif raw_allow.lower() == "dynamic":
            # Most turns do not need a tool. Only expose the callback tool for
            # explicit callback/transfer intent so the schema stays tiny.
            allow = {"callback.create"} if needs_callback else set()
        else:
            allow = {item.strip() for item in raw_allow.split(",") if item.strip()}
        available_tools = ToolGateway.to_openai_tools(allow)

        # Use streaming response to reduce TTFT
        sentence_id = 1
        accumulated_text = ""
        first_sentence_sent = False
        decision: dict[str, Any] = {}
        tool_calls_from_llm: list[dict[str, Any]] = []
        llm_provider = ""
        llm_model = ""
        reply_extractor = ReplyExtractor() if (self.stream_mode == "reply" and not plain_mode) else None
        streamed_reply = ""
        streamed_prefix = ""
        plain_buffer = ""

        # Speak each phase filler at most once per session. The real answer
        # still follows; only the acknowledgement is de-duplicated.
        phase = flow_state.upper() if flow_state.upper() in self._fillers else "DEFAULT"
        spoken = self.fillers_spoken.setdefault(session_id, set())
        filler = self._fillers[phase].strip()
        if filler and phase not in spoken:
            spoken.add(phase)
            if self.stage_wait:
                filler = f"{filler}{self.stage_wait}"
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                   "text": filler, "final": False, "filler": True}
            sentence_id += 1

        async for chunk in self.router.complete_stream(
            msg_list,
            tools=[] if plain_mode else available_tools,
            tool_choice="auto",
            response_format=None if plain_mode else {"type": "json_object"},
            temperature=0.2,
            max_tokens=int(os.getenv("LLM_PLAIN_MAX_TOKENS", "96")) if plain_mode else self.max_tokens,
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
                yield {"kind": "first_token", "provider": llm_provider, "model": llm_model,
                       "latency_ms": chunk.latency_ms}
                continue

            if chunk.type == "token":
                if plain_mode:
                    accumulated_text += chunk.text
                    plain_buffer += chunk.text
                    if not first_sentence_sent:
                        # Cut the first spoken clause early instead of waiting
                        # for the router's 15-char sentence boundary.
                        match = re.search(r"[。！？!?；;，,]", plain_buffer)
                        if match and match.start() >= 6:
                            candidate = plain_buffer[:match.end()].strip()
                            candidate = (self._strip_repeated_identity(candidate)
                                         if has_prior else candidate)
                            if candidate:
                                streamed_prefix = candidate
                                yield {"kind": "sentence", "reply_id": reply_id,
                                       "sentence_id": sentence_id, "text": candidate,
                                       "final": False}
                                first_sentence_sent = True
                                sentence_id += 1
                    continue
                # Reply mode extracts only the JSON `reply` value from tokens;
                # raw JSON is never passed to TTS. Buffered mode keeps the
                # existing complete-JSON validation path.
                if reply_extractor is not None:
                    value = reply_extractor.feed(chunk.text)
                    if value:
                        streamed_reply = value
                        candidate = self._strip_repeated_identity(value) if has_prior else value
                        if candidate and not first_sentence_sent:
                            streamed_prefix = candidate
                            yield {"kind": "sentence", "reply_id": reply_id,
                                   "sentence_id": sentence_id, "text": candidate, "final": False}
                            first_sentence_sent = True
                            sentence_id += 1
                    elif not first_sentence_sent:
                        partial = reply_extractor.first_sentence()
                        if partial:
                            candidate = self._strip_repeated_identity(partial) if has_prior else partial
                            if candidate:
                                streamed_prefix = candidate
                                yield {"kind": "sentence", "reply_id": reply_id,
                                       "sentence_id": sentence_id, "text": candidate, "final": False}
                                first_sentence_sent = True
                                sentence_id += 1
                continue

            if chunk.type == "sentence":
                if plain_mode:
                    candidate = self._strip_repeated_identity(chunk.text) if has_prior else chunk.text
                    candidate = candidate.strip()
                    if candidate and not first_sentence_sent:
                        streamed_prefix = candidate
                        yield {"kind": "sentence", "reply_id": reply_id,
                               "sentence_id": sentence_id, "text": candidate, "final": False}
                        first_sentence_sent = True
                        sentence_id += 1
                    continue
                if self.stream_mode == "buffered":
                    # Buffered mode waits for the complete JSON object. Sending
                    # streamed sentence chunks here would leak JSON fragments
                    # (e.g. '"reply": "您好') into TTS.
                    continue
                if self.stream_mode == "reply":
                    # In reply mode token chunks are the source of truth; the
                    # sentence chunks contain the raw JSON wrapper.
                    continue
                # Buffered mode: never send a partial JSON object to TTS.
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
            # Plain-text turns have no JSON decision. Keep only the intent
            # needed by the state machine; the reply is the model text itself.
            if plain_mode:
                intent = "confirm" if affirmative else (
                    "callback" if needs_callback else "other"
                )
                decision = {"intent": intent, "reply": accumulated_text.strip(), "tool": None}
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

        # The outbound opening already introduced the agent and asked whether
        # the user is the owner. Small models often repeat that whole identity
        # sentence on every later turn; remove it once the opening is over.
        if has_prior:
            reply = self._strip_repeated_identity(reply)
        reply = self._limit_reply(
            reply, int(os.getenv("LLM_MAX_REPLY_CHARS", "50"))
        )

        # If only the first sentence was streamed, emit the remaining suffix
        # once the full reply is known; otherwise emit the whole reply.
        if first_sentence_sent and streamed_prefix and reply.startswith(streamed_prefix):
            suffix = reply[len(streamed_prefix):].strip()
            if suffix:
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                       "text": suffix, "final": decision.get("tool") is None}
                sentence_id += 1
        elif not first_sentence_sent:
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
