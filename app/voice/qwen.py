"""Network-only Qwen client; torch is confined to the GPU sidecar."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

from app.cancel import TurnCancelled
from .streaming import cancellable
from .types import AudioChunk, VoiceError, mono_ms, QWEN_MAX_TEXT_CHARS


class QwenTts:
    sample_rate = 24000

    def __init__(self, url: str):
        parsed = urlsplit(url.rstrip("/"))
        if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
            raise VoiceError("QWEN_CONFIG", "QWEN_TTS_URL must be an HTTP/WS base URL")
        secure = parsed.scheme in ("https", "wss")
        self.url = urlunsplit(("wss" if secure else "ws", parsed.netloc, parsed.path + "/ws/tts", "", ""))
        self.health_url = urlunsplit(("https" if secure else "http", parsed.netloc, parsed.path + "/healthz", "", ""))
        self.timeout = float(os.getenv("QWEN_TTS_TIMEOUT_S", "30"))

    async def health(self):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(self.health_url)
                data = response.json()
                return bool(response.is_success and data.get("ready")), data.get("detail", "not ready")
        except Exception as exc:
            return False, f"Qwen sidecar unavailable: {str(exc) or type(exc).__name__}"

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        import websockets
        if sample_rate != self.sample_rate or voice != "default":
            raise VoiceError("TTS_CONFIG", "Qwen supports the configured voice at 24000 Hz")
        cancel.check()
        if not isinstance(text, str) or not text.strip() or len(text) > QWEN_MAX_TEXT_CHARS:
            raise VoiceError("TTS_INVALID_REQUEST", f"text must be 1..{QWEN_MAX_TEXT_CHARS} chars; split at sentence boundaries")
        ws = None
        connection = None
        try:
            import asyncio
            connection = asyncio.ensure_future(websockets.connect(
                self.url, open_timeout=5, close_timeout=0.5, max_size=2**20, max_queue=8))
            ws = await cancellable(connection, cancel, 5)
            await cancellable(ws.send(json.dumps({"text": text, "sample_rate": sample_rate})), cancel, 5)
            started, seq, samples = False, 0, 0
            while True:
                message = await cancellable(ws.recv(), cancel, self.timeout)
                if isinstance(message, bytes):
                    if not started or not message or len(message) % 2:
                        raise VoiceError("QWEN_PROTOCOL", "invalid PCM or missing started frame")
                    samples += len(message) // 2
                    yield AudioChunk(message, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                    seq += 1
                    continue
                data = json.loads(message)
                if data.get("type") == "error":
                    err = data.get("error", {})
                    raise VoiceError(err.get("code", "QWEN_FAILED"), err.get("message", "Qwen failed"),
                                     err.get("retryable", False))
                if data.get("type") == "started" and not started:
                    if data.get("sample_rate") != sample_rate or data.get("channels") != 1:
                        raise VoiceError("QWEN_PROTOCOL", "unexpected sidecar audio format")
                    started = True
                elif data.get("type") == "completed" and started:
                    if not samples or data.get("samples") != samples:
                        raise VoiceError("QWEN_PROTOCOL", "empty or truncated synthesis")
                    yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                    return
                else:
                    raise VoiceError("QWEN_PROTOCOL", "unexpected sidecar message")
        except (VoiceError, TurnCancelled):
            raise
        except Exception as exc:
            raise VoiceError("QWEN_UNAVAILABLE", str(exc) or type(exc).__name__, True) from exc
        finally:
            # Cancellation may win exactly as the handshake finishes.
            if ws is None and connection is not None and connection.done() and not connection.cancelled():
                if connection.exception() is None:
                    ws = connection.result()
            if ws is not None:
                await ws.close()
