from __future__ import annotations

import asyncio
import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .brain import Brain
from .forecast import OpeningForecastCache
from .hearing import Hearing
from .local_asr import load_from_env, make_transcriber
from .models import TaskCard
from .providers import Cloud, Settings
from .session import Session
from .decision_model import DecisionModel
from .task_prewarm import cancel_task_prewarm, start_task_prewarm
from .vad import model as voice_detector_model
from .decision_tracker import DecisionTracker
from .monitoring import PerformanceMonitor
from .prediction_stats import build_prediction_stats

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.load()
    app.state.default_card = TaskCard.model_validate_json(
        (ROOT / "tasks" / "jd-qualification.json").read_text(encoding="utf-8")
    )
    cache_path = Path(os.getenv("CACHE_PATH", "runtime/cache.sqlite3"))
    if not cache_path.is_absolute():
        cache_path = ROOT / cache_path
    # Trackers are created first so provider cache events can be recorded at
    # the single point where each cache/network request actually happens.
    app.state.decision_tracker = DecisionTracker()
    app.state.performance_monitor = PerformanceMonitor()
    app.state.cloud = Cloud(
        app.state.settings, cache_path=cache_path,
        on_cache_event=app.state.decision_tracker.record_cache_event,
    )
    app.state.local_asr = load_from_env()
    app.state.transcriber = make_transcriber(app.state.local_asr, app.state.cloud)
    if app.state.local_asr is not None:
        try:
            await asyncio.to_thread(app.state.local_asr.load)
        except Exception:  # noqa: BLE001 - the cloud path still works without the model
            app.state.local_asr = None
            app.state.transcriber = app.state.cloud.transcribe
    voice_detector_model()
    app.state.sessions = set()
    app.state.opening_forecasts = OpeningForecastCache(app.state.cloud.persistent)
    yield
    for session in list(app.state.sessions):
        await session.close()
    await app.state.cloud.close()


app = FastAPI(title="AI Call G2", lifespan=lifespan)


@app.get("/api/config")
async def config():
    settings = app.state.settings
    legacy = [TaskCard.model_validate_json((ROOT / "tasks" / name).read_text(encoding="utf-8")).model_dump()
              for name in ("home-broadband-support.json", "legacy/jd-qualification-v1.json", "legacy/jd-qualification-v2.json")]
    return {"configured": bool(settings.api_key), "model": settings.model,
            "legacy_default_tasks": legacy, "legacy_default_task": legacy[0],
            "predictions": settings.predictions, "default_task": app.state.default_card.model_dump()}


@app.get("/api/cache/stats")
async def cache_stats():
    """Return cache performance metrics."""
    return app.state.cloud.cache_report()


@app.get("/api/metrics")
async def performance_metrics():
    """Return comprehensive performance and decision metrics."""
    return {
        "cache": app.state.cloud.cache_report(),
        "decisions": app.state.decision_tracker.report(),
        "performance": app.state.performance_monitor.report(),
    }


@app.get("/api/prediction/stats")
async def prediction_stats():
    """Read-only prediction reuse, latency and cache counters.

    Counts and percentiles are derived from the same turn records that write
    ``runtime/sessions/*.jsonl``; unmeasured fields are ``null`` and carry a
    ``count`` of 0 instead of an invented 0 ms / 0% reading.
    """
    return build_prediction_stats(app.state.decision_tracker,
                                  app.state.performance_monitor)


@app.post("/api/warm")
async def warm(payload: dict):
    """Start a background synthetic conversation for a loaded task card."""
    try:
        card = TaskCard.model_validate(payload.get("task", app.state.default_card.model_dump()))
    except (ValidationError, TypeError, ValueError):
        return {"started": False, "error": "invalid_task"}
    task = start_task_prewarm(card, app.state.cloud,
                              app.state.opening_forecasts, ROOT / "runtime")
    return {"started": task is not None}


@app.get("/healthz")
async def health():
    return {"ok": True, "name": "ai-call-g2", "sessions": len(app.state.sessions)}


