from __future__ import annotations

import asyncio
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


class Connection(Protocol):
    async def send_json(self, data: dict[str, Any]) -> None: ...


def now_ms() -> float:
    return time.perf_counter() * 1000.0


@dataclass
class TurnRuntime:
    turn_id: str
    trace_id: str
    reply_id: str
    cancel: CancelToken = field(default_factory=CancelToken)
    t: dict[str, float] = field(default_factory=dict)
    t9: float | None = None
    playback: asyncio.Event = field(default_factory=asyncio.Event)
    tts_cursor_ms: float = 0.0
    tts_started_sent: bool = False
    char_cursor: int = 0
    barge_in_stop_ms: float | None = None


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
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    ws: Connection | None = None
    transport: str = "ws_pcm"
    started_mono_ms: float = field(default_factory=now_ms)
    audio_frames: int = 0

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
    ) -> None:
        self.bus = bus
        self.agent = agent
        self.tools = tools
        self.compliance = compliance
        self.release_id = release_id
        self.scenario_id = scenario_id
        self.sessions: dict[str, SessionState] = {}

    # ---- session ----
    async def create_session(self, mode: str = "voice") -> SessionState:
        sid = make_session_id()
        session = SessionState(
            session_id=sid,
            token="dev-token",
            mode=mode,
            release_id=self.release_id,
            version_set={
                "release_id": self.release_id,
                "flow": "service_outbound@1.0.0",
                "prompt": "agent_system@1",
                "knowledge": "faq@dev",
                "model": "fake@dev",
                "config": "runtime@1",
            },
        )
        self.sessions[sid] = session
        self.bus.register_session(sid, mode)
        await self.bus.emit(
            session_id=sid, type="SessionStarted", module="orchestrator", mode=mode,
            payload={"mode": mode, "scenario_id": self.scenario_id, "client_version": "dev"},
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
            "tts_alignment": False,
            "timeouts": {
                "listening_silence_ms": 10000,
                "session_max_ms": 600000,
                "wrapup_ms": 3000,
            },
        })
        await self._send_state(session)

    async def end_session(self, session_id: str, reason: str = "user_hangup") -> None:
        session = self.get_session(session_id)
        if session.current_turn:
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
            if turn and (not msg.get("turn_id") or msg["turn_id"] == turn.turn_id):
                turn.t9 = now_ms()
                turn.playback.set()
                await self.bus.emit(
                    session_id=session_id, type="PlaybackFirstFrame", module="client",
                    turn_id=turn.turn_id, payload={
                        "client_mono_ms": msg.get("client_mono_ms"),
                        "audio_ctx_time": msg.get("audio_ctx_time"),
                        "turn_id": turn.turn_id,
                        "reply_id": turn.reply_id,
                    },
                )
        elif kind == "playback_ended":
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
        turn = session.current_turn
        if not turn:
            return False
        if turn.cancel.cancelled():
            return False
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
        await self.bus.emit(
            session_id=session_id, type="CancelAcked", module="orchestrator",
            turn_id=turn.turn_id, latency_ms=round(now_ms() - start, 2),
            payload={"target": "agent,tts"},
        )
        return True

    # ---- text turn ----
    async def handle_text(self, session_id: str, text: str, conn: Connection | None = None) -> dict[str, Any]:
        session = self.get_session(session_id)
        if conn is not None:
            session.ws = conn

        if session.current_turn:
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
            turn.t["t1"] = turn.t["t0"] + 1.0
            session.current_turn = turn
            session.done.clear()
            session.turn_state = "THINKING"
            await self._send_state(session)
            await self.bus.emit(
                session_id=session_id, type="TurnStarted", module="orchestrator",
                turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"trigger": "user"},
            )
            await self.bus.emit(
                session_id=session_id, type="Transcript", module="orchestrator",
                turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"text": text, "final": True},
            )
            if conn:
                await conn.send_json({"type": "transcript", "turn_id": turn.turn_id, "text": text, "final": True})
            turn.t["t2"] = now_ms()
            await self.bus.emit(
                session_id=session_id, type="TurnCommitted", module="orchestrator",
                turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"phase": "input", "text": text},
            )

            sentence_no = 0
            sentences: list[str] = []
            result: dict[str, Any] | None = None
            tts_done = False
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
                    "dialog_state": {"flow_state": session.flow_state, "slots": session.slots},
                    "recent_turns": [],
                }
                async for chunk in self.agent.handle(req, turn.cancel, self.tools):
                    turn.cancel.check()
                    kind = chunk.get("kind")
                    if kind == "sentence":
                        sentence_no += 1
                        if "t4" not in turn.t:
                            turn.t["t4"] = now_ms()
                            await self.bus.emit(
                                session_id=session_id, type="AgentFirstToken", module="agent",
                                turn_id=turn.turn_id, trace_id=turn.trace_id,
                                latency_ms=round(turn.t["t4"] - turn.t["t3"], 2), payload={},
                            )
                        text_out = str(chunk.get("text", ""))
                        original_text = text_out
                        if "t5" not in turn.t:
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
                            await self._speak(session, turn, text_out, sentence_no, True)
                            sentences.append(text_out)
                            tts_done = True
                            break

                        final = bool(chunk.get("final"))
                        await self.bus.emit(
                            session_id=session_id, type="AgentSentence", module="agent",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            payload={"reply_id": turn.reply_id, "sentence_id": sentence_no,
                                     "text": text_out, "final": final},
                        )
                        await self._speak(session, turn, text_out, sentence_no, final)
                        sentences.append(text_out)
                    elif kind == "transfer":
                        result = {"outcome": "transferred", "reason": chunk.get("reason"), "slots": session.slots}
                        await self.bus.emit(
                            session_id=session_id, type="ComplianceTransfer", module="compliance",
                            turn_id=turn.turn_id, trace_id=turn.trace_id,
                            payload={"reason": chunk.get("reason"), "queue": chunk.get("queue")},
                        )
                        if conn:
                            await conn.send_json({"type": "agent_text", "turn_id": turn.turn_id,
                                                  "reply_id": turn.reply_id, "sentence_id": sentence_no + 1,
                                                  "text": "好的，正在为您转接人工。", "final": True})
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

                if not tts_done:
                    await self.bus.emit(
                        session_id=session_id, type="TTSCompleted", module="voice.tts",
                        turn_id=turn.turn_id, trace_id=turn.trace_id, payload={"chunks": len(sentences)},
                    )
                    if session.ws and turn.tts_started_sent:
                        await session.ws.send_json({
                            "type": "tts_completed",
                            "turn_id": turn.turn_id,
                            "reply_id": turn.reply_id,
                            "total_ms": round(turn.tts_cursor_ms, 2),
                            "interrupted": False,
                            "estimated": True,
                        })
                if result is None:
                    result = {"outcome": "completed", "slots": session.slots}

                if turn.t9 is None:
                    if conn is not None:
                        try:
                            await asyncio.wait_for(turn.playback.wait(), timeout=0.8)
                        except asyncio.TimeoutError:
                            turn.t9 = turn.t.get("t8", now_ms()) + 30.0
                    else:
                        turn.t9 = turn.t.get("t8", now_ms()) + 30.0
                turn.t["t9"] = turn.t9

                session.flow_state = "CLOSE" if result.get("outcome") in {"rescheduled", "completed"} else session.flow_state
                session.slots.update(result.get("slots") or {})
                await self.bus.emit(
                    session_id=session_id, type="TurnCommitted", module="orchestrator",
                    turn_id=turn.turn_id, trace_id=turn.trace_id,
                    payload={"phase": "result", "result": result},
                )
                session.turn_state = "LISTENING"
                await self._send_state(session)
                await self._send_turn_end(session, turn, result)
                await self._emit_turn_latency(session, turn)
                return {
                    "turn_id": turn.turn_id,
                    "reply_id": turn.reply_id,
                    "sentences": sentences,
                    "result": result,
                    "latency_ms": round(turn.t["t9"] - turn.t["t0"], 2),
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
                        "estimated": True,
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
                await self.bus.emit(
                    session_id=session_id, type="AgentError", module="agent",
                    turn_id=turn.turn_id, trace_id=turn.trace_id, ok=False,
                    error={"code": "INTERNAL", "message": str(exc), "retryable": False},
                    payload={"reply_id": turn.reply_id},
                )
                session.turn_state = "LISTENING"
                await self._send_state(session)
                return {"turn_id": turn.turn_id, "error": str(exc)}
            finally:
                session.current_turn = None
                session.done.set()

    async def _speak(self, session: SessionState, turn: TurnRuntime, text: str,
                     sentence_no: int, final: bool) -> None:
        if "t7" not in turn.t:
            await self.bus.emit(
                session_id=session.session_id, type="TTSStarted", module="voice.tts",
                turn_id=turn.turn_id, trace_id=turn.trace_id,
                payload={"reply_id": turn.reply_id, "sentence_id": sentence_no,
                         "text_hash": str(hash(text)), "tts_alignment": False,
                         "estimated": True},
            )
            await self.bus.emit(
                session_id=session.session_id, type="TTSFirstChunk", module="voice.tts",
                turn_id=turn.turn_id, trace_id=turn.trace_id,
                latency_ms=round(now_ms() - turn.t.get("t6", now_ms()), 2),
                payload={"reply_id": turn.reply_id, "sentence_id": sentence_no, "bytes": 0, "fake": True},
            )
            turn.t["t7"] = now_ms()
        session.turn_state = "SPEAKING"
        start_ms = turn.tts_cursor_ms
        duration_ms = max(600.0, len(text) * 180.0)
        end_ms = start_ms + duration_ms
        if session.ws:
            if not turn.tts_started_sent:
                await session.ws.send_json({
                    "type": "tts_started",
                    "turn_id": turn.turn_id,
                    "reply_id": turn.reply_id,
                    "sample_rate": 24000,
                    "channels": 1,
                    "tts_alignment": False,
                    "estimated": True,
                    "cursor": None,
                    "sentences": [],
                })
                turn.tts_started_sent = True
            await session.ws.send_json({
                "type": "tts_sentence",
                "turn_id": turn.turn_id,
                "reply_id": turn.reply_id,
                "sentence_id": sentence_no,
                "text": text,
                "start_ms": round(start_ms, 2),
                "end_ms": round(end_ms, 2),
                "char_start": turn.char_cursor,
                "char_end": turn.char_cursor + len(text),
                "is_last": final,
                "estimated": True,
            })
        turn.tts_cursor_ms = end_ms + 120.0
        turn.char_cursor += len(text)
        if session.ws:
            await session.ws.send_json({
                "type": "agent_text", "turn_id": turn.turn_id, "reply_id": turn.reply_id,
                "sentence_id": sentence_no, "text": text, "final": final,
            })
        turn.t["t8"] = now_ms()
        await self._send_state(session)

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
        t9 = t.get("t9", t.get("t8", now_ms()))
        e2e = t9 - t.get("t0", t9)
        official = t9 - t.get("t1", t.get("t0", t9))
        await self.bus.emit(
            session_id=session.session_id, type="TurnLatency", module="orchestrator",
            turn_id=turn.turn_id, trace_id=turn.trace_id,
            payload={
                "t0_last_voiced_ms": t.get("t0"), "t1_endpoint_ms": t.get("t1"),
                "t2_asr_final_ms": t.get("t2"), "t3_agent_start_ms": t.get("t3"),
                "t4_first_token_ms": t.get("t4"), "t5_first_sentence_ms": t.get("t5"),
                "t6_compliance_ms": t.get("t6"), "t7_tts_first_chunk_ms": t.get("t7"),
                "t8_first_downlink_ms": t.get("t8"), "t9_first_playback_ms": t9,
                "e2e_ms": round(e2e, 2), "e2e_official_proxy_ms": round(official, 2),
                "vad_to_playback_ms": round(official, 2), "clock_offset_ms": 0.0,
                "clock_suspect": False, "barge_in_stop_ms": turn.barge_in_stop_ms,
                "t0_client_ms": None, "t0_vad_ms": t.get("t0"),
            },
        )
