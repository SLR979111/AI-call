"""Volcengine Seed-ASR 2.0 streaming adapter (bigmodel_async, 16k PCM)."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import struct
import uuid

import websockets

from .types import AsrEvent, AudioFrame, VoiceError

FULL_CLIENT = bytes([0x11, 0x10, 0x10, 0x00])
AUDIO_ONLY = bytes([0x11, 0x20, 0x00, 0x00])
AUDIO_FINAL = bytes([0x11, 0x22, 0x00, 0x00])


def _frame(header: bytes, payload: bytes) -> bytes:
    return header + struct.pack(">I", len(payload)) + payload


def _parse(data: bytes) -> dict:
    msg_type = data[1] >> 4
    flags = data[1] & 0x0F
    compression = data[2] & 0x0F
    off = 4
    seq = 0
    if flags in (0b0001, 0b0011) or msg_type == 0b1001:
        seq = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
    size = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    payload = data[off:off + size]
    if compression == 1:
        payload = gzip.decompress(payload)
    out = {"msg_type": msg_type, "flags": flags, "seq": seq}
    if msg_type == 0b1111:
        out["error"] = payload.decode("utf-8", "replace")
        return out
    try:
        out["json"] = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        out["raw"] = payload.decode("utf-8", "replace")
    return out


class VolcAsrStream:
    def __init__(self, endpoint: str, headers: dict[str, str], sample_rate: int = 16000) -> None:
        self.endpoint = endpoint
        self.headers = headers
        self.sample_rate = sample_rate
        self.audio: asyncio.Queue[AudioFrame | None] = asyncio.Queue(maxsize=128)
        self.events_q: asyncio.Queue[AsrEvent | None] = asyncio.Queue(maxsize=128)
        self.closed = False
        self.task: asyncio.Task | None = None
        self.last_partial = ""

    def _emit(self, event: AsrEvent | None) -> None:
        if self.events_q.full():
            try:
                self.events_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.events_q.put_nowait(event)

    async def _run(self) -> None:
        request = {
            "user": {"uid": "aicall"},
            "audio": {"format": "pcm", "rate": self.sample_rate, "bits": 16, "channel": 1},
            "request": {
                "model_name": "bigmodel",
                "enable_nonstream": False,
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
                "result_type": "single",
                "end_window_size": int(os.getenv("VOLC_ASR_END_WINDOW_MS", "800")),
                "force_to_speech_time": 1000,
            },
        }
        try:
            async with websockets.connect(
                self.endpoint, additional_headers=self.headers, max_size=10_000_000,
                open_timeout=10,
            ) as ws:
                await ws.send(_frame(FULL_CLIENT, json.dumps(request, ensure_ascii=False).encode()))

                async def sender() -> None:
                    while True:
                        item = await self.audio.get()
                        if item is None:
                            await ws.send(_frame(AUDIO_FINAL, b""))
                            return
                        if item.pcm:
                            await ws.send(_frame(AUDIO_ONLY, item.pcm))

                send_task = asyncio.create_task(sender())
                try:
                    async for raw in ws:
                        if isinstance(raw, str):
                            raw = raw.encode("latin-1")
                        msg = _parse(raw)
                        if "error" in msg:
                            raise VoiceError("VOLC_ASR_UPSTREAM", msg["error"], True)
                        payload = msg.get("json") or {}
                        result = payload.get("result") or {}
                        text = str(result.get("text") or "").strip()
                        utterances = result.get("utterances") or []
                        definite = bool(result.get("definite")) or any(
                            isinstance(u, dict) and u.get("definite") for u in utterances
                        )
                        if text and text != self.last_partial:
                            self.last_partial = text
                            self._emit(AsrEvent("partial", text))
                        if definite and text:
                            self._emit(AsrEvent("final", text))
                            self.last_partial = ""
                finally:
                    send_task.cancel()
                    await asyncio.gather(send_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(AsrEvent("error", error={
                "code": "VOLC_ASR_FAILED", "message": str(exc), "retryable": True,
            }))
        finally:
            self._emit(None)

    async def push(self, frame: AudioFrame) -> None:
        if self.closed:
            raise VoiceError("ASR_CLOSED", "ASR stream is closed")
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        await self.audio.put(frame)

    async def finish(self) -> None:
        if not self.closed:
            self.closed = True
            if self.task:
                await self.audio.put(None)

    async def events(self):
        while (event := await self.events_q.get()) is not None:
            yield event

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.closed = True


class VolcAsr:
    def __init__(self, *, api_key: str | None = None, resource: str | None = None,
                 endpoint: str | None = None) -> None:
        self.api_key = api_key or os.getenv("VOLC_ASR_API_KEY", "").strip()
        self.resource = resource or os.getenv(
            "VOLC_ASR_RESOURCE_ID", "volc.seedasr.sauc.duration"
        ).strip()
        self.endpoint = endpoint or os.getenv(
            "VOLC_ASR_ENDPOINT", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        ).strip()
        if not (self.api_key and self.resource):
            raise VoiceError("VOLC_ASR_CONFIG", "set VOLC_ASR_API_KEY / VOLC_ASR_RESOURCE_ID")

    async def start(self, session_id, *, sample_rate=16000, hotwords=None,
                    client_endpointing=False) -> VolcAsrStream:
        if sample_rate != 16000:
            raise VoiceError("ASR_CONFIG", "Volc ASR requires 16000 Hz")
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        return VolcAsrStream(self.endpoint, headers)

