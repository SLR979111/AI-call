from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .brain import Brain
from .hearing import Hearing
from .models import TaskCard
from .providers import Cloud, Settings
from .session import Session
from .vad import model as voice_detector_model

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.load()
    app.state.default_card = TaskCard.model_validate_json(
        (ROOT / "tasks" / "jd-qualification.json").read_text(encoding="utf-8")
    )
    app.state.cloud = Cloud(app.state.settings)
    voice_detector_model()
    app.state.sessions = set()
    yield
    for session in list(app.state.sessions):
        await session.close()
    await app.state.cloud.close()


app = FastAPI(title="AI Call G2", lifespan=lifespan)


@app.get("/api/config")
async def config():
    settings = app.state.settings
    return {"configured": bool(settings.api_key), "model": settings.model,
            "predictions": settings.predictions, "default_task": app.state.default_card.model_dump()}


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
                    session = Session(card, cloud, Brain(cloud), send,
                                      voice=bool(event.get("voice", True)),
                                      predictions=bool(event.get("predictions", app.state.settings.predictions)),
                                      log_dir=ROOT / "runtime" / "sessions")
                    app.state.sessions.add(session)
                    if session.voice:
                        hearing = Hearing(cloud,
                            on_start=lambda: session.interrupt("speech", evidence=hearing.last_onset),
                            on_text=lambda text, end: session.user_text(text, source="voice", end_ms=end),
                            on_partial=lambda text: session.send({"type": "transcript", "text": text, "final": False}),
                            on_error=error,
                            is_playing=lambda: session.phase == "speaking")
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
                        for progress in event.get("progress", [])[:2]:
                            await session.playback_progress(epoch, int(progress["unit"]), int(progress["samples"]))
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
                        await session.playback_started(int(event["epoch"]), float(event["client_ms"]))
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
