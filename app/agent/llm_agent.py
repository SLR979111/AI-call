from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..flow.pack import FlowPack
from ..llm.router import LLMRouter, messages, parse_json_object
from ..rag.knowledge import KnowledgeBase
from .reply_stream import ReplyExtractor

SYSTEM_PROMPT = """浣犳槸鐢佃瘽澶栧懠瀹㈡湇鏁板瓧鍛樺伐銆傚彧杈撳嚭涓€涓?JSON 瀵硅薄锛屼笉瑕?Markdown銆?
瀛楁锛?
- intent: confirm|deny|unclear|order_query|reschedule|faq|transfer|other
- reply: 瑕佸鐢ㄦ埛璇寸殑涓€鍙ヨ瘽锛堥鍙ュ敖閲?<=20 瀛楋紝鍙ｈ鍖栵級
- slots: 瀵硅薄锛屽凡鐭ョ殑璁㈠崟鍙?鏃堕棿绛?
- tool: 鍙€夛紝order.query|order.reschedule|ticket.create|lead.query
- tool_args: 瀵硅薄锛屼粎褰?tool 瀛樺湪
- outcome: rescheduled|answered|transferred|completed|tool_failed
- citation: 鍙€夛紝鐭ヨ瘑鏉ユ簮 id

瑙勫垯锛?
- 涓嶆壙璇烘棤娉曚繚璇佺殑缁撴灉锛屼笉浣跨敤鈥滀繚璇?涓€瀹?缁濆/鐧惧垎鐧锯€濄€?
- 鐢ㄦ埛瑕佹眰浜哄伐/鎶曡瘔鏃讹紝intent=transfer銆?
- 淇℃伅涓嶈冻鏃跺厛杩介棶锛屼笉瑕佺紪閫犺鍗曚俊鎭€?
"""

POP_SYSTEM_PROMPT = """浣犳槸浜笢 POP 鍟嗗璧勮川瀹℃牳椹冲洖澶栧懠涓撳憳銆傚彧杈撳嚭涓€涓?JSON 瀵硅薄锛屼笉瑕?Markdown銆?
瀛楁锛?
- intent: confirm|ask_reason|ask_action|jd_reject|callback|complaint|unclear
- reply: 涓€鍙ュ彛璇寲鍥炲锛堥鍙ュ敖閲?<=20 瀛楋級
- slots: {request_id, brand, reject_l1, reject_l2, reject_l3, handling_mode, remediation}
- tool: 鍙€夛紝callback.create锛堢櫥璁板洖鐢典换鍔★級
- tool_args: 瀵硅薄锛屼粎褰?tool 瀛樺湪
- outcome: rejected|jd_rejected|callback_scheduled|transferred|completed
- citation: 鍙€夛紝鐭ヨ瘑鏉ユ簮 id

瑙勫垯锛?
- 鍏堢‘璁ゅ晢瀹?搴楅摵韬唤锛屽啀璇存槑椹冲洖鍘熷洜鍜屾暣鏀瑰姩浣溿€?
- 鍘熷洜蹇呴』鏉ヨ嚜鐭ヨ瘑搴擄紱涓嶈缂栭€犱笁绾у垎绫绘垨鍝佺墝淇℃伅銆?
- 鐢佃瘽鍦烘櫙锛氳鏄庡師鍥犮€佹暣鏀瑰姩浣溿€侀噸鏂版彁浜よ矾寰勶紱浜害椹冲洖鍦烘櫙锛氭槑纭珯鍐呴┏鍥炪€佷笉寮哄埗鍥炵數銆?
- 鍟嗗瑕佹眰鍥炵數/娌″惉娓咃細鐧昏 callback.create锛涙姇璇?杞汉宸ワ細transfer銆?
- 涓嶆壙璇衡€滆ˉ浜嗗氨涓€瀹氶€氳繃鈥濓紱涓嶆硠闇插叾浠栧晢瀹朵俊鎭€?
- 鍗曞彞瓒呰繃 2000 瀛楃鏃跺厛姒傛嫭锛屼笉瑕佹埅鏂€?
- 鑻ョ郴缁熸彁渚涗簡鏈崟涓氬姟涓婁笅鏂囷紝璇存槑鍘熷洜/鏁存敼鏃跺繀椤讳娇鐢ㄥ叾涓殑 request_id銆乥rand銆乺eject_l1/l2/l3锛屼笉瑕佹硾鍖栨垚鈥滃叿浣撳鏍搁」鈥濄€?
"""


