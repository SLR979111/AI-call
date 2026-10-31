from __future__ import annotations

import asyncio
import json
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
from .compliance import ComplianceGuard
from .config import settings
from .events import EventBus
from .eval.runner import EvalCase, EvalRunner
from .flow.pack import FlowPack, FlowPackError
from .llm.router import LLMRouter
from .media.livekit import create_livekit_token
from .orchestrator import Orchestrator, SessionState
from .registry import Registry, RegistryError
from .tools.gateway import MockBusiness, ToolGateway
from .voice.factory import voice_status

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class WsConnection:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.ws.send_json(data)


class CreateSessionRequest(BaseModel):
    mode: Literal["voice", "text"] = "text"
    scenario_id: str | None = None


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    bus = EventBus(settings.data_dir, settings.release_id, settings.clock_id)
    mock = MockBusiness()
    tools = ToolGateway(settings.data_dir, mock, bus)
    flowpack = FlowPack.load(
        Path(__file__).resolve().parent.parent / "flowpacks" / "service_outbound.json"
    )
    llm = LLMRouter(
        bus=bus,
        daily_budget_cny=float(os.getenv("LLM_DAILY_BUDGET_CNY", "100")),
    )
    if os.getenv("AGENT_PROVIDER", "fake").strip().lower() == "llm":
        from .agent.llm_agent import LLMAgent
        agent: Any = LLMAgent(llm, Path(__file__).resolve().parent.parent / "knowledge" / "faq.json",
                              flowpack)
    else:
        agent = FakeAgent()
    compliance = ComplianceGuard()
    orchestrator = Orchestrator(
        bus=bus,
        agent=agent,
        tools=tools,
        compliance=compliance,
        release_id=settings.release_id,
        scenario_id=settings.scenario_id,
    )
    app.state.bus = bus
    app.state.tools = tools
    app.state.mock = mock
    app.state.orchestrator = orchestrator
    app.state.registry = Registry(settings.data_dir)
    app.state.flowpack = flowpack
    app.state.llm = llm
    app.state.evals = {}
    try:
        yield
    finally:
        bus.close()
        tools.close()


app = FastAPI(title="智语外呼 Fake Skeleton", version="0.1.0", lifespan=lifespan)


def _orc(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def _bus(request: Request) -> EventBus:
    return request.app.state.bus


@app.get("/voice/status")
async def voice(request: Request) -> dict[str, Any]:
    return voice_status().to_dict()


@app.get("/media/status")
async def media_status(request: Request) -> dict[str, Any]:
    configured = all(os.getenv(key) for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    return {"provider": "livekit", "configured": configured,
            "url": os.getenv("LIVEKIT_URL", "") if configured else ""}


@app.post("/sessions/{session_id}/rtc")
async def rtc_token(request: Request, session_id: str) -> dict[str, Any]:
    try:
        _orc(request).get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
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


@app.post("/sessions")
async def create_session(request: Request, body: CreateSessionRequest) -> dict[str, Any]:
    orc = _orc(request)
    session = await orc.create_session(body.mode)
    session.token = secrets.token_urlsafe(16)
    return {
        "session_id": session.session_id,
        "token": session.token,
        "ws_url": f"/ws/session/{session.session_id}",
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
        path = Path(body.suite_path) if body.suite_path else (
            Path(__file__).resolve().parent.parent / "eval" / "cases" / "dev.json"
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


@app.websocket("/ws/session/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str) -> None:
    orc: Orchestrator = websocket.app.state.orchestrator
    try:
        session = orc.get_session(session_id)
    except KeyError:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    conn = WsConnection(websocket)
    await orc.attach(session_id, conn)
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "hello":
                await conn.send_json({"type": "hello_ack", "session_id": session_id})
            elif kind == "text":
                spawn(orc.handle_text(session_id, str(msg.get("text", "")), conn))
            elif kind in {"speech_started", "local_muted", "playback_first_frame", "ping"}:
                await orc.on_client_event(session_id, msg)
            else:
                await conn.send_json({"type": "error", "error": {
                    "code": "UNKNOWN_MESSAGE", "message": f"unknown type {kind}", "retryable": False}})
    except WebSocketDisconnect:
        pass
    finally:
        session.ws = None
        for task in list(tasks):
            task.cancel()


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    async def index_placeholder() -> str:
        return "<h1>web/ not found. Run the API or add web/index.html.</h1>"
