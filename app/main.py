from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent.fake import FakeAgent
from .agent.pop_audit import PopAuditAgent
from .business.gateway import create_business_gateway
from .compliance import ComplianceGuard
from .config import _bool, settings
from .events import EventBus
from .eval.runner import EvalCase, EvalRunner
from .flow.pack import FlowPack, FlowPackError
from .llm.router import LLMRouter
from .media.livekit import create_livekit_token
from .media.livekit_worker import BridgeRegistry
from .orchestrator import Orchestrator, SessionState
from .registry import Registry, RegistryError
from .rsi.gate import GateConfig, compare as rsi_compare
from .tools.gateway import MockBusiness, ToolGateway
from .voice.factory import VoiceRuntime
from .voice.types import AudioFrame, VoiceError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CLIENT_EVENT_NAMES = {
    "PlaybackCancelled",
    "PlaybackUnderrun",
    "NetStats",
    "AecState",
    "ClockSync",
    "BargeInCancelledLocal",
    "VadMetrics",
}


class WsConnection:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()
        self.rtc_pcm = None

    async def send_json(self, data: dict[str, Any]) -> None:
        async with self.lock:
            if self.rtc_pcm:
                await self.rtc_pcm.send(data)
            else:
                await asyncio.wait_for(self.ws.send_json(data), 5)

    async def send_audio(self, data, cancel) -> None:
        async with self.lock:
            cancel.check()
            if self.rtc_pcm:
                await self.rtc_pcm.send(data, cancel)
            else:
                await asyncio.wait_for(self.ws.send_json(data), 5)


class CreateSessionRequest(BaseModel):
    mode: Literal["voice", "text"] = "text"
    scenario_id: str | None = None
    case_id: str | None = Field(default=None, min_length=1, max_length=64)


class TextTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class CancelRequest(BaseModel):
    turn_id: str | None = None
    reason: str = "USER_CANCEL"


class EvalRequest(BaseModel):
    turns: list[str] | None = None
    scenario_id: str | None = None
    release_id: str | None = None


class ToolInvokeRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    deadline_ms: int = 2000


class CandidateRequest(BaseModel):
    artifacts: dict[str, str]
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class PromoteRequest(BaseModel):
    release_id: str
    gate: dict[str, Any] | None = None


class LLMCompleteRequest(BaseModel):
    messages: list[dict[str, str]]
    temperature: float = 0.3
    max_tokens: int = 512
    session_id: str = "s_internal"
    turn_id: str = "t_0000"
    response_format: dict[str, Any] | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)


class FlowSimulateRequest(BaseModel):
    intents: list[str] = Field(default_factory=list)


class EvalRunRequest(BaseModel):
    cases: list[dict[str, Any]] | None = None
    suite_path: str | None = None
    release_id: str | None = None


class RSICompareRequest(BaseModel):
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    min_delta: float = 0.03


