from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.cancel import CancelToken, TurnCancelled
from app.voice.streaming import thread_stream
from app.voice.types import VoiceError, QWEN_MAX_TEXT_CHARS

INSTRUCT = "用自然、平稳、亲切的客服语气，语速适中，句间停顿自然，不要夸张。"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise VoiceError("QWEN_CONFIG", f"{name} must be 0/1, true/false")


class QwenEngine:
    sample_rate = 24000

    def __init__(self):
        self.ready = False
        self.detail = "loading"
        self.lock = threading.Lock()
        self.model = None

    def load(self):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        model_dir = Path(os.environ.get("QWEN_MODEL_DIR", "models/Qwen3-TTS-12Hz-1.7B-Base"))
        ref = Path(os.environ.get("QWEN_REF_AUDIO", ""))
        self.ref_text = os.environ.get("QWEN_REF_TEXT", "").strip()
        self.instruct = os.getenv("QWEN_INSTRUCT", INSTRUCT)
        self.chunk_size = int(os.getenv("QWEN_CHUNK_SIZE", "4"))
        # These defaults reproduce the auditioned "Qwen3-TTS Base · clone + service
        # instruct" sample: library clone defaults, step-by-step text feed, chunk 4.
        self.non_streaming_mode = _env_bool("QWEN_NON_STREAMING_MODE", False)
        self.temperature = float(os.getenv("QWEN_TEMPERATURE", "0.9"))
        self.top_k = int(os.getenv("QWEN_TOP_K", "50"))
        self.top_p = float(os.getenv("QWEN_TOP_P", "1.0"))
        self.max_new_tokens = int(os.getenv("QWEN_MAX_NEW_TOKENS", "2048"))
        if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
            raise VoiceError("QWEN_CONFIG", "mount a complete local Qwen Base model directory")
        config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
        if (config.get("tts_model_type"), config.get("tts_model_size"), config.get("tokenizer_type")) != (
                "base", "1b7", "qwen3_tts_tokenizer_12hz"):
            raise VoiceError("QWEN_CONFIG", "expected Qwen3-TTS-12Hz-1.7B-Base")
        if not ref.is_file() or not self.ref_text:
            raise VoiceError("QWEN_CONFIG", "configure QWEN_REF_AUDIO and QWEN_REF_TEXT on the server")
        audition = os.getenv("QWEN_REFERENCE_MODE") == "audition" and os.getenv("APP_ENV") != "prod"
        if not audition and os.getenv("QWEN_REFERENCE_CONSENT") != "1":
            raise VoiceError("QWEN_CONFIG", "set QWEN_REFERENCE_CONSENT=1 for an authorized voice recording")
        if not 1 <= self.chunk_size <= 48:
            raise VoiceError("QWEN_CONFIG", "QWEN_CHUNK_SIZE must be 1..48")
        if not 0.0 < self.temperature <= 2.0:
            raise VoiceError("QWEN_CONFIG", "QWEN_TEMPERATURE must be in (0, 2]")
        if not 1 <= self.top_k <= 200:
            raise VoiceError("QWEN_CONFIG", "QWEN_TOP_K must be 1..200")
        if not 0.0 < self.top_p <= 1.0:
            raise VoiceError("QWEN_CONFIG", "QWEN_TOP_P must be in (0, 1]")
        if not 1 <= self.max_new_tokens <= 8192:
            raise VoiceError("QWEN_CONFIG", "QWEN_MAX_NEW_TOKENS must be 1..8192")
        self.ref_audio = str(ref.resolve())
        import torch
        from faster_qwen3_tts import FasterQwen3TTS
        self.model = FasterQwen3TTS.from_pretrained(
            str(model_dir.resolve()), device="cuda", dtype=torch.bfloat16,
            attn_implementation="sdpa", backend="torch", local_files_only=True)
        self.model.warmup(prefill_len=100)
        gen = self.generate("您好。")
        try:
            for _pcm in gen:
                pass
        finally:
            gen.close()
        self.ready, self.detail = True, "ready (Base ICL, audited clone defaults, PCM16/24000)"
        if audition:
            self.detail += "; audition reference, NOT production authorized"

    @property
    def generation_settings(self):
        return dict(language="Chinese", instruct=self.instruct, xvec_only=False,
                    chunk_size=self.chunk_size, non_streaming_mode=self.non_streaming_mode,
                    temperature=self.temperature, top_k=self.top_k, top_p=self.top_p,
                    max_new_tokens=self.max_new_tokens)

    def generate(self, text):
        import numpy as np
        gen = self.model.generate_voice_clone_streaming(
            text=text, ref_audio=self.ref_audio, ref_text=self.ref_text, **self.generation_settings)
        try:
            for samples, rate, timing in gen:
                if timing.get("total_steps_so_far", 0) >= self.max_new_tokens:
                    raise VoiceError("QWEN_OUTPUT_LIMIT", "generation reached token limit; split the text", False)
                if rate != self.sample_rate:
                    raise VoiceError("QWEN_FORMAT", f"model returned unexpected sample rate {rate}")
                pcm = (np.clip(np.asarray(samples).reshape(-1), -1, 1) * 32767).astype("<i2").tobytes()
                if pcm:
                    yield pcm
            # faster-qwen3-tts 0.5.4 can also stop at the static KV cache
            # boundary without EOS, before max_new_tokens. Do not call that
            # a successful synthesis. This is intentionally conservative at
            # the last usable position (even if EOS happens there).
            graph = self.model.talker_graph
            if int(graph.cache_position.item()) >= graph.max_seq_len - 2:
                raise VoiceError("QWEN_OUTPUT_LIMIT", "generation reached context limit; split the text", False)
        finally:
            gen.close()


