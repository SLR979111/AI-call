"""Volcengine Seed-TTS 2.0 unidirectional streaming adapter.

Protocol (v3, PCM):
  header 11 10 10 00
  uint32 BE payload length
  JSON {user, req_params:{text,speaker,audio_params}}
Response frames carry event numbers; event 352 is PCM audio, 152 finishes.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid
from contextlib import aclosing

import websockets

from ..cancel import TurnCancelled
from .types import AudioChunk, VoiceError, mono_ms

HDR = bytes([0x11, 0x10, 0x10, 0x00])
EVT_TTS_START = 350
EVT_TTS_END = 351
EVT_TTS_AUDIO = 352
EVT_SESSION_FINISHED = 152


def _parse_frame(data: bytes) -> dict:
    msg_type = data[1] >> 4
    off = 4
    if msg_type == 0b1111:
        code = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        size = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        return {"error": code, "message": data[off:off + size].decode("utf-8", "replace")}
    event = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    sid_len = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    off += sid_len
    size = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    payload = data[off:off + size]
    out = {"event": event, "msg_type": msg_type}
    if msg_type == 0b1011:
        out["audio"] = payload
    else:
        try:
            out["json"] = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            out["raw"] = payload.decode("utf-8", "replace")
    return out


class VolcTts:
    def __init__(self, *, api_key: str | None = None, resource: str | None = None,
                 speaker: str | None = None, endpoint: str | None = None,
                 default_rate: int = 24000) -> None:
        self.api_key = api_key or os.getenv("VOLC_TTS_API_KEY", "").strip()
        self.resource = resource or os.getenv("VOLC_TTS_RESOURCE_ID", "seed-tts-2.0").strip()
        self.speaker = speaker or os.getenv("VOLC_TTS_SPEAKER", "").strip()
        self.endpoint = endpoint or os.getenv(
            "VOLC_TTS_ENDPOINT",
            "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream",
        ).strip()
        self.sample_rate = default_rate
        if not (self.api_key and self.resource and self.speaker):
            raise VoiceError(
                "VOLC_TTS_CONFIG",
                "set VOLC_TTS_API_KEY / VOLC_TTS_RESOURCE_ID / VOLC_TTS_SPEAKER",
            )

    def _request(self, text: str, rate: int) -> bytes:
        body = {
            "user": {"uid": "aicall"},
            "req_params": {
                "text": text,
                "speaker": self.speaker,
                "audio_params": {"format": "pcm", "sample_rate": rate},
            },
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return HDR + struct.pack(">I", len(payload)) + payload

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if voice != "default" or sample_rate not in (8000, 16000, 22050, 24000, 32000, 44100, 48000):
            raise VoiceError("VOLC_TTS_CONFIG", "unsupported voice or sample rate")
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Control-Require-Usage-Tokens-Return": "*",
        }
        seq = 0
        received = 0
        try:
            async with websockets.connect(
                self.endpoint, additional_headers=headers, max_size=10_000_000, open_timeout=10,
            ) as ws:
                await ws.send(self._request(text, sample_rate))
                async for raw in ws:
                    cancel.check()
                    if isinstance(raw, str):
                        raw = raw.encode("latin-1")
                    msg = _parse_frame(raw)
                    if "error" in msg:
                        raise VoiceError("VOLC_TTS_UPSTREAM", f"{msg['error']}: {msg['message']}", True)
                    event = msg.get("event")
                    if event == EVT_TTS_AUDIO:
                        pcm = msg.get("audio", b"")
                        if pcm:
                            received += len(pcm)
                            yield AudioChunk(pcm, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                            seq += 1
                    elif event == EVT_SESSION_FINISHED:
                        status = (msg.get("json") or {}).get("status_code")
                        if status not in (None, 20000000):
                            raise VoiceError("VOLC_TTS_UPSTREAM", f"session status {status}", True)
                        break
            cancel.check()
            if not received:
                raise VoiceError("VOLC_TTS_EMPTY", "provider returned no audio", True)
            yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
        except (VoiceError, TurnCancelled):
            raise
        except Exception as exc:
            raise VoiceError("VOLC_TTS_FAILED", str(exc), True) from exc