class RSIPromoteRequest(BaseModel):
    release_id: str
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    min_delta: float = 0.03


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    bus = EventBus(settings.data_dir, settings.release_id, settings.clock_id)
    mock = MockBusiness()
    business_gateway = None
    if os.getenv("BUSINESS_GATEWAY", "mock").strip().lower() == "real":
        business_gateway = create_business_gateway()
    tools = ToolGateway(settings.data_dir, mock, bus, business_gateway=business_gateway)
    # 场景决定 Flow、知识库和默认 Agent；默认是赛题指定的 POP 资审驳回外呼。
    root = Path(__file__).resolve().parent.parent
    if settings.scenario_id == "pop_qualification_audit":
        flowpack_path = root / "flowpacks" / "pop_qualification_audit.json"
        knowledge_path = root / "knowledge" / "pop_audit.json"
    else:
        flowpack_path = root / "flowpacks" / "service_outbound.json"
        knowledge_path = root / "knowledge" / "faq.json"
    flowpack = FlowPack.load(flowpack_path)
    llm = LLMRouter(
        bus=bus,
        daily_budget_cny=float(os.getenv("LLM_DAILY_BUDGET_CNY", "100")),
    )
    if os.getenv("AGENT_PROVIDER", "fake").strip().lower() == "llm":
        from .agent.llm_agent import LLMAgent
        # 8084/LLM experiment path: inject the real POP case context instead of
        # making the model guess from the knowledge base.
        case_context: dict[str, Any] = {}
        raw_context = os.getenv("POP_CONTEST_CONTEXT", "").strip()
        if raw_context:
            try:
                parsed = json.loads(raw_context)
            except json.JSONDecodeError as exc:
                raise RuntimeError("POP_CONTEST_CONTEXT must be a JSON object") from exc
            if isinstance(parsed, dict):
                case_context = parsed
        agent: Any = LLMAgent(llm, knowledge_path, flowpack, case_context=case_context)
    elif settings.scenario_id == "pop_qualification_audit":
        agent = PopAuditAgent(knowledge_path)
    else:
        agent = FakeAgent()

    # Competition path: six-stage POP agent with a case-specific context and
    # a local writeback store. It is opt-in because it needs request/brand/
    # reject fields that are not available for a generic text session.
    pop_store = None
    if _bool("POP_CONTEST", "0") and settings.scenario_id == "pop_qualification_audit":
        from .agent.pop_competition import PopCompetitionAgent
        from .agent.pop_repair import BASELINE, GUIDED, REPAIRED
        from .tools.pop_writeback import PopContactStore

        raw_context = os.getenv("POP_CONTEST_CONTEXT", "").strip()
        try:
            context = json.loads(raw_context) if raw_context else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("POP_CONTEST_CONTEXT must be a JSON object") from exc
        required = ("request_id", "brand", "reject_l1", "reject_l2", "reject_l3")
        missing = [key for key in required if not str(context.get(key, "")).strip()]
        if missing:
            raise RuntimeError(f"POP_CONTEST_CONTEXT missing: {', '.join(missing)}")
        policy_name = os.getenv("POP_CONTEST_POLICY", "guided").strip().lower()
        policy_map = {"legacy": BASELINE, "repair": REPAIRED, "guided": GUIDED}
        if policy_name not in policy_map:
            raise RuntimeError("POP_CONTEST_POLICY must be legacy|repair|guided")
        pop_store = PopContactStore(settings.data_dir / "index" / "pop_contacts.db", bus)
        agent = PopCompetitionAgent(knowledge_path, context, pop_store,
                                    policy=policy_map[policy_name])

    compliance = ComplianceGuard()
    voice = VoiceRuntime()
    await voice.initialize()
    orchestrator = Orchestrator(
        bus=bus,
        agent=agent,
        tools=tools,
        compliance=compliance,
        release_id=settings.release_id,
        scenario_id=settings.scenario_id,
        tts=voice.tts,
    )
    app.state.bus = bus
    app.state.tools = tools
    app.state.mock = mock
    app.state.business_gateway = business_gateway
    app.state.orchestrator = orchestrator
    app.state.registry = Registry(settings.data_dir)
    app.state.flowpack = flowpack
    app.state.llm = llm
    app.state.evals = {}
    app.state.voice = voice
    app.state.pop_store = pop_store
    app.state.livekit_bridges = BridgeRegistry(app)
    try:
        yield
    finally:
        await app.state.livekit_bridges.stop_all()
        if pop_store is not None:
            pop_store.close()
        bus.close()
        await tools.aclose()


app = FastAPI(title="智语外呼", version="0.2.0", lifespan=lifespan)


