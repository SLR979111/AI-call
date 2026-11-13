"""Volcengine bidirectional TTS with a reusable WebSocket connection.

The unidirectional endpoint closes after one request. This adapter keeps one
connection and opens a StartSession/FinishSession pair per synthesized text.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid

import websockets

from ..cancel import TurnCancelled
from .types import AudioChunk, VoiceError, mono_ms

FULL_CLIENT = bytes([0x11, 0x14, 0x10, 0x00])
EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_SESSION_STARTED = 150
EV_SESSION_FAILED = 153
EV_SESSION_FINISHED = 152
EV_TTS_RESPONSE = 352
EV_ERROR = 0


def _read_content(data: bytes, off: int) -> tuple[str, int]:
    size = struct.unpack(">i", data[off:off + 4])[0]
    off += 4
    text = data[off:off + size].decode("utf-8", "replace")
    return text, off + size


def _read_payload(data: bytes, off: int) -> tuple[bytes, int]:
    size = struct.unpack(">i", data[off:off + 4])[0]
    off += 4
    return data[off:off + size], off + size


def _parse(data: bytes) -> dict:
    msg_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    off = 4
    event = 0
    payload = b""
    if msg_type in (0b1001, 0b1011) and flags & 0b0100:
        event = struct.unpack(">i", data[off:off + 4])[0]
        off += 4
        if event == 50:
            _, off = _read_content(data, off)
        elif event == 51:
            _, off = _read_content(data, off)
        elif event in (150, 152, 153):
            _, off = _read_content(data, off)
            _, off = _read_content(data, off)
        else:
            _, off = _read_content(data, off)
            payload, off = _read_payload(data, off)
    elif msg_type == 0b1111:
        code = struct.unpack(">i", data[off:off + 4])[0]
        off += 4
        payload, off = _read_payload(data, off)
        return {"error": code, "message": payload.decode("utf-8", "replace")}
    return {"event": event, "payload": payload, "msg_type": msg_type}


def _optional(event: int, session_id: str) -> bytes:
    sid = session_id.encode()
    return struct.pack(">i", event) + struct.pack(">i", len(sid)) + sid


def _msg(event: int, session_id: str, body: dict) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode()
    return FULL_CLIENT + _optional(event, session_id) + struct.pack(">i", len(payload)) + payload


class VolcBidirectionalTts:
    sample_rate = 24000

    def __init__(self) -> None:
        self.api_key = os.getenv("VOLC_TTS_API_KEY", "").strip()
        self.resource = os.getenv("VOLC_TTS_RESOURCE_ID", "seed-tts-2.0").strip()
        self.speaker = os.getenv("VOLC_TTS_SPEAKER", "").strip()
        self.endpoint = os.getenv(
            "VOLC_TTS_BIDIRECTIONAL_ENDPOINT",
            "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
        ).strip()
        if not (self.api_key and self.resource and self.speaker):
            raise VoiceError("VOLC_TTS_CONFIG", "set VOLC_TTS_API_KEY / RESOURCE_ID / SPEAKER")
        self._ws = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        if self._ws is not None:
            return self._ws
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        self._ws = await websockets.connect(
            self.endpoint, additional_headers=headers, max_size=10_000_000, open_timeout=10
        )
        return self._ws

    def _body(self, event: int, text: str, rate: int) -> dict:
        return {
            "user": {"uid": "aicall"},
            "event": event,
            "namespace": "BidirectionalTTS",
            "req_params": {
                "text": text,
                "speaker": self.speaker,
                "audio_params": {"format": "pcm", "sample_rate": rate},
            },
        }

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if voice != "default":
            raise VoiceError("VOLC_TTS_CONFIG", "unsupported voice")
        async with self._lock:
            session_id = uuid.uuid4().hex
            try:
                ws = await self._connect()
                await ws.send(_msg(EV_START_SESSION, session_id, self._body(EV_START_SESSION, "", sample_rate)))
                await ws.send(_msg(200, session_id, self._body(200, text, sample_rate)))
                await ws.send(_msg(EV_FINISH_SESSION, session_id, {"user": {"uid": "aicall"}}))
                seq = 0
                received = 0
                while True:
                    cancel.check()
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    msg = _parse(bytes(raw))
                    if "error" in msg:
                        raise VoiceError("VOLC_TTS_UPSTREAM", msg["message"], True)
                    event = msg.get("event", 0)
                    if event in (EV_SESSION_FAILED, EV_ERROR):
                        raise VoiceError("VOLC_TTS_UPSTREAM", "session failed", True)
                    if event == EV_TTS_RESPONSE and msg.get("payload"):
                        pcm = msg["payload"]
                        received += len(pcm)
                        yield AudioChunk(pcm, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                        seq += 1
                    if event == EV_SESSION_FINISHED:
                        break
                cancel.check()
                if not received:
                    raise VoiceError("VOLC_TTS_EMPTY", "no audio", True)
                yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
            except (VoiceError, TurnCancelled):
                raise
            except Exception as exc:
                await self._drop()
                raise VoiceError("VOLC_TTS_FAILED", str(exc), True) from exc

    async def _drop(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
