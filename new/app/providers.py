"""Fresh HTTP adapters. Secrets stay on the server; no inherited runtime code."""

from __future__ import annotations

import base64
import io
import json
import os
import wave
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    voice: str = "stella"
    predictions: bool = True

    @classmethod
    def load(cls):
        # Keep this implementation independent from the parent app's .env.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-flash"),
            voice=os.getenv("DEEPSEEK_TTS_VOICE", "stella"),
            predictions=os.getenv("PREDICTION_ENABLED", "1") == "1",
        )


def wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


class Cloud:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(25, connect=8), trust_env=False,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        )
        self.owns_client = client is None
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        self.cache_bytes = 0

    @property
    def headers(self):
        if not self.settings.api_key:
            raise ProviderError("尚未配置 DEEPSEEK_API_KEY")
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    def check(self, response: httpx.Response, service: str):
        if response.status_code >= 400:
            # Error bodies can contain user data or credentials. Do not echo them.
            raise ProviderError(f"{service} 请求失败（HTTP {response.status_code}）")

    async def chat(self, messages: list[dict], *, max_tokens: int = 650) -> AsyncIterator[str]:
        async with self.client.stream(
            "POST", self.settings.base_url + "/chat/completions", headers=self.headers,
            json={"model": self.settings.model, "messages": messages, "stream": True,
                  "temperature": 0.2, "max_tokens": max_tokens, "reasoning_effort": "none"},
        ) as response:
            self.check(response, "对话模型")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                    choices = payload.get("choices") or []
                    if choices:
                        if choices[0].get("finish_reason") == "length":
                            raise ProviderError("模型输出达到长度限制")
                        content = choices[0].get("delta", {}).get("content")
                        if content:
                            yield content
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ProviderError("模型返回了无法解析的数据") from exc

    async def transcribe(self, pcm: bytes) -> str:
        response = await self.client.post(
            self.settings.base_url + "/v1/tools/asr", headers=self.headers,
            files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")},
        )
        self.check(response, "语音识别")
        try:
            text = response.json()["text"]
            if not isinstance(text, str):
                raise TypeError()
            return text.strip()
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("语音识别没有返回有效文本") from exc

    def cached(self, text: str) -> bytes | None:
        pcm = self.cache.get(text)
        if pcm is not None:
            self.cache.move_to_end(text)
        return pcm

    def remember(self, text: str, pcm: bytes):
        if len(pcm) > 2_000_000:
            return
        previous = self.cache.pop(text, b"")
        self.cache_bytes += len(pcm) - len(previous)
        self.cache[text] = pcm
        while len(self.cache) > 48 or self.cache_bytes > 12_000_000:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed)

    async def speech(self, text: str) -> AsyncIterator[bytes]:
        cached = self.cached(text)
        if cached is not None:
            for offset in range(0, len(cached), 1920):
                yield cached[offset:offset + 1920]
            return
        collected = bytearray()
        pending = bytearray()
        async with self.client.stream(
            "POST", self.settings.base_url + "/v1/tools/tts", headers=self.headers,
            json={"text": text, "voice_id": self.settings.voice, "format": "pcm"},
        ) as response:
            self.check(response, "语音合成")
            kind = response.headers.get("content-type", "").lower()
            if "json" in kind:
                body = await response.aread()
                try:
                    payload = json.loads(body)
                    value = payload.get("audio") or payload.get("audio_base64") or payload.get("data")
                    pcm = base64.b64decode(value, validate=True)
                    if not pcm or len(pcm) % 2:
                        raise ValueError()
                except (ValueError, TypeError) as exc:
                    raise ProviderError("语音合成没有返回 PCM 音频") from exc
                self.remember(text, pcm)
                for offset in range(0, len(pcm), 1920):
                    yield pcm[offset:offset + 1920]
                return
            if not ("audio/pcm" in kind or "application/octet-stream" in kind):
                raise ProviderError("语音合成返回了不支持的音频格式")
            async for packet in response.aiter_bytes():
                pending.extend(packet)
                if not collected and pending.startswith(b'{"error"'):
                    raise ProviderError("语音合成返回了错误数据")
                if len(collected) + len(pending) > 4_800_000:
                    raise ProviderError("单句音频超出长度限制")
                while len(pending) >= 1920:
                    chunk = bytes(pending[:1920])
                    del pending[:1920]
                    collected.extend(chunk)
                    yield chunk
            if len(pending) % 2:
                raise ProviderError("语音合成的音频不完整")
            if pending:
                collected.extend(pending)
                yield bytes(pending)
        if not collected:
            raise ProviderError("语音合成返回了空音频")
        self.remember(text, bytes(collected))

    async def prefetch(self, text: str):
        async for _ in self.speech(text):
            pass

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
