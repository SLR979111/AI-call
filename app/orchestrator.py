from __future__ import annotations

import asyncio
import base64
import os
from contextlib import aclosing
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from .agent.fake import FakeAgent
from .cancel import CancelToken, TurnCancelled
from .compliance import BLOCK, REWRITE, TRANSFER, ComplianceGuard
from .events import EventBus
from .ids import reply_id as make_reply_id
from .ids import session_id as make_session_id
from .ids import trace_id as make_trace_id
from .ids import turn_id as make_turn_id
from .tools.gateway import ToolGateway
from .voice.factory import create_tts, providers
from .voice.types import VoiceError, QWEN_MAX_TEXT_CHARS
from .voice.streaming import cancellable


class Connection(Protocol):
    async def send_json(self, data: dict[str, Any]) -> None: ...


def now_ms() -> float:
    return time.perf_counter() * 1000.0


TURN_ABORT_REASONS = {
    "BARGE_IN", "TIMEOUT", "SESSION_END", "COMPLIANCE_BLOCK",
    "USER_HANGUP", "INTERNAL",
}


def normalize_abort_reason(reason: str) -> str:
    return reason if reason in TURN_ABORT_REASONS else "INTERNAL"


@dataclass
class TurnRuntime:
    turn_id: str
    trace_id: str
    reply_id: str
    cancel: CancelToken = field(default_factory=CancelToken)
    t: dict[str, float] = field(default_factory=dict)
    t9: float | None = None
    t9_formal: float | None = None
    playback: asyncio.Event = field(default_factory=asyncio.Event)
    playback_formal: asyncio.Event = field(default_factory=asyncio.Event)
    tts_cursor_ms: float = 0.0
    tts_started_sent: bool = False
    audio_seq: int = 0
    pending_speech: list = field(default_factory=list)
    char_cursor: int = 0
    barge_in_stop_ms: float | None = None
    played_ms: float | None = None


@dataclass
class SessionState:
    session_id: str
    token: str
    mode: str
    release_id: str
    version_set: dict[str, str]
    turn_seq: int = 0
    turn_state: str = "IDLE"
    flow_state: str = "OPEN"
    slots: dict[str, Any] = field(default_factory=dict)
    current_turn: TurnRuntime | None = None
    playing_turn: TurnRuntime | None = None
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    ws: Connection | None = None
    transport: str = "ws_pcm"
    started_mono_ms: float = field(default_factory=now_ms)
    audio_frames: int = 0
    outbound_started: bool = False
    case_id: str | None = None

    def __post_init__(self) -> None:
        self.done.set()