@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    # This local app is deliberately same-origin. A random website cannot open
    # a websocket to the user's local API and spend their inference credentials.
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if origin and origin not in {f"http://{host}", f"https://{host}"}:
        await ws.close(code=1008)
        return
    await ws.accept()
    session: Session | None = None
    hearing: Hearing | None = None

    async def send(payload):
        if isinstance(payload, bytes):
            await ws.send_bytes(payload)
        else:
            await ws.send_json(payload)

    async def error(message):
        if session:
            await session.send({"type": "error", "message": message, "recoverable": True})
        else:
            await send({"type": "error", "message": message})

    try:
        while True:
            incoming = await ws.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            if incoming.get("bytes") is not None:
                if hearing:
                    await hearing.feed(incoming["bytes"])
                continue
            raw = incoming.get("text", "")
            if len(raw) > 20000:
                await error("消息过长")
                continue
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError()
                kind = event.get("type")
                if kind == "start" and session is None:
                    if event.get("voice",True) and event.get("audio_protocol") != 2:
                        await error("语音组件已更新，请刷新页面后重新开始通话。")
                        await ws.close(code=1003)
                        break
                    card = TaskCard.model_validate(event.get("task", app.state.default_card.model_dump()))
                    cloud = app.state.cloud
                    # The real call wins immediately. Cancel any synthetic
                    # prewarm conversation for this card; cache entries already
                    # prepared before this point remain valid.
                    cancel_task_prewarm(card, "foreground")
                    session = Session(card, cloud, Brain(
                        cloud, app.state.opening_forecasts,
                        forecast_branches=app.state.settings.forecast_branches,
                        decision_model=DecisionModel(cloud)), send,
                                      voice=bool(event.get("voice", True)),
                                      predictions=bool(event.get("predictions", app.state.settings.predictions)),
                                      log_dir=ROOT / "runtime" / "sessions",
                                      decision_tracker=app.state.decision_tracker,
                                      performance_monitor=app.state.performance_monitor,
                                      on_hangup=lambda: ws.close(code=1000, reason="conversation ended"))
                    app.state.sessions.add(session)
                    if session.voice:
                        hearing = Hearing(cloud,
                            on_start=lambda: session.interrupt("speech", evidence=hearing.last_onset),
                            on_text=lambda text, end: session.user_text(text, source="voice", end_ms=end),
                            on_partial=lambda text: session.send({"type": "transcript", "text": text, "final": False}),
                            on_error=error,
                            is_playing=lambda: session.phase == "speaking",
                            transcriber=app.state.transcriber,
                            performance_monitor=app.state.performance_monitor)
                    await session.start()
                elif session:
                    if kind == "text":
                        if hearing:
                            hearing.reset()
                        if not isinstance(event.get("text"), str):
                            raise ValueError()
                        await session.user_text(event["text"])
                    elif kind == "interrupt":
                        if event.get("source") not in {"manual", "text"}:
                            # Automatic interruptions come exclusively from
                            # cleaned audio + VAD in Hearing, never raw volume.
                            continue
                        epoch = int(event.get("epoch", -1))
                        for progress in event.get("progress", [])[:4]:
                            await session.playback_progress(epoch, int(progress["unit"]), int(progress["samples"]), done=True)
                        source = event.get("source")
                        reason = {"manual": "manual", "text": "text_input"}[source]
                        raw_evidence = event.get("evidence")
                        evidence = {}
                        if isinstance(raw_evidence, dict):
                            for key in ("rms", "threshold", "attack_ms"):
                                value = raw_evidence.get(key)
                                if isinstance(value, (float, int)) and math.isfinite(value):
                                    evidence[key] = round(value, 5)
                            if isinstance(raw_evidence.get("playback"), bool):
                                evidence["playback"] = raw_evidence["playback"]
                        await session.interrupt(reason, expected_epoch=epoch, evidence=evidence)
                    elif kind == "played":
                        await session.playback_progress(int(event["epoch"]), int(event["unit"]),
                                                        int(event["samples"]), done=bool(event.get("done")))
                    elif kind == "playback_started":
                        await session.playback_started(int(event["epoch"]), float(event["client_ms"]),
                                                       unit_id=int(event["unit"]) if "unit" in event else None)
                    elif kind == "stop":
                        break
            except (ValueError, KeyError, TypeError, ValidationError):
                await error("消息格式无效，请检查任务卡或重新开始。")
    except WebSocketDisconnect:
        pass
    finally:
        if hearing:
            await hearing.close()
        if session:
            await session.close()
            app.state.sessions.discard(session)
        try:
            await ws.close()
        except RuntimeError:
            pass


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="preview-assets")
app.mount("/demo", StaticFiles(directory=ROOT / "demo", html=True), name="demo")