def _orc(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def _bus(request: Request) -> EventBus:
    return request.app.state.bus


@app.get("/voice/status")
async def voice(request: Request) -> dict[str, Any]:
    return (await request.app.state.voice.refresh()).to_dict()


@app.get("/media/status")
async def media_status(request: Request) -> dict[str, Any]:
    configured = all(os.getenv(key) for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    return {"provider": "livekit", "configured": configured,
            "url": os.getenv("LIVEKIT_URL", "") if configured else ""}


def _rtc_payload(session_id: str) -> dict[str, Any]:
    url = os.getenv("LIVEKIT_URL", "").strip()
    key = os.getenv("LIVEKIT_API_KEY", "").strip()
    secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not (url and key and secret):
        raise HTTPException(status_code=503, detail={
            "code": "MEDIA_NOT_CONFIGURED",
            "message": "set LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET",
        })
    room = f"aicall-{session_id}"
    token = create_livekit_token(key, secret, session_id, room)
    return {"provider": "livekit", "url": url, "room": room, "token": token}


@app.post("/sessions/{session_id}/rtc")
async def rtc_token(request: Request, session_id: str) -> dict[str, Any]:
    try:
        _orc(request).get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    payload = _rtc_payload(session_id)
    # The LiveKit bridge must join the same room/session as the browser. This
    # endpoint is the single place both sides obtain that room.
    if _bool("LIVEKIT_BRIDGE", "0"):
        try:
            await request.app.state.livekit_bridges.start(session_id, payload["room"])
        except Exception as exc:
            # Keep the media token usable even if the optional bridge/SDK fails;
            # the browser can still join, and the existing ws_pcm path remains.
            print(f"[livekit-bridge] start failed: {type(exc).__name__}: {exc}", flush=True)
    return payload


@app.post("/sessions/{session_id}/token")
async def refresh_token(request: Request, session_id: str) -> dict[str, Any]:
    try:
        session = _orc(request).get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    session.token = secrets.token_urlsafe(16)
    return {"session_id": session_id, "token": session.token,
            "ws_url": f"/ws/session/{session_id}?token={session.token}"}


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    orc = _orc(request)
    return {
        "status": "ok",
        "release_id": settings.release_id,
        "mode": "fake",
        "sessions": len(orc.sessions),
        "llm_configured": request.app.state.llm.configured,
        "clock_id": settings.clock_id,
    }


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    orc = _orc(request)
    lines = [
        "# HELP aicall_up 1 if the skeleton is running",
        "# TYPE aicall_up gauge",
        "aicall_up 1",
        "# HELP aicall_sessions Number of sessions in this process",
        "# TYPE aicall_sessions gauge",
        f"aicall_sessions {len(orc.sessions)}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/contest/cases")
async def contest_cases(request: Request) -> dict[str, Any]:
    catalog = getattr(request.app.state, "contest_catalog", None)
    if catalog is None:
        raise HTTPException(status_code=404, detail="contest mode is unavailable")
    return catalog


@app.post("/sessions")
async def create_session(request: Request, body: CreateSessionRequest) -> dict[str, Any]:
    orc = _orc(request)
    if body.case_id is not None:
        catalog = getattr(request.app.state, "contest_catalog", None)
        if catalog is None or all(case["id"] != body.case_id for case in catalog["cases"]):
            raise HTTPException(status_code=422, detail="unknown contest case")
    session = await orc.create_session(body.mode, case_id=body.case_id)
    session.token = secrets.token_urlsafe(16)
    return {
        "session_id": session.session_id,
        "token": session.token,
        "ws_url": f"/ws/session/{session.session_id}?token={session.token}",
        "version_set": session.version_set,
    }


@app.get("/sessions/{session_id}/state")
async def get_state(request: Request, session_id: str) -> dict[str, Any]:
    try:
        session = _orc(request).get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": session.session_id,
        "turn_state": session.turn_state,
        "flow_state": session.flow_state,
        "slots": session.slots,
        "turn_id": session.current_turn.turn_id if session.current_turn else None,
        "turn_seq": session.turn_seq,
        "version_set": session.version_set,
    }


@app.post("/sessions/{session_id}/text")
async def text_turn(request: Request, session_id: str, body: TextTurnRequest) -> dict[str, Any]:
    try:
        return await _orc(request).handle_text(session_id, body.text, None)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.post("/sessions/{session_id}/cancel")
async def cancel_turn(request: Request, session_id: str, body: CancelRequest) -> dict[str, Any]:
    try:
        ok = await _orc(request).cancel_current(session_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": ok, "turn_id": body.turn_id}


@app.post("/sessions/{session_id}/end")
async def end_session(request: Request, session_id: str) -> dict[str, Any]:
    await request.app.state.livekit_bridges.stop(session_id)
    try:
        await _orc(request).end_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@app.get("/sessions/{session_id}/events")
async def get_events(request: Request, session_id: str,
                     since_seq: int = Query(default=0, ge=0)) -> Response:
    try:
        events = _bus(request).read_events_since(session_id, since_seq)
    except Exception:
        events = []
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    return Response(content=body, media_type="application/x-ndjson")


@app.post("/eval/run")
async def eval_run(request: Request, body: EvalRunRequest) -> dict[str, Any]:
    orc = _orc(request)
    runner = EvalRunner(orc, _bus(request))
    if body.cases:
        cases = [
            EvalCase(
                case_id=str(item.get("case_id", f"case-{index}")),
                turns=list(item.get("turns", [])),
                expected_outcome=str(item.get("expected_outcome", "any")),
                tags=list(item.get("tags", [])),
            )
            for index, item in enumerate(body.cases, start=1)
        ]
    else:
        default_suite = "pop_audit.json" if settings.scenario_id == "pop_qualification_audit" else "dev.json"
        path = Path(body.suite_path) if body.suite_path else (
            Path(__file__).resolve().parent.parent / "eval" / "cases" / default_suite
        )
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"suite not found: {path}")
        cases = EvalRunner.load_cases(path)
    return await runner.run(cases, body.release_id)


@app.post("/eval/sessions")
async def eval_session(request: Request, body: EvalRequest) -> dict[str, Any]:
    orc = _orc(request)
    session = await orc.create_session("text")
    turns = body.turns or [
        "你好，我想问一下订单什么时候到？",
        "我想改到明天下午三点。",
        "可以，帮我改吧。",
    ]
    results = []
    for text in turns:
        results.append(await orc.handle_text(session.session_id, text, None))
    events = _bus(request).read_events(session.session_id)
    latencies = [e["payload"].get("e2e_ms") for e in events if e.get("type") == "TurnLatency"]
    last = results[-1] if results else {}
    outcome = (last.get("result") or {}).get("outcome") if isinstance(last, dict) else None
    metrics = {
        "turns": len(results),
        "task_success": outcome in {"rescheduled", "completed"},
        "outcome": outcome,
        "e2e_ms": latencies,
        "p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
    }
    eval_id = "es_" + secrets.token_hex(8)
    record = {
        "eval_session_id": eval_id,
        "session_id": session.session_id,
        "scenario_id": body.scenario_id or settings.scenario_id,
        "release_id": body.release_id or settings.release_id,
        "metrics": metrics,
        "turns": results,
        "artifacts": {"events": str(_bus(request).events_path(session.session_id))},
    }
    request.app.state.evals[eval_id] = record
    return record


@app.get("/eval/sessions/{eval_session_id}")
async def get_eval(request: Request, eval_session_id: str) -> dict[str, Any]:
    record = request.app.state.evals.get(eval_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="eval session not found")
    return record


@app.post("/internal/tools/{tool}/invoke")
async def invoke_tool(request: Request, tool: str, body: ToolInvokeRequest) -> dict[str, Any]:
    session_id = request.headers.get("X-Session-Id", "s_internal")
    turn_id = request.headers.get("X-Turn-Id", "t_0000")
    trace_id = request.headers.get("X-Trace-Id")
    return await request.app.state.tools.invoke(
        tool, body.args,
        idempotency_key=body.idempotency_key,
        deadline_ms=body.deadline_ms,
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
    )


@app.get("/flowpacks")
async def list_flowpacks(request: Request) -> dict[str, Any]:
    pack: FlowPack = request.app.state.flowpack
    return {"flowpacks": [{"id": pack.id, "version": pack.version, "entry": pack.entry}]}


@app.get("/flowpacks/{pack_id}")
async def get_flowpack(request: Request, pack_id: str) -> dict[str, Any]:
    pack: FlowPack = request.app.state.flowpack
    if pack.id != pack_id:
        raise HTTPException(status_code=404, detail="flow pack not found")
    return {"id": pack.id, "version": pack.version, "entry": pack.entry,
            "states": pack.states, "slots": pack.slots}


@app.post("/flowpacks/{pack_id}/simulate")
async def simulate_flowpack(request: Request, pack_id: str, body: FlowSimulateRequest) -> dict[str, Any]:
    pack: FlowPack = request.app.state.flowpack
    if pack.id != pack_id:
        raise HTTPException(status_code=404, detail="flow pack not found")
    from .flow.pack import FlowEngine
    engine = FlowEngine(pack)
    steps = [engine.start()]
    for intent in body.intents:
        steps.append(engine.apply_intent(intent))
    return {"flow_id": pack.id, "steps": steps, "snapshot": engine.snapshot()}


@app.post("/knowledge/search")
async def knowledge_search(request: Request, body: KnowledgeSearchRequest) -> dict[str, Any]:
    kb = request.app.state.orchestrator.agent.knowledge
    chunks = kb.search(body.query, top_k=body.top_k)
    return {
        "query": body.query,
        "chunks": [
            {
                "doc_id": c.doc_id,
                "title": c.title,
                "content": c.content,
                "score": round(c.score, 4),
                "citation": c.citation(),
            }
            for c in chunks
        ],
    }


@app.post("/internal/llm/complete")
async def llm_complete(request: Request, body: LLMCompleteRequest) -> dict[str, Any]:
    result = await request.app.state.llm.complete(
        body.messages,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        response_format=body.response_format,
        session_id=body.session_id,
        turn_id=body.turn_id,
    )
    return asdict(result)


@app.post("/rsi/compare")
async def rsi_compare_endpoint(body: RSICompareRequest) -> dict[str, Any]:
    return rsi_compare(body.baseline, body.candidate, GateConfig(min_delta=body.min_delta))


@app.post("/rsi/promote")
async def rsi_promote(request: Request, body: RSIPromoteRequest) -> dict[str, Any]:
    gate = rsi_compare(body.baseline, body.candidate, GateConfig(min_delta=body.min_delta))
    if gate["decision"] != "pass":
        raise HTTPException(status_code=409, detail={"gate": gate})
    try:
        release = request.app.state.registry.promote(
            body.release_id, {"ok": True, "reasons": gate.get("reasons", [])}
        )
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"gate": gate, "release": release}