_AFFIRMATIVE_MARKERS = ("鍙互", "鏂逛究", "濂界殑", "濂?, "琛?, "娌￠棶棰?, "鏄?, "瀵?, "鍡?, "璐熻矗浜?, "鍚槑鐧?, "鐭ラ亾")
_FLOW_HINTS = {
    "IDENTITY_CONFIRM": "褰撳墠鍙仛韬唤纭銆傜敤鎴疯鍙互/鏂逛究/鏄?瀵?濂?鍡?璐熻矗浜烘椂锛宨ntent=confirm锛宺eply 鐢ㄤ竴鍙ョ畝鐭‘璁わ紱涓嶈鍐嶆澶嶈堪椹冲洖闂銆?,
    "CONFIRM_ACTION": "鐢ㄦ埛璇村彲浠?濂?瀵规椂锛宨ntent=confirm锛涗笉瑕侀噸澶嶄笂涓€鍙ャ€?,
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
                 case_context: dict[str, Any] | None = None,
                 stream_mode: str | None = None) -> None:
        self.router = router
        self.knowledge = KnowledgeBase(knowledge_path)
        self.flow_pack = flow_pack
        self.case_context = dict(case_context or {})
        self.sessions: dict[str, list[dict[str, str]]] = {}  # session_id -> conversation history
        self.flow_engines: dict[str, Any] = {}  # session_id -> FlowEngine instance
        self.max_history_turns = max_history_turns
        # buffered: parse the complete JSON before speaking (safe default).
        # reply: emit only the natural-language `reply` field from the stream.
        self.stream_mode = (stream_mode or os.getenv("LLM_STREAM_MODE", "buffered")).strip().lower()
        if self.stream_mode not in {"buffered", "reply"}:
            raise ValueError("LLM_STREAM_MODE must be buffered or reply")

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
            context += f"\n\n褰撳墠娴佺▼鐘舵€侊細{flow_state}"

        if slots:
            context += f"\n宸茬煡淇℃伅锛歿json.dumps(slots, ensure_ascii=False)}"

        # Real contest context: without these fields the local LLM cannot know
        # which merchant/rejection is being discussed.
        if self.case_context:
            context += "\n鏈崟涓氬姟涓婁笅鏂囷細" + json.dumps(self.case_context, ensure_ascii=False)

        if self.flow_pack:
            states = ", ".join(self.flow_pack.states.keys())
            context += f"\n\n涓氬姟娴佺▼鐘舵€侀泦鍚堬細{states}锛涘叆鍙ｏ細{self.flow_pack.entry}銆?

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

        # A short "鍙互/鏂逛究/濂? is a state transition, not a knowledge query.
        # Retrieving top-2 docs for it injects unrelated POP documents and makes
        # small models repeat the opening question.
        affirmative = flow_state in {"IDENTITY_CONFIRM", "CONFIRM_ACTION"} and _is_affirmative(text)
        chunks = [] if affirmative else self.knowledge.search(text, top_k=2)
        context = "\n".join(f"[{c.doc_id}] {c.title}: {c.content}" for c in chunks)
        user_prompt = text if not context else f"鐢ㄦ埛璇达細{text}\n鍙兘鐩稿叧鐨勭煡璇嗭細\n{context}"

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
        reply_extractor = ReplyExtractor() if self.stream_mode == "reply" else None
        streamed_reply = ""

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
                       "text": "鎶辨瓑锛屾垜杩欒竟鏆傛椂娌″惉娓咃紝鎮ㄥ啀璇翠竴閬嶅ソ鍚楋紵", "final": True}
                yield {"kind": "end", "reply_id": reply_id, "outcome": "llm_failed",
                       "slots": {}, "result": {"error": chunk.error}}
                return

            if chunk.type == "first_token":
                llm_provider = chunk.provider
                llm_model = chunk.model
                continue

            if chunk.type == "token":
                # Reply mode extracts only the JSON `reply` value from tokens;
                # raw JSON is never passed to TTS. Buffered mode keeps the
                # existing complete-JSON validation path.
                if reply_extractor is not None:
                    value = reply_extractor.feed(chunk.text)
                    if value and not first_sentence_sent:
                        streamed_reply = value
                        yield {"kind": "sentence", "reply_id": reply_id,
                               "sentence_id": sentence_id, "text": value, "final": False}
                        first_sentence_sent = True
                        sentence_id += 1
                continue

            if chunk.type == "sentence":
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
                "reply": accumulated_text or "鎴戞潵甯偍鏌ヨ涓€涓嬨€?,
                "tool": tool_name,
                "tool_args": tool_args,
                "outcome": "tool_called"
            }
        else:
            # JSON mode fallback (for LLMs without Function Calling)
            decision = parse_json_object(accumulated_text) or {}

        # Small local models often label "鍙互" as unclear even when the flow
        # state is explicitly waiting for confirmation. Keep the state machine
        # authoritative and prevent the same opening question from repeating.
        if affirmative:
            decision["intent"] = "confirm"
            current_reply = str(decision.get("reply", ""))
            ctx = self.case_context
            if flow_state == "IDENTITY_CONFIRM" and ctx.get("request_id") and ctx.get("reject_l3"):
                # Use the real case instead of a context-free "濂界殑锛屾劅璋㈢‘璁?.
                decision["reply"] = (
                    f"濂界殑锛寋ctx.get('brand', '鎮?)}鐨勭敵璇峰崟{ctx['request_id']}锛?
                    f"椹冲洖鍦▄ctx.get('reject_l1', '')}/{ctx.get('reject_l2', '')}锛?
                    f"鍘熷洜鏄瘂ctx['reject_l3']}銆傞渶瑕佹寜椹冲洖鎰忚琛ュ厖鎴栦慨姝ｆ潗鏂欏悗閲嶆柊鎻愪氦銆?
                )
            elif not current_reply or "鎯宠窡鎮ㄧ‘璁? in current_reply or "鏂逛究鐜板湪璇存槑" in current_reply:
                decision["reply"] = "濂界殑锛屾劅璋㈢‘璁ゃ€?
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
        # pack's generic "鍏蜂綋瀹℃牳椤? sentence.
        if flow_result and flow_result.get("say") and not self.case_context:
            reply = flow_result["say"]
        else:
            reply = str(decision.get("reply") or (flow_result or {}).get("say") or "濂界殑锛屾垜鏄庣櫧浜嗐€?)

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
                summary = str(data.get("message") or data.get("ticket_id") or "宸茬粡涓烘偍澶勭悊濂戒簡銆?)
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                       "text": summary, "final": True}
                sentence_id += 1
            else:
                yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": sentence_id,
                       "text": "鎶辨瓑锛屼笟鍔＄郴缁熸殏鏃舵病鏈夊搷搴旓紝鎴戝厛甯偍鐧昏锛岀◢鍚庝汉宸ヨ窡杩涖€?,
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
