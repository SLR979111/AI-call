"""Use the G2 speech providers with the contest WebRTC transport."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace
from os import getenv

import httpx

from new.app.local_asr import load_from_env, make_transcriber
from new.app.providers import Cloud, ProviderError, Settings

from .speech_endpoint import SpeechEndpoint
from .types import AsrEvent, AudioChunk, VoiceError, mono_ms


def load_g2_settings():
    settings = Settings.load()
    if not settings.api_key and getenv("POP_JUDGE_API_KEY"):
        settings = replace(settings, api_key=getenv("POP_JUDGE_API_KEY"),
                           base_url=getenv("POP_JUDGE_BASE_URL", settings.base_url).rstrip("/").removesuffix("/v1"))
    if not settings.api_key:
        raise ValueError("Configure the original Demo's DeepSeek provider")
    return settings


async def build_g2_voice(settings, cache_path):
    cloud = Cloud(settings, cache_path=cache_path)
    local = load_from_env()
    if local is not None:
        try:
            await asyncio.to_thread(local.load)
        except Exception:
            local = None
    name = "sensevoice/deepseek" if local else "deepseek"
    return cloud, G2Asr(make_transcriber(local, cloud)), G2Tts(cloud), name


class G2Tts:
    sample_rate = 24000

    def __init__(self, cloud):
        self.cloud = cloud

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if sample_rate != self.sample_rate or voice not in {"default", self.cloud.settings.voice}:
            raise VoiceError("TTS_CONFIG", "G2 expects its configured voice at 24 kHz")
        seq = 0
        cancel.check()
        try:
            async with aclosing(self.cloud.speech(text)) as stream:
                async for pcm in stream:
                    cancel.check()
                    yield AudioChunk(pcm, seq, ts_mono_ms=mono_ms(), sample_rate=sample_rate)
                    seq += 1
        except (ProviderError, httpx.HTTPError) as exc:
            raise VoiceError("TTS_FAILED", "G2 speech synthesis failed", retryable=True) from exc
        yield AudioChunk(b"", seq, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)


class G2Asr:
    def __init__(self, transcribe):
        self.transcribe = transcribe

    async def start(self, session_id, *, sample_rate=16000, hotwords=None, client_endpointing=False,
                    server_silence_ms=None):
        if sample_rate != 16000 or hotwords:
            raise VoiceError("ASR_CONFIG", "G2 expects 16 kHz PCM")
        endpoint = None
        if server_silence_ms is not None:
            from new.app.vad import SpeechDetector

            endpoint = SpeechEndpoint(SpeechDetector(), server_silence_ms)
        return G2AsrStream(self.transcribe, endpoint)


class G2AsrStream:
    def __init__(self, transcribe, endpoint=None):
        self.transcribe = transcribe
        self.frames = bytearray()
        self.queue = asyncio.Queue()
        self.closed = False
        self.overflow = False
        self.endpoint = endpoint

    async def push(self, frame):
        if self.closed:
            return
        if len(self.frames) + len(frame.pcm) > 16000 * 2 * 30:
            self.overflow = True
            return
        if self.endpoint:
            self.endpoint.feed(frame, self.frames)
        else:
            self.frames.extend(frame.pcm)

    async def finish(self):
        if self.closed:
            return
        try:
            if self.overflow:
                raise VoiceError("ASR_TOO_LONG", "请分成两句话再说。", retryable=True)
            clip = self.endpoint.clip(self.frames) if self.endpoint else bytes(self.frames)
            if clip:
                text = await self.transcribe(clip)
                self.queue.put_nowait(AsrEvent("final", text=text, audio_end_ms=len(clip) // 32))
        except VoiceError as exc:
            self.queue.put_nowait(AsrEvent("error", error=exc.to_dict()))
        except (ProviderError, httpx.HTTPError):
            error = VoiceError("ASR_FAILED", "这次没能识别，请再说一遍。", retryable=True)
            self.queue.put_nowait(AsrEvent("error", error=error.to_dict()))
        finally:
            await self.close()

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self):
        if not self.closed:
            self.closed = True
            self.frames.clear()
            self.queue.put_nowait(None)
