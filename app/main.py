from __future__ import annotations

import asyncio
import json
import math
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .brain import Brain
from .forecast import OpeningForecastCache
from .hearing import Hearing
from .local_asr import FirstPassASR, load_from_env, make_transcriber
from .local_provider import LocalProvider
from .semantic_decision import warmup_shared
from .models import TaskCard
from .providers import Cloud, ProviderError, Settings
from .session import Session
from .vad import model as voice_detector_model
from .decision_tracker import DecisionTracker
from .monitoring import PerformanceMonitor
from .task_prewarm import TaskPrewarmer
from .prediction_stats import build_prediction_stats

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACK = "jd-qualification"
TASK_PACKS = (DEFAULT_PACK, "jd-submitted", "jd-authorization", "jd-busy", "jd-goodbye", "jd-long-call",
              "jd-category-report", "jd-license-appendix", "jd-license-scope",
              "jd-trademark-renewal", "jd-brand-mixup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.load()
    app.state.task_packs = {
        name: TaskCard.model_validate_json((ROOT / "tasks" / f"{name}.json").read_text(encoding="utf-8"))
        for name in TASK_PACKS
    }
    app.state.default_card = app.state.task_packs[DEFAULT_PACK]
    async with AsyncExitStack() as cleanup:
        app.state.first_pass = FirstPassASR.load()
        if app.state.first_pass:
            cleanup.callback(app.state.first_pass.close)
        app.state.local = LocalProvider(app.state.first_pass)
        cleanup.push_async_callback(app.state.local.close)
        app.state.local_forecasts = OpeningForecastCache()
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
        cleanup.push_async_callback(app.state.cloud.close)
        app.state.local_asr = load_from_env()
        app.state.transcriber = make_transcriber(app.state.local_asr, app.state.cloud)
        if app.state.local_asr is not None:
            try:
                await asyncio.to_thread(app.state.local_asr.load)
            except Exception:  # noqa: BLE001 - the cloud path still works without the model
                app.state.local_asr = None
                app.state.transcriber = app.state.cloud.transcribe
        # Load the semantic matcher in the background: turns that arrive first simply
        # fall back to the lexical rules until the weights are ready.
        app.state.semantic_task = asyncio.create_task(warmup_shared())
        cleanup.push_async_callback(asyncio.gather, app.state.semantic_task, return_exceptions=True)
        voice_detector_model()
        app.state.sessions = set()
        app.state.opening_forecasts = OpeningForecastCache(app.state.cloud.persistent)
        app.state.prewarm = None

        async def close_connection_warmup():
            task = app.state.prewarm
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        async def close_sessions():
            await asyncio.gather(*(session.close() for session in list(app.state.sessions)))

        cleanup.push_async_callback(close_connection_warmup)
        app.state.task_prewarmer = TaskPrewarmer()
        cleanup.push_async_callback(app.state.task_prewarmer.close)
        cleanup.push_async_callback(close_sessions)
        yield


app = FastAPI(title="AI Call G2", lifespan=lifespan)


@app.get("/api/config")
async def config():
    settings = app.state.settings
    legacy = [TaskCard.model_validate_json((ROOT / "tasks" / name).read_text(encoding="utf-8")).model_dump()
              for name in ("home-broadband-support.json", "legacy/jd-qualification-v1.json", "legacy/jd-qualification-v2.json")]
    return {"configured": bool(settings.api_key), "model": settings.model,
            "legacy_default_tasks": legacy, "legacy_default_task": legacy[0],
            "task_packs": {name: card.model_dump() for name, card in app.state.task_packs.items()},
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


def warm_cloud_connection():
    # Open the connection when the browser selects cloud mode.
    if app.state.prewarm is None:
        app.state.prewarm = asyncio.create_task(app.state.cloud.prewarm())


@app.post("/api/warm")
async def warm(payload: dict):
    """Start background synthetic conversations for a loaded task card."""
    try:
        card = TaskCard.model_validate(payload.get("task", app.state.default_card.model_dump()))
    except (ValidationError, TypeError, ValueError):
        return {"started": False, "error": "invalid_task"}
    warm_cloud_connection()
    task = app.state.task_prewarmer.start(card, app.state.cloud, app.state.opening_forecasts,
                                        ROOT / "runtime")
    return {"started": task is not None}


@app.get("/api/prediction/stats")
async def prediction_stats():
    """Read-only prediction reuse, latency and cache counters."""
    return build_prediction_stats(app.state.decision_tracker,
                                  app.state.performance_monitor)


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
                    mode = event.get("mode", "cloud")
                    if mode not in ("local", "cloud"):
                        raise ValueError("Invalid call mode")
                    local = mode == "local"
                    cloud = app.state.local if local else app.state.cloud
                    if local:
                        try:
                            await cloud.ready(voice=bool(event.get("voice", True)))
                        except (ProviderError, RuntimeError) as exc:
                            await error(str(exc) if isinstance(exc, ProviderError) else "本地语音模型加载失败，请重新安装。")
                            await ws.close(code=1011)
                            break
                    else:
                        warm_cloud_connection()
                    forecasts = app.state.local_forecasts if local else app.state.opening_forecasts
                    # A real call wins immediately; prewarm cache entries already
                    # written before this point remain valid.
                    app.state.task_prewarmer.cancel(card)
                    session = Session(card, cloud, Brain(
                        cloud, forecasts,
                        forecast_branches=app.state.settings.forecast_branches), send,
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
                            first_pass=None if local else app.state.first_pass, on_draft=session.prepare_voice,
                            min_silence_ms=680 if local else 0,
                            discard_draft=session.discard_voice,
                            on_lead=session.prepare_lead if session.predictions and not local else None,
                            on_error=error,
                            is_playing=lambda: session.phase == "speaking",
                            transcriber=cloud.transcribe if local else app.state.transcriber,
                            performance_monitor=app.state.performance_monitor)
                    await session.start()
                elif session:
                    if kind == "text":
                        if not isinstance(event.get("text"), str):
                            raise ValueError()
                        if hearing:
                            hearing.reset()
                        await session.user_text(event["text"])
                    elif kind == "interrupt":
                        if event.get("source") not in {"manual", "text"}:
                            # Automatic interruptions come exclusively from
                            # cleaned audio + VAD in Hearing, never raw volume.
                            continue
                        epoch = int(event.get("epoch", -1))
                        receipts = event.get("progress", [])
                        if not isinstance(receipts, list):
                            raise ValueError()
                        progress = [(int(item["unit"]), int(item["samples"])) for item in receipts[:4]]
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
                        for unit, samples in progress:
                            await session.playback_progress(epoch, unit, samples, done=True)
                        await session.interrupt(reason, expected_epoch=epoch, evidence=evidence)
                    elif kind == "played":
                        await session.playback_progress(int(event["epoch"]), int(event["unit"]),
                                                        int(event["samples"]), done=bool(event.get("done")))
                    elif kind == "playback_started":
                        await session.playback_started(int(event["epoch"]), float(event["client_ms"]),
                                                       unit_id=int(event["unit"]) if "unit" in event else None)
                    elif kind == "stop":
                        break
            except (ValueError, KeyError, TypeError, OverflowError, ValidationError):
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