@app.get("/registry/production")
async def registry_production(request: Request) -> dict[str, Any]:
    try:
        return request.app.state.registry.production()
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/registry/releases")
async def registry_releases(request: Request) -> dict[str, Any]:
    return {"releases": request.app.state.registry.list_releases()}


@app.post("/registry/candidates")
async def registry_candidate(request: Request, body: CandidateRequest) -> dict[str, Any]:
    try:
        return request.app.state.registry.create_candidate(body.artifacts, body.metrics, body.notes)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/registry/promote")
async def registry_promote(request: Request, body: PromoteRequest) -> dict[str, Any]:
    try:
        return request.app.state.registry.promote(body.release_id, body.gate)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/registry/rollback")
async def registry_rollback(request: Request) -> dict[str, Any]:
    try:
        return request.app.state.registry.rollback()
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def request_bus_emit(websocket: WebSocket, session_id: str, event_type: str,
                            module: str, payload: dict[str, Any]) -> None:
    if not event_type or not event_type[0].isupper():
        event_type = "ClientEvent"
    await websocket.app.state.bus.emit(
        session_id=session_id, type=event_type, module=module, payload=payload,
    )


@app.websocket("/ws/session/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str) -> None:
    orc: Orchestrator = websocket.app.state.orchestrator
    try:
        session = orc.get_session(session_id)
    except KeyError:
        await websocket.close(code=4404)
        return

    if not settings.dev_no_auth:
        token = websocket.query_params.get("token", "")
        if token != session.token:
            await websocket.close(code=4401)
            return

    await websocket.accept()
    if session.ws is not None or session.turn_state == "ENDED":
        await websocket.close(code=4409)
        return
    conn = WsConnection(websocket)
    await orc.attach(session_id, conn)
    # The LiveKit bridge may be started before or after this WS attaches.
    _bridge = getattr(websocket.app.state, "livekit_bridges", None)
    if _bridge is not None and session_id in _bridge.bridges:
        _bridge.bridges[session_id].browser_ws = conn
    tasks: set[asyncio.Task] = set()
    asr = None
    asr_task = None
    last_seq = None
    rtc = None
    inbox = None
    ws_reader = None
    if getattr(websocket.app.state, "rtc_pcm_enabled", False):
        inbox = asyncio.Queue(maxsize=256)

        async def forward_ws():
            while True:
                event = await websocket.receive()
                await inbox.put(event)
                if event["type"] == "websocket.disconnect":
                    return

        ws_reader = asyncio.create_task(forward_ws())

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def consume_asr(stream):
        handled = False
        async for result in stream.events():
            if result.kind == "error":
                await conn.send_json({"type": "error", "error": result.error})
                handled = True
                continue
            metadata = {"confidence": result.confidence, "audio_start_ms": result.audio_start_ms,
                        "audio_end_ms": result.audio_end_ms}
            if result.kind == "final":
                if result.text.strip():
                    handled = True
                    # Allocate the real turn under the orchestrator's lock, then
                    # send exactly one final with that turn_id (never predict it).
                    spawn(orc.handle_text(session_id, result.text, conn, transcript_metadata=metadata))
            else:
                await conn.send_json({"type": "transcript", "text": result.text,
                                      "final": False, **metadata})
        return handled

    try:
        while True:
            event = await inbox.get() if inbox is not None else await websocket.receive()
            if event["type"] == "websocket.disconnect":
                break
            if conn.rtc_pcm and not event.get("rtc_pcm"):
                continue
            if "bytes" in event and event["bytes"] is not None:
                try:
                    frame = AudioFrame.parse(event["bytes"])
                    if last_seq is not None:
                        delta = (frame.seq - last_seq) & 0xFFFFFFFF
                        if delta == 0 or delta > 0x7FFFFFFF:
                            raise VoiceError("AUDIO_SEQUENCE", "duplicate or out-of-order PCM frame")
                        if delta > 1:
                            await request_bus_emit(websocket, session_id, "Degraded", "voice.asr",
                                                   {"reason": "audio_gap", "missing_frames": delta - 1})
                    last_seq = frame.seq
                    if asr is None:
                        asr = await websocket.app.state.voice.start_asr(
                            session_id, client_endpointing=session.case_id is not None,
                        )
                        asr_task = asyncio.create_task(consume_asr(asr))
                    await asr.push(frame)
                    session.audio_frames += 1
                except Exception as exc:
                    error = exc if isinstance(exc, VoiceError) else VoiceError("ASR_FAILED", str(exc), True)
                    await conn.send_json({"type": "error", "error": error.to_dict()})
                continue
            raw = event.get("text")
            if raw is None:
                continue
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError("expected JSON object")
            except ValueError as exc:
                await conn.send_json({"type": "error", "error": {
                    "code": "INVALID_MESSAGE", "message": str(exc), "retryable": False}})
                continue
            kind = msg.get("type")
            if kind == "rtc_pcm_offer" and inbox is not None and rtc is None:
                from .media.rtc_pcm import RtcPcm
                sdp = msg.get("sdp")
                if not isinstance(sdp, str) or len(sdp) > 16000:
                    await conn.send_json({"type": "error", "error": {
                        "code": "RTC_OFFER_INVALID", "message": "Invalid WebRTC offer", "retryable": False}})
                    continue
                rtc = RtcPcm(inbox)
                try:
                    answer = await asyncio.wait_for(rtc.answer(sdp), 6)
                    await conn.send_json({"type": "rtc_pcm_answer", "sdp": answer})
                except Exception:
                    await rtc.peer.close()
                    rtc = None
                    await conn.send_json({"type": "error", "error": {
                        "code": "RTC_NEGOTIATION", "message": "WebRTC negotiation failed", "retryable": True}})
            elif kind == "rtc_pcm_ready" and rtc and rtc.ready and event.get("rtc_pcm") and not conn.rtc_pcm:
                conn.rtc_pcm = rtc
                session.transport = "rtc_pcm"
                await request_bus_emit(websocket, session_id, "TransportSwitched", "media",
                                       {"to": "rtc_pcm", "profile": "webrtc_datachannel"})
                await conn.send_json({"type": "transport_switched", "to": "rtc_pcm"})
            elif kind == "hello":
                await conn.send_json({
                    "type": "hello_ack", "session_id": session_id,
                    "protocol_version": "0.2", "server_version": "0.2.0",
                    "transport": session.transport,
                })
            elif kind == "call_ready":
                spawn(orc.open_outbound(session_id, conn))
            elif kind == "text":
                text = msg.get("text")
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                    await conn.send_json({"type": "error", "error": {
                        "code": "INVALID_TEXT", "message": "text must be 1..2000 chars", "retryable": False}})
                else:
                    spawn(orc.handle_text(session_id, text, conn))
            elif kind == "speech_ended":
                if asr is not None:
                    await asr.finish()
                    handled = await asr_task
                    asr, asr_task = None, None
                    voiced = msg.get("last_voiced_mono_ms")
                    if (not handled and getattr(orc.agent, "opens_outbound", False)
                            and isinstance(voiced, (int, float)) and voiced > 0):
                        await request_bus_emit(websocket, session_id, "AsrNoMatch", "voice.asr", {})
                        await conn.send_json({"type": "error", "error": {
                            "code": "ASR_NO_MATCH", "message": "No speech was recognized",
                            "user_message": "没有听清，请再说一遍", "retryable": True,
                        }})
            elif kind in {"speech_started", "local_muted", "playback_first_frame",
                          "playback_ended", "ping"}:
                await orc.on_client_event(session_id, msg)
            elif kind == "hangup":
                await orc.end_session(session_id, str(msg.get("reason", "user_click")))
                break
            elif kind == "media_request":
                try:
                    payload = _rtc_payload(session_id)
                    if _bool("LIVEKIT_BRIDGE", "0"):
                        try:
                            await websocket.app.state.livekit_bridges.start(session_id, payload["room"])
                        except Exception as exc:
                            print(f"[livekit-bridge] start failed: {type(exc).__name__}: {exc}", flush=True)
                    await conn.send_json({"type": "media_ready", **payload})
                except HTTPException as exc:
                    await conn.send_json({"type": "error", "error": {
                        "code": "MEDIA_NOT_CONFIGURED", "message": str(exc.detail),
                        "retryable": False}})
            elif kind == "transport_degraded":
                reason = str(msg.get("reason", "unknown"))
                target = str(msg.get("to", "ws_pcm"))
                await request_bus_emit(websocket, session_id, "Degraded", "media", {
                    "component": "media", "from": msg.get("from", "webrtc"),
                    "to": target, "reason": reason, "stats": msg.get("stats", {}),
                })
                if target == "text_only":
                    await conn.send_json({"type": "transport_failed", "reason": reason,
                                          "fallback": "text_only"})
                else:
                    session.transport = target
                    await conn.send_json({"type": "transport_switched", "to": target,
                                          "audio": {"uplink": "pcm16/16000",
                                                    "downlink": f"pcm16/{orc.tts.sample_rate}"}})
            elif kind == "client_event":
                name = str(msg.get("name", ""))
                if name not in CLIENT_EVENT_NAMES:
                    await conn.send_json({"type": "error", "error": {
                        "code": "CLIENT_EVENT_NOT_ALLOWED",
                        "message": f"allowed: {sorted(CLIENT_EVENT_NAMES)}",
                        "retryable": False}})
                    continue
                await request_bus_emit(websocket, session_id, name, "client", {
                    "name": name, "payload": msg.get("payload", {}),
                    "client_mono_ms": msg.get("client_mono_ms"),
                })
            else:
                await conn.send_json({"type": "error", "error": {
                    "code": "UNKNOWN_MESSAGE", "message": f"unknown type {kind}", "retryable": False}})
    except WebSocketDisconnect:
        pass
    finally:
        if ws_reader:
            ws_reader.cancel()
            await asyncio.gather(ws_reader, return_exceptions=True)
        if asr is not None:
            await asr.close()
        if asr_task:
            asr_task.cancel()
            await asyncio.gather(asr_task, return_exceptions=True)
        if session.current_turn:
            session.current_turn.cancel.cancel("SESSION_END")
        if session.playing_turn:
            session.playing_turn.cancel.cancel("SESSION_END")
            session.playing_turn = None
        session.ws = None
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if rtc:
            await rtc.peer.close()


if WEB_DIR.exists():
    # Python's Windows mimetypes registry often maps .mjs to text/plain, which
    # makes browsers reject the LiveKit ESM import. Serve it as JavaScript.
    mimetypes.add_type("text/javascript", ".mjs")
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    async def index_placeholder() -> str:
        return "<h1>web/ not found. Run the API or add web/index.html.</h1>"
