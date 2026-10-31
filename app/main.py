from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
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
from .orchestrator import Orchestrator, SessionState
from .tools.gateway import MockBusiness, ToolGateway

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    bus = EventBus(settings.data_dir, settings.release_id, settings.clock_id)
    mock = MockBusiness()
    tools = ToolGateway(settings.data_dir, mock, bus)
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


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    orc = _orc(request)
    return {
        "status": "ok",
        "release_id": settings.release_id,
        "mode": "fake",
        "sessions": len(orc.sessions),
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