class Orchestrator:
    def __init__(
        self,
        bus: EventBus,
        agent: FakeAgent,
        tools: ToolGateway,
        compliance: ComplianceGuard,
        release_id: str = "rel_dev_001",
        scenario_id: str = "service_outbound",
        tts=None,
        *,
        tts_provider: str | None = None,
    ) -> None:
        self.bus = bus
        self.agent = agent
        self.tools = tools
        self.compliance = compliance
        self.release_id = release_id
        self.scenario_id = scenario_id
        self.sessions: dict[str, SessionState] = {}
        self.tts = tts if tts is not None else create_tts()
        self.tts_provider = tts_provider or providers()[1]
        self.tts_group_size = max(1, min(3, int(os.getenv("QWEN_GROUP_SENTENCES", "3")))) if self.tts_provider == "qwen3" else 1
        self.tts_alignment = self.tts_provider != "fake" and self.tts_group_size == 1

    # ---- session ----
    async def create_session(self, mode: str = "voice", *, case_id: str | None = None) -> SessionState:
        sid = make_session_id()
        session = SessionState(
            session_id=sid,
            token="dev-token",
            mode=mode,
            case_id=case_id,
            release_id=self.release_id,
            version_set={
                "release_id": self.release_id,
                "flow": f"{self.scenario_id}@1.0.0",
                "prompt": "agent_system@1",
                "knowledge": "pop_audit@dev" if self.scenario_id == "pop_qualification_audit" else "faq@dev",
                "model": "fake@dev",
                "config": "runtime@1",
            },
        )
        self.sessions[sid] = session
        self.bus.register_session(sid, mode)
        # LLMAgent needs an explicit session hook to create its FlowEngine/history.
        start_session = getattr(self.agent, "start_session", None)
        flow_context = {"id": self.scenario_id}
        if case_id is not None:
            flow_context["case_id"] = case_id
        if start_session is not None:
            await start_session(sid, session.version_set, flow_context)
        await self.bus.emit(
            session_id=sid, type="SessionStarted", module="orchestrator", mode=mode,
            payload={"mode": mode, "scenario_id": self.scenario_id, "client_version": "dev",
                     **({"case_id": case_id} if case_id is not None else {})},
        )
        await self.bus.emit(
            session_id=sid, type="VersionPinned", module="registry", mode=mode,
            payload={"release_id": self.release_id, "artifacts": session.version_set},
        )
        return session

    def get_session(self, session_id: str) -> SessionState:
        if session_id not in self.sessions:
            raise KeyError(f"session not found: {session_id}")
        return self.sessions[session_id]

    async def attach(self, session_id: str, ws: Connection) -> None:
        session = self.get_session(session_id)
        session.ws = ws
        await ws.send_json({
            "type": "session_ready",
            "session_id": session_id,
            "version_set": session.version_set,
            "ice_servers": [],
            "protocol_version": "0.2",
            "server_version": "0.2.0",
            "transport": session.transport,
            "tts_alignment": self.tts_alignment,
            "opens_outbound": session.mode == "voice" and bool(getattr(self.agent, "opens_outbound", False)),
            "timeouts": {
                "listening_silence_ms": 10000,
                "session_max_ms": 600000,
                "wrapup_ms": 3000,
            },
        })
        await self._send_state(session)

    async def end_session(self, session_id: str, reason: str = "user_hangup") -> None:
        session = self.get_session(session_id)
        if session.current_turn or session.playing_turn:
            await self.cancel_current(session_id, "SESSION_END")
            try:
                await asyncio.wait_for(session.done.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        await self.bus.emit(
            session_id=session_id, type="SessionEnded", module="orchestrator",
            payload={"reason": reason, "turns": session.turn_seq},
        )
        session.turn_state = "ENDED"
        await self._send_state(session)
        if session.ws:
            await session.ws.send_json({
                "type": "session_ended",
                "reason": reason,
                "duration_ms": round(now_ms() - session.started_mono_ms, 2),
                "turns": session.turn_seq,
            })

    # ---- client events ----
    async def on_client_event(self, session_id: str, msg: dict[str, Any]) -> None:
        session = self.get_session(session_id)
        kind = msg.get("type")
        if kind == "speech_started":
            await self.bus.emit(
                session_id=session_id, type="ClientSpeechStarted", module="client",
                payload={k: msg.get(k) for k in ("client_ts_ms", "client_mono_ms", "energy_db", "source")},
            )
            await self.cancel_current(session_id, "BARGE_IN")
        elif kind == "local_muted":
            turn = session.current_turn
            if turn:
                stop_latency = msg.get("stop_latency_ms")
                if isinstance(stop_latency, (int, float)):
                    turn.barge_in_stop_ms = float(stop_latency)
                if isinstance(msg.get("played_ms"), (int, float)):
                    turn.played_ms = float(msg["played_ms"])
            await self.bus.emit(
                session_id=session_id, type="ClientLocalMuted", module="client",
                turn_id=msg.get("turn_id"),
                payload={k: msg.get(k) for k in (
                    "client_mono_ms", "reason", "played_ms", "sentence_id",
                    "char_idx", "last_frame_played_mono_ms", "stop_latency_ms",
                    "b0_mono_ms",
                )},
            )
            if turn and isinstance(msg.get("stop_latency_ms"), (int, float)):
                await self.bus.emit(
                    session_id=session_id, type="BargeInConfirmed", module="client",
                    turn_id=turn.turn_id, latency_ms=float(msg["stop_latency_ms"]),
                    payload={"stop_latency_ms": float(msg["stop_latency_ms"]),
                             "played_ms": msg.get("played_ms"),
                             "sentence_id": msg.get("sentence_id"),
                             "char_idx": msg.get("char_idx"),
                             "b3_mono_ms": msg.get("last_frame_played_mono_ms")},
                )
        elif kind == "playback_first_frame":
            turn = session.current_turn
            formal = bool(msg.get("formal"))
            if (turn and not turn.cancel.cancelled()
                    and msg.get("turn_id") == turn.turn_id
                    and msg.get("reply_id") == turn.reply_id):
                if formal:
                    if turn.t9_formal is None:
                        turn.t9_formal = now_ms()
                        turn.playback_formal.set()
                elif turn.t9 is None:
                    turn.t9 = now_ms()
                    turn.playback.set()
                await self.bus.emit(
                    session_id=session_id, type="PlaybackFirstFrame", module="client",
                    turn_id=turn.turn_id, payload={
                        "client_mono_ms": msg.get("client_mono_ms"),
                        "audio_ctx_time": msg.get("audio_ctx_time"),
                        "turn_id": turn.turn_id,
                        "reply_id": turn.reply_id,
                        "formal": formal,
                    },
                )
        elif kind == "playback_ended":
            if session.playing_turn and msg.get("reply_id") == session.playing_turn.reply_id:
                session.playing_turn = None
            await self.bus.emit(
                session_id=session_id, type="PlaybackEnded", module="client",
                turn_id=msg.get("turn_id"),
                payload={"client_mono_ms": msg.get("client_mono_ms"),
                         "played_ms": msg.get("played_ms")},
            )
        elif kind == "ping":
            if session.ws:
                await session.ws.send_json({
                    "type": "pong",
                    "t": msg.get("t"),
                    "client_mono_ms": msg.get("client_mono_ms"),
                    "server_rx_mono_ms": now_ms(),
                    "server_tx_mono_ms": now_ms(),
                })

    async def cancel_current(self, session_id: str, reason: str = "BARGE_IN") -> bool:
        session = self.get_session(session_id)
        turn = session.current_turn or session.playing_turn
        if not turn:
            return False
        if turn.cancel.cancelled():
            return False
        reason = normalize_abort_reason(reason)
        start = now_ms()
        turn.cancel.cancel(reason)
        await self.bus.emit(
            session_id=session_id, type="CancelRequested", module="orchestrator",
            turn_id=turn.turn_id, payload={"reason": reason, "target": "agent,tts"},
        )
        if reason == "BARGE_IN":
            await self.bus.emit(
                session_id=session_id, type="BargeInDetected", module="orchestrator",
                turn_id=turn.turn_id, payload={"during_turn_id": turn.turn_id, "at_ms": start},
            )
        if session.ws:
            await session.ws.send_json({"type": "cancel", "turn_id": turn.turn_id, "reason": reason})
            if session.current_turn is not turn and turn.tts_started_sent:
                await session.ws.send_json({"type": "tts_completed", "turn_id": turn.turn_id,
                    "reply_id": turn.reply_id, "total_ms": round(turn.tts_cursor_ms, 2),
                    "interrupted": True, "estimated": False})
        if session.playing_turn is turn:
            session.playing_turn = None
        await self.bus.emit(
            session_id=session_id, type="CancelAcked", module="orchestrator",
            turn_id=turn.turn_id, latency_ms=round(now_ms() - start, 2),
            payload={"target": "agent,tts"},
        )
        return True

    # ---- text turn ----
    async def open_outbound(self, session_id: str, conn: Connection) -> None:
        session = self.get_session(session_id)
        if (session.mode != "voice" or not getattr(self.agent, "opens_outbound", False)
                or session.outbound_started or session.turn_seq):
            return
        session.outbound_started = True
        await self.handle_text(session_id, "", conn, outbound_opening=True)

    async def handle_text(self, session_id: str, text: str, conn: Connection | None = None,
                          *, transcript_metadata: dict[str, Any] | None = None,
                          outbound_opening: bool = False) -> dict[str, Any]:
        session = self.get_session(session_id)
        if conn is not None:
            session.ws = conn

        if session.current_turn or session.playing_turn:
            await self.cancel_current(session_id, "BARGE_IN")
            try:
                await asyncio.wait_for(session.done.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        async with session.turn_lock:
            session.turn_seq += 1
            turn = TurnRuntime(
                turn_id=make_turn_id(session.turn_seq),
                trace_id=make_trace_id(),
                reply_id=make_reply_id(make_turn_id(session.turn_seq)),
            )
            turn.t["t0"] = now_ms()
            if not outbound_opening:
                turn.t["t1"] = turn.t["t0"] + 1.0
            session.current_turn = turn
            session.done.clear()
            session.turn_state = "THINKING"
            await self._send_state(session)
            await self.bus.emit(
                session_id=session_id, type="TurnStarted", module="orchestrator",
                turn_id=turn.turn_id, trace_id=turn.trace_id,
                payload={"trigger": "outbound_opening" if outbound_opening else "user"},
            )
            if not outbound_opening:
                await self.bus.emit(
                    session_id=session_id, type="Transcript", module="orchestrator",
                    turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"text": text, "final": True},
                )
                if conn:
                    await conn.send_json({**(transcript_metadata or {}), "type": "transcript",
                                          "turn_id": turn.turn_id, "text": text, "final": True})
                turn.t["t2"] = now_ms()
                await self.bus.emit(
                    session_id=session_id, type="TurnCommitted", module="orchestrator",
                    turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"phase": "input", "text": text},
                )

            sentence_no = 0
            sentences: list[str] = []
            result: dict[str, Any] | None = None
            try:
                turn.t["t3"] = now_ms()
                await self.bus.emit(
                    session_id=session_id, type="AgentStarted", module="agent",
                    turn_id=turn.turn_id, trace_id=turn.trace_id,
                    payload={"user_text": text, "dialog_state_hash": "dev"},
                )
                req = {
                    "session_id": session_id,
                    "turn_id": turn.turn_id,
                    "trace_id": turn.trace_id,
                    "reply_id": turn.reply_id,
                    "user_text": text,
                    "asr_conf": 1.0,
                    "flow_state": session.flow_state,
                    "slots": dict(session.slots),
                    "dialog_state": {"flow_state": session.flow_state, "slots": session.slots},
                    "recent_turns": [],
                }
                async for chunk in self.agent.handle(req, turn.cancel, self.tools):
                    turn.cancel.check()
                    kind = chunk.get("kind")
                    if kind == "stage":
                        stage = chunk["stage"]
                        session.flow_state = stage
                        await self.bus.emit(
                            session_id=session_id, type="BusinessStageReached", module="orchestrator",
                            turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"stage": stage},
                        )
                        await self._send_state(session)
                    if kind == "first_token":
                        if "t4" not in turn.t:
                            turn.t["t4"] = turn.t.get("t3", now_ms()) + float(
                                chunk.get("latency_ms") or 0
                            )
                            await self.bus.emit(
                                session_id=session_id, type="AgentFirstToken", module="agent",
                                turn_id=turn.turn_id, trace_id=turn.trace_id,
                                latency_ms=round(turn.t["t4"] - turn.t["t3"], 2), payload={},
                            )
                    elif kind == "sentence":
                        sentence_no += 1
                        is_filler = bool(chunk.get("filler"))
                        if not is_filler and "t4" not in turn.t:
                            turn.t["t4"] = now_ms()
                            await self.bus.emit(
                                session_id=session_id, type="AgentFirstToken", module="agent",
                                turn_id=turn.turn_id, trace_id=turn.trace_id,
                                latency_ms=round(turn.t["t4"] - turn.t["t3"], 2), payload={},
                            )
                        text_out = str(chunk.get("text", ""))
                        original_text = text_out
                        if not is_filler and "t5" not in turn.t:
                            turn.t["t5"] = now_ms()
                        guard = await self.compliance.check_sentence(text_out, {"turn_id": turn.turn_id})
                        await self.bus.emit(
                            session_id=session_id, type="ComplianceChecked", module="compliance",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            latency_ms=round(guard.latency_ms, 2),
                            payload={"scope": "sentence", "decision": guard.decision,
                                     "rule_ids": guard.rule_ids, "text": text_out},
                        )
                        if session.ws:
                            await session.ws.send_json({
                                "type": "compliance",
                                "turn_id": turn.turn_id,
                                "scope": "sentence",
                                "decision": guard.decision,
                                "rule_ids": guard.rule_ids,
                                "before": original_text,
                                "after": guard.text,
                                "severity": guard.severity,
                            })
                        if "t6" not in turn.t:
                            turn.t["t6"] = now_ms()
                        if guard.decision == REWRITE:
                            await self.bus.emit(
                                session_id=session_id, type="ComplianceRewritten", module="compliance",
                                turn_id=turn.turn_id, trace_id=turn.trace_id,
                                payload={"before": text_out, "after": guard.text, "rule_ids": guard.rule_ids},
                            )
                            text_out = guard.text
                        elif guard.decision == BLOCK:
                            await self.bus.emit(
                                session_id=session_id, type="ComplianceBlocked", module="compliance",
                                turn_id=turn.turn_id, trace_id=turn.trace_id, ok=False,
                                error={"code": "COMPLIANCE_BLOCK", "message": "forbidden content",
                                       "retryable": False},
                                payload={"text": text_out, "rule_ids": guard.rule_ids,
                                         "severity": guard.severity, "action_taken": "safe_fallback"},
                            )
                            text_out = guard.text
                            result = {"outcome": "blocked", "slots": session.slots}
                            await self._speak(session, turn, text_out, sentence_no, True, is_filler=False)
                            sentences.append(text_out)
                            break

                        final = bool(chunk.get("final"))
                        await self.bus.emit(
                            session_id=session_id, type="AgentSentence", module="agent",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            payload={"reply_id": turn.reply_id, "sentence_id": sentence_no,
                                     "text": text_out, "final": final, "filler": is_filler},
                        )
                        await self._speak(session, turn, text_out, sentence_no, final, is_filler=is_filler)
                        sentences.append(text_out)
                    elif kind == "transfer":
                        result = {"outcome": "transferred", "reason": chunk.get("reason"), "slots": session.slots}
                        await self.bus.emit(
                            session_id=session_id, type="ComplianceTransfer", module="compliance",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            payload={"reason": chunk.get("reason"), "queue": chunk.get("queue")},
                        )
                        transfer_text = "好的，正在为您转接人工。"
                        await self._speak(session, turn, transfer_text, sentence_no + 1, True, is_filler=False)
                        sentences.append(transfer_text)
                        break
                    elif kind == "end":
                        result = dict(chunk)
                        await self.bus.emit(
                            session_id=session_id, type="AgentCompleted", module="agent",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            payload={"reply_id": turn.reply_id, "outcome": result.get("outcome"),
                                     "result": result.get("result"), "latency_ms": round(now_ms() - turn.t["t3"], 2)},
                        )
                        break
                    elif kind == "error":
                        err = chunk.get("error") or {}
                        await self.bus.emit(
                            session_id=session_id, type="AgentError", module="agent",
                            turn_id=turn.turn_id, trace_id=turn.trace_id, ok=False,
                            error=err, payload={"reply_id": turn.reply_id},
                        )
                        result = {"outcome": "error", "error": err}
                        break

                await self._flush_speech(session, turn)
                turn.cancel.check()
                await self.bus.emit(
                    session_id=session_id, type="TTSCompleted", module="voice.tts",
                    turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"chunks": turn.audio_seq, "total_ms": turn.tts_cursor_ms},
                )
                if session.ws and turn.tts_started_sent:
                    await session.ws.send_json({
                        "type": "tts_completed",
                        "turn_id": turn.turn_id,
                        "reply_id": turn.reply_id,
                        "total_ms": round(turn.tts_cursor_ms, 2),
                        "interrupted": False,
                        "estimated": False,
                    })
                if result is None:
                    result = {"outcome": "completed", "slots": session.slots}

                if turn.t9 is None and conn is not None and turn.tts_started_sent:
                    try:
                        await cancellable(turn.playback.wait(), turn.cancel, timeout=0.8)
                    except asyncio.TimeoutError:
                        pass
                turn.cancel.check()
                # Only hermetic fake evaluations retain a labelled playback simulation.
                if turn.t9 is None and conn is None and self.tts_provider == "fake":
                    turn.t9 = turn.t.get("t8", now_ms()) + 30.0
                if turn.t9 is not None:
                    turn.t["t9"] = turn.t9

                session.flow_state = "CLOSE" if result.get("outcome") in {"rescheduled", "completed"} else session.flow_state
                session.slots.update(result.get("slots") or {})
                flow_snapshot = (result.get("result") or {}).get("flow_snapshot") or {}
                if flow_snapshot.get("state"):
                    session.flow_state = str(flow_snapshot["state"])
                if isinstance(flow_snapshot.get("slots"), dict):
                    session.slots.update(flow_snapshot["slots"])
                await self.bus.emit(
                    session_id=session_id, type="TurnCommitted", module="orchestrator",
                    turn_id=turn.turn_id, trace_id=turn.trace_id,
                    payload={"phase": "result", "result": result},
                )
                session.turn_state = "LISTENING"
                await self._send_state(session)
                await self._send_turn_end(session, turn, result)
                if outbound_opening:
                    await self.bus.emit(
                        session_id=session_id, type="OutboundOpeningLatency", module="orchestrator",
                        turn_id=turn.turn_id, trace_id=turn.trace_id,
                        payload={"ready_to_first_playback_ms": (
                            round(turn.t9 - turn.t["t0"], 2) if turn.t9 is not None else None
                        ), "playback_measured": turn.playback.is_set()},
                    )
                else:
                    await self._emit_turn_latency(session, turn)
                if result.get("outcome") in {"wrong_contact", "closing"}:
                    # Play the short closing/apology, then end the call instead
                    # of returning to LISTENING.
                    asyncio.create_task(self.end_session(session_id, str(result["outcome"])))
                return {
                    "turn_id": turn.turn_id,
                    "reply_id": turn.reply_id,
                    "sentences": sentences,
                    "result": result,
                    "latency_ms": round(turn.t9 - turn.t["t0"], 2) if turn.t9 is not None else None,
                }
            except TurnCancelled:
                await self.bus.emit(
                    session_id=session_id, type="AgentCancelled", module="agent",
                    turn_id=turn.turn_id, trace_id=turn.trace_id,
                    payload={"reply_id": turn.reply_id, "at_sentence": sentence_no},
                )
                await self.bus.emit(
                    session_id=session_id, type="TurnAborted", module="orchestrator",
                    turn_id=turn.turn_id, trace_id=turn.trace_id,
                    payload={"reason": turn.cancel.reason(), "last_state": session.turn_state},
                )
                if session.ws and turn.tts_started_sent:
                    await session.ws.send_json({
                        "type": "tts_completed",
                        "turn_id": turn.turn_id,
                        "reply_id": turn.reply_id,
                        "total_ms": round(turn.tts_cursor_ms, 2),
                        "interrupted": True,
                        "played_ms": turn.played_ms,
                        "estimated": False,
                    })
                if session.ws:
                    await session.ws.send_json({
                        "type": "turn_aborted",
                        "turn_id": turn.turn_id,
                        "reason": turn.cancel.reason(),
                    })
                session.turn_state = "LISTENING"
                await self._send_state(session)
                return {"turn_id": turn.turn_id, "aborted": True, "reason": turn.cancel.reason()}
            except Exception as exc:
                error = exc.to_dict() if isinstance(exc, VoiceError) else {
                    "code": "INTERNAL", "message": str(exc), "retryable": False}
                await self.bus.emit(
                    session_id=session_id, type="AgentError", module="voice.tts" if isinstance(exc, VoiceError) else "agent",
                    turn_id=turn.turn_id, trace_id=turn.trace_id, ok=False,
                    error=error, payload={"reply_id": turn.reply_id})
                if session.ws:
                    await session.ws.send_json({"type": "error", "turn_id": turn.turn_id,
                                                "reply_id": turn.reply_id, "error": error})
                    if turn.tts_started_sent:
                        await session.ws.send_json({"type": "tts_completed", "turn_id": turn.turn_id,
                            "reply_id": turn.reply_id, "total_ms": round(turn.tts_cursor_ms, 2),
                            "interrupted": True, "estimated": False})
                    await self._send_turn_end(session, turn, {"outcome": "error", "error": error})
                session.turn_state = "LISTENING"
                await self._send_state(session)
                return {"turn_id": turn.turn_id, "error": error}
            finally:
                if turn.cancel.cancelled() and session.playing_turn is turn:
                    session.playing_turn = None
                session.current_turn = None
                session.done.set()

    async def _speak(self, session: SessionState, turn: TurnRuntime, text: str,
                     sentence_no: int, final: bool, *, is_filler: bool = False) -> None:
        turn.cancel.check()
        if self.tts_provider == "qwen3":
            if len(text) > QWEN_MAX_TEXT_CHARS:
                raise VoiceError("TTS_INVALID_REQUEST", "Qwen sentence exceeds 2000 chars; split the Agent output into sentences")
            if sum(len(item[1]) for item in turn.pending_speech) + len(text) > QWEN_MAX_TEXT_CHARS:
                await self._flush_speech(session, turn)
        if session.ws:
            await session.ws.send_json({
                "type": "agent_text", "turn_id": turn.turn_id, "reply_id": turn.reply_id,
                "sentence_id": sentence_no, "text": text, "final": final,
                "filler": is_filler})
        if not turn.pending_speech:
            turn._group_filler = is_filler
        turn.pending_speech.append((sentence_no, text, final, turn.char_cursor))
        turn.char_cursor += len(text)
        # First sentence immediately; subsequent approved sentences grouped for Qwen prosody.
        if sentence_no == 1 or final or len(turn.pending_speech) >= self.tts_group_size:
            await self._flush_speech(session, turn)

    async def _flush_speech(self, session: SessionState, turn: TurnRuntime) -> None:
        if not turn.pending_speech:
            return
        group, turn.pending_speech = turn.pending_speech, []
        is_filler = bool(getattr(turn, "_group_filler", False))
        turn._group_filler = False
        turn.cancel.check()
        text = "".join(item[1] for item in group)
        start_ms = turn.tts_cursor_ms
        rate = self.tts.sample_rate
        session.turn_state = "SPEAKING"
        await self._send_state(session)
        samples = 0

        async def send_audio(pcm: bytes, is_last: bool, ts_mono_ms: float) -> None:
            data = {"type": "audio_chunk", "turn_id": turn.turn_id,
                    "reply_id": turn.reply_id, "sentence_id": group[0][0],
                    "sentence_ids": [item[0] for item in group], "seq": turn.audio_seq,
                    "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                    "sample_rate": rate, "channels": 1, "is_last": is_last,
                    "ts_mono_ms": ts_mono_ms}
            if hasattr(session.ws, "send_audio"):
                await session.ws.send_audio(data, turn.cancel)
            else:
                turn.cancel.check()
                await session.ws.send_json(data)
            if pcm:
                if not is_filler:
                    turn.t.setdefault("t8", now_ms())
                else:
                    turn.t.setdefault("t8_filler", now_ms())

        async def send_sentences(end_ms: float, *, provisional: bool) -> None:
            offset = start_ms
            for number, sentence, final, char_start in group:
                duration = (end_ms - start_ms) * len(sentence) / max(1, len(text))
                turn.cancel.check()
                if session.ws:
                    estimated = provisional or len(group) > 1
                    await session.ws.send_json({"type": "tts_sentence", "turn_id": turn.turn_id,
                        "reply_id": turn.reply_id, "sentence_id": number, "text": sentence,
                        "start_ms": round(offset, 2), "end_ms": round(offset + duration, 2),
                        "char_start": char_start, "char_end": char_start + len(sentence),
                        "is_last": final, "estimated": estimated, "tts_alignment": not estimated,
                        "provisional": provisional})
                offset += duration

        async with aclosing(self.tts.synthesize(text, sample_rate=rate, cancel=turn.cancel)) as stream:
            async for chunk in stream:
                turn.cancel.check()
                if chunk.sample_rate != rate or len(chunk.pcm) % 2:
                    raise VoiceError("TTS_FORMAT", "invalid PCM or changed sample rate")
                if not chunk.pcm:
                    # Normalize provider termination below, after successful
                    # exhaustion. This also covers an is_last on nonempty PCM.
                    continue
                if not is_filler and "t7" not in turn.t:
                    turn.t["t7"] = now_ms()
                    await self.bus.emit(
                        session_id=session.session_id, type="TTSStarted", module="voice.tts",
                        turn_id=turn.turn_id, trace_id=turn.trace_id,
                        payload={"reply_id": turn.reply_id, "sample_rate": rate,
                                 "tts_alignment": self.tts_alignment, "filler": False})
                    await self.bus.emit(
                        session_id=session.session_id, type="TTSFirstChunk", module="voice.tts",
                        turn_id=turn.turn_id, trace_id=turn.trace_id,
                        latency_ms=round(turn.t["t7"] - turn.t.get("t6", turn.t["t7"]), 2),
                        payload={"reply_id": turn.reply_id, "sentence_id": group[0][0],
                                 "bytes": len(chunk.pcm), "fake": self.tts_provider == "fake",
                                 "filler": False})
                if is_filler and "t7_filler" not in turn.t:
                    turn.t["t7_filler"] = now_ms()
                    await self.bus.emit(
                        session_id=session.session_id, type="TTSFirstChunk", module="voice.tts",
                        turn_id=turn.turn_id, trace_id=turn.trace_id,
                        latency_ms=round(turn.t["t7_filler"] - turn.t.get("t6", turn.t["t7_filler"]), 2),
                        payload={"reply_id": turn.reply_id, "sentence_id": group[0][0],
                                 "bytes": len(chunk.pcm), "fake": self.tts_provider == "fake",
                                 "filler": True})
                if session.ws:
                    if not turn.tts_started_sent:
                        await session.ws.send_json({"type": "tts_started", "turn_id": turn.turn_id,
                            "reply_id": turn.reply_id, "sample_rate": rate, "channels": 1,
                            "audio_format": "pcm16", "tts_alignment": self.tts_alignment,
                            "estimated": not self.tts_alignment, "sentences": [], "filler": is_filler})
                        turn.tts_started_sent = True
                        session.playing_turn = turn
                    if not samples:
                        # Existing clients upsert by sentence_id. Publish before
                        # PCM, then replace estimates with sample-based timing.
                        await send_sentences(start_ms + max(100, len(text) * 180), provisional=True)
                    turn.cancel.check()
                    await send_audio(chunk.pcm, False, chunk.ts_mono_ms)
                samples += len(chunk.pcm) // 2
                turn.audio_seq += 1
                turn.tts_cursor_ms = start_ms + samples * 1000 / rate
        if not samples:
            raise VoiceError("TTS_EMPTY", "provider returned no audio", True)
        turn.cancel.check()
        if session.ws:
            await send_audio(b"", True, now_ms())
            turn.audio_seq += 1
        # A single synthesis has exact boundaries. Internal boundaries of a Qwen
        # group are proportional estimates, explicitly labelled; total duration is exact.
        await send_sentences(turn.tts_cursor_ms, provisional=False)

    async def _send_state(self, session: SessionState) -> None:
        if session.ws:
            await session.ws.send_json({
                "type": "state",
                "turn_state": session.turn_state,
                "flow_state": session.flow_state,
                "slots": session.slots,
                "turn_id": session.current_turn.turn_id if session.current_turn else None,
            })

    async def _send_turn_end(self, session: SessionState, turn: TurnRuntime, result: dict[str, Any]) -> None:
        if session.ws:
            await session.ws.send_json({"type": "turn_end", "turn_id": turn.turn_id, "result": result})
            await session.ws.send_json({"type": "turn_result", "turn_id": turn.turn_id, "result": result})

    async def _emit_turn_latency(self, session: SessionState, turn: TurnRuntime) -> None:
        t = turn.t
        t9 = t.get("t9")
        t9_formal = turn.t9_formal
        e2e = t9 - t["t0"] if t9 is not None else None
        official = t9 - t["t1"] if t9 is not None else None
        e2e_formal = t9_formal - t["t0"] if t9_formal is not None else None
        formal_official = t9_formal - t["t1"] if t9_formal is not None else None
        await self.bus.emit(
            session_id=session.session_id, type="TurnLatency", module="orchestrator",
            turn_id=turn.turn_id, trace_id=turn.trace_id,
            payload={
                "t0_last_voiced_ms": t.get("t0"), "t1_endpoint_ms": t.get("t1"),
                "t2_asr_final_ms": t.get("t2"), "t3_agent_start_ms": t.get("t3"),
                "t4_first_token_ms": t.get("t4"), "t5_first_sentence_ms": t.get("t5"),
                "t6_compliance_ms": t.get("t6"), "t7_tts_first_chunk_ms": t.get("t7"),
                "t7_filler_ms": t.get("t7_filler"), "t8_filler_ms": t.get("t8_filler"),
                "t8_first_downlink_ms": t.get("t8"), "t9_first_playback_ms": t9,
                "t9_formal_ms": t9_formal,
                "e2e_ms": round(e2e, 2) if e2e is not None else None,
                "e2e_formal_ms": round(e2e_formal, 2) if e2e_formal is not None else None,
                "e2e_official_proxy_ms": round(official, 2) if official is not None else None,
                "e2e_formal_official_ms": round(formal_official, 2) if formal_official is not None else None,
                "vad_to_playback_ms": round(official, 2) if official is not None else None,
                "clock_offset_ms": 0.0,
                "playback_measured": turn.playback.is_set(),
                "playback_formal_measured": turn.playback_formal.is_set(),
                "clock_suspect": not turn.playback.is_set(), "barge_in_stop_ms": turn.barge_in_stop_ms,
                "t0_client_ms": None, "t0_vad_ms": t.get("t0"),
            },
        )