def create_app(engine=None):
    engine = engine or QwenEngine()

    @asynccontextmanager
    async def lifespan(app):
        async def load():
            try:
                await asyncio.to_thread(engine.load)
            except Exception as exc:
                engine.ready, engine.detail = False, str(exc)
        loading = asyncio.create_task(load())
        yield
        await loading

    app = FastAPI(title="Qwen3 TTS sidecar", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/healthz")
    async def health():
        return JSONResponse({"ready": engine.ready, "detail": engine.detail,
                             "sample_rate": engine.sample_rate, "busy": engine.lock.locked(),
                             "generation": engine.generation_settings if engine.ready and isinstance(engine, QwenEngine) else None},
                            status_code=200 if engine.ready else 503)

    @app.websocket("/ws/tts")
    async def synthesize(ws: WebSocket):
        await ws.accept()
        cancel = CancelToken()
        watcher = None
        acquired = False
        handed_off = False
        try:
            request = await asyncio.wait_for(ws.receive_json(), 5)
            text = request.get("text") if isinstance(request, dict) else None
            if (not isinstance(text, str) or not text.strip() or len(text) > QWEN_MAX_TEXT_CHARS
                    or request.get("sample_rate", 24000) != engine.sample_rate):
                raise VoiceError("TTS_INVALID_REQUEST", "text must be 1..2000 chars, sample_rate=24000")
            if set(request) - {"text", "sample_rate"}:
                raise VoiceError("TTS_INVALID_REQUEST", "only text and sample_rate are accepted")
            if not engine.ready:
                raise VoiceError("QWEN_NOT_READY", engine.detail, True)
            acquired = engine.lock.acquire(blocking=False)
            if not acquired:
                raise VoiceError("TTS_BUSY", "GPU is already generating", True)

            async def watch_disconnect():
                try:
                    await ws.receive()
                finally:
                    cancel.cancel("SESSION_END")
            watcher = asyncio.create_task(watch_disconnect())

            def produce(emit, stop):
                gen = engine.generate(text)
                try:
                    for pcm in gen:
                        if stop.is_set() or not emit(pcm):
                            break
                finally:
                    gen.close()
                    engine.lock.release()

            await ws.send_json({"type": "started", "sample_rate": engine.sample_rate, "channels": 1})
            samples = 0
            handed_off = True
            async with aclosing(thread_stream(produce, cancel)) as stream:
                async for pcm in stream:
                    cancel.check()
                    await asyncio.wait_for(ws.send_bytes(pcm), 5)
                    samples += len(pcm) // 2
            if not samples:
                raise VoiceError("QWEN_EMPTY", "model generated no audio", True)
            await ws.send_json({"type": "completed", "samples": samples,
                                "total_ms": samples * 1000 / engine.sample_rate})
        except (WebSocketDisconnect, TurnCancelled):
            pass
        except Exception as exc:
            error = exc if isinstance(exc, VoiceError) else VoiceError("QWEN_FAILED", str(exc), True)
            try:
                await ws.send_json({"type": "error", "error": error.to_dict()})
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            cancel.cancel("SESSION_END")
            if acquired and not handed_off:
                engine.lock.release()
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
    return app


app = create_app()
