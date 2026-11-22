"""Fresh HTTP adapters. Secrets stay on the server; no inherited runtime code."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
import wave
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from .speech_start import first_sound
from .cache_audit import CacheAudit
from .enhanced_cache import EnhancedCacheManager
from .persistent_cache import PersistentCache


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    voice: str = "stella"
    predictions: bool = True
    forecast_branches: int = 5

    @classmethod
    def load(cls):
        # Keep this implementation independent from the parent app's .env.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        try:
            branches = int(os.getenv("PREDICTION_BRANCHES", "5"))
        except ValueError:
            logging.getLogger(__name__).warning("Invalid PREDICTION_BRANCHES; using 5")
            branches = 5
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-flash"),
            voice=os.getenv("DEEPSEEK_TTS_VOICE", "stella"),
            predictions=os.getenv("PREDICTION_ENABLED", "1") == "1",
            forecast_branches=max(1, min(branches, 5)),
        )


def wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


# TTS budget. Predicted replies are synthesised before anyone asks for them,
# so the cache holds several turns' worth of candidates: at 24 kHz mono an
# average sentence is ~350 KB and a forecast batch can reach 4 MB.
TTS_MEMORY_ENTRIES = 96
TTS_MEMORY_BYTES = 24_000_000
TTS_DISK_ENTRIES = 192
TTS_DISK_BYTES = 48_000_000


class Cloud:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None,
                 cache_path: str | Path | None = None,
                 on_cache_event: Callable[[str, bool, float, bool], None] | None = None,
                 cache_audit_path: str | Path | None = None):
        self.settings = settings
        self.forecast_slots = asyncio.Semaphore(3)
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(25, connect=8), trust_env=False,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        )
        self.owns_client = client is None
        self.on_cache_event = on_cache_event
        self.audit = CacheAudit(cache_audit_path or os.getenv("CACHE_AUDIT_PATH", "").strip())
        self.persistent = PersistentCache.for_live_calls(cache_path)
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        # One synthesis per text: a forecast batch often repeats a sentence.
        self.warm_tasks: dict[str, asyncio.Task] = {}
        self.cache_bytes = 0
        self.cache_manager = EnhancedCacheManager(self.persistent)
        # Last lookup result, useful to label a turn's LLM/ASR/TTS source.
        self.last_llm_cache_hit: bool | None = None
        self.last_asr_cache_hit: bool | None = None
        self.last_tts_cache_hit: bool | None = None
        if self.persistent is not None:
            for record in reversed(self.persistent.load(
                "tts", max_entries=TTS_MEMORY_ENTRIES, max_bytes=TTS_MEMORY_BYTES,
            )):
                self.cache[record.key] = record.value
                self.cache_bytes += len(record.value)

    @property
    def headers(self):
        if not self.settings.api_key:
            raise ProviderError("尚未配置 DEEPSEEK_API_KEY")
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    def emit_cache_event(self, cache_type: str, hit: bool, started: float,
                         interrupted: bool = False):
        """Record exactly one cache/network operation for a provider request.

        Metrics must never take down the call path, so a broken listener is
        swallowed after the provider has already produced or failed its result.
        """
        if cache_type == "llm":
            self.last_llm_cache_hit = hit
        elif cache_type == "tts":
            self.last_tts_cache_hit = hit
        elif cache_type == "asr":
            self.last_asr_cache_hit = hit
        if self.on_cache_event is None:
            return
        try:
            self.on_cache_event(cache_type, hit, (time.perf_counter() - started) * 1000,
                                interrupted)
        except Exception:  # noqa: BLE001 - observability must not break a call
            pass

    def check(self, response: httpx.Response, service: str):
        if response.status_code >= 400:
            # Error bodies can contain user data or credentials. Do not echo them.
            raise ProviderError(f"{service} 请求失败（HTTP {response.status_code}）")

    async def chat(self, messages: list[dict], *, max_tokens: int = 650,
                   use_cache: bool = True) -> AsyncIterator[str]:
        started = time.perf_counter()
        emitted = False
        hit = False
        try:
            temperature = 0.2
            cached_response = None
            if use_cache:
                cached_response = self.cache_manager.llm.get(
                    messages, self.settings.model, temperature, max_tokens=max_tokens,
                )
            hit = cached_response is not None
            # Expose the result before the first yielded chunk so the caller's
            # TTFT mark can label this turn's LLM source correctly.
            self.last_llm_cache_hit = hit
            if cached_response is not None:
                # Cache hit - simulate streaming from cache
                for i in range(0, len(cached_response), 50):
                    yield cached_response[i:i + 50]
                self.emit_cache_event("llm", True, started)
                emitted = True
                return

            # Cache miss - call API and collect response
            collected = []
            async with self.client.stream(
                "POST", self.settings.base_url + "/chat/completions", headers=self.headers,
                json={"model": self.settings.model, "messages": messages, "stream": True,
                      "temperature": temperature, "max_tokens": max_tokens, "reasoning_effort": "none"},
            ) as response:
                self.check(response, "对话模型")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                        choices = payload.get("choices") or []
                        if choices:
                            if choices[0].get("finish_reason") == "length":
                                raise ProviderError("模型输出达到长度限制")
                            content = choices[0].get("delta", {}).get("content")
                            if content:
                                collected.append(content)
                                yield content
                    except (KeyError, TypeError, json.JSONDecodeError) as exc:
                        raise ProviderError("模型返回了无法解析的数据") from exc

            if collected:
                full_response = "".join(collected)
                if use_cache:
                    self.cache_manager.llm.put(
                        messages, self.settings.model, temperature, full_response,
                        max_tokens=max_tokens,
                    )
                self.audit.llm(messages=messages, model=self.settings.model,
                               temperature=temperature, max_tokens=max_tokens,
                               response=full_response)
            self.emit_cache_event("llm", False, started)
            emitted = True
        finally:
            if not emitted:
                self.emit_cache_event("llm", hit, started, interrupted=True)

    async def transcribe(self, pcm: bytes) -> str:
        started = time.perf_counter()
        emitted = False
        try:
            # Check ASR cache first
            cached_text = self.cache_manager.asr.get(pcm)
            self.last_asr_cache_hit = cached_text is not None
            if cached_text is not None:
                self.emit_cache_event("asr", True, started)
                emitted = True
                return cached_text

            # Cache miss - call API
            response = await self.client.post(
                self.settings.base_url + "/v1/tools/asr", headers=self.headers,
                files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")},
            )
            self.check(response, "语音识别")
            try:
                text = response.json()["text"]
                if not isinstance(text, str):
                    raise TypeError()
                text = text.strip()

                # Cache the result
                self.cache_manager.asr.put(pcm, text)
                self.audit.asr(pcm=pcm, text=text)
                self.emit_cache_event("asr", False, started)
                emitted = True
                return text
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError("语音识别没有返回有效文本") from exc
        finally:
            if not emitted:
                self.emit_cache_event("asr", False, started, interrupted=True)

    def _tts_key(self, text: str) -> str:
        # Voice is part of the identity: a cached PCM must match its speaker.
        return f"{self.settings.voice}\0{text}"

    def _retain_tts(self, key: str, pcm: bytes):
        self.cache_bytes -= len(self.cache.pop(key, b""))
        if len(pcm) > TTS_MEMORY_BYTES or TTS_MEMORY_ENTRIES < 1:
            return
        self.cache[key] = pcm
        self.cache_bytes += len(pcm)
        while len(self.cache) > TTS_MEMORY_ENTRIES or self.cache_bytes > TTS_MEMORY_BYTES:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed)

    def cached(self, text: str) -> bytes | None:
        key = self._tts_key(text)
        pcm = self.cache.get(key)
        if pcm is not None:
            self.cache.move_to_end(key)
            if self.persistent is not None:
                self.persistent.touch("tts", key)
        elif self.persistent is not None:
            record = self.persistent.get("tts", key)
            if record is not None:
                pcm = record.value
                self._retain_tts(key, pcm)
        self.last_tts_cache_hit = pcm is not None
        return pcm

    def remember(self, text: str, pcm: bytes):
        if len(pcm) > 2_000_000:
            return
        key = self._tts_key(text)
        self._retain_tts(key, pcm)
        if self.persistent is not None:
            self.persistent.put(
                "tts", key, pcm,
                max_entries=TTS_DISK_ENTRIES, max_bytes=TTS_DISK_BYTES,
            )

    def cache_report(self) -> dict:
        report = self.cache_manager.report()
        report["tts"] = {
            "entries": len(self.cache),
            "size_mb": round(self.cache_bytes / (1024 * 1024), 2),
            "persistent": self.persistent is not None,
        }
        if self.persistent is not None:
            report["tts"]["disk"] = self.persistent.stats("tts")
        report["persistent"] = self.persistent is not None
        return report

    async def speech(self, text: str) -> AsyncIterator[bytes]:
        started = time.perf_counter()
        emitted = False
        hit = False
        try:
            cached = self.cached(text)
            hit = cached is not None
            if cached is not None:
                for offset in range(0, len(cached), 1920):
                    yield cached[offset:offset + 1920]
                self.emit_cache_event("tts", True, started)
                emitted = True
                return
            collected = bytearray()
            pending = bytearray()
            onset_found = False
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
                    pcm = pcm[first_sound(pcm) or 0:]
                    self.remember(text, pcm)
                    self.audit.tts(text=text, voice=self.settings.voice, pcm=pcm)
                    for offset in range(0, len(pcm), 1920):
                        yield pcm[offset:offset + 1920]
                    self.emit_cache_event("tts", False, started)
                    emitted = True
                    return
                if not ("audio/pcm" in kind or "application/octet-stream" in kind):
                    raise ProviderError("语音合成返回了不支持的音频格式")
                async for packet in response.aiter_bytes():
                    pending.extend(packet)
                    if not collected and pending.startswith(b'{"error"'):
                        raise ProviderError("语音合成返回了错误数据")
                    if len(collected) + len(pending) > 4_800_000:
                        raise ProviderError("单句音频超出长度限制")
                    if not onset_found:
                        start = first_sound(pending)
                        if start is None:
                            continue
                        del pending[:start]
                        onset_found = True
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
            self.audit.tts(text=text, voice=self.settings.voice, pcm=bytes(collected))
            self.emit_cache_event("tts", False, started)
            emitted = True
        finally:
            if not emitted:
                self.emit_cache_event("tts", hit, started, interrupted=True)

    async def prefetch(self, text: str) -> bytes:
        return b"".join([chunk async for chunk in self.speech(text)])

    async def warm(self, text: str) -> bytes | None:
        """Synthesise ``text`` once so its PCM lives in the caches.

        Forecasts call this for every predicted reply, including the candidates
        the conversation never uses: a later turn that reuses the text then
        starts from cached audio instead of a 700 ms synthesis. Failures are
        swallowed — a warm-up must never break a turn.
        """
        existing = self.warm_tasks.get(text)
        if existing is None or existing.done():
            existing = asyncio.create_task(self._warm_now(text))
            self.warm_tasks[text] = existing
            existing.add_done_callback(
                lambda task, key=text: self.warm_tasks.pop(key, None)
                if self.warm_tasks.get(key) is task else None)
        try:
            return await asyncio.shield(existing)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a warm-up is best effort
            return None

    async def plan_phone(self, card, *, experiences=()):
        from .phone_file import BasicResponse, CompiledMaterials, DependencyReview, PhoneFile, PLAN_PROMPT, REVIEW_PROMPT, model_json
        task = card.model_dump()
        prompt = PLAN_PROMPT
        if experiences:
            task["reviewed_experience"] = experiences
            prompt += (
                "\nreviewed_experience 是跨工单审核过的沟通约束。只采用符合 when 的条目。"
                "flow 约束体现到节点、前置条件、分支及完成依据；reply 约束体现到 guide 或相关节点的 action。"
                "工单原有事实、目标和必要检查点优先。经验只指导沟通，不提供本单事实、权限或承诺。"
            )

        async def compile_graph(instruction):
            materials = await model_json(self, instruction, task, CompiledMaterials, 4400, cache=False,
                                         allow_large_context=bool(experiences))
            file = PhoneFile(title=card.title, basic_response=BasicResponse(opening=card.opening, guide=materials.guide),
                             background=materials.background, checkpoints=card.checkpoints, graph=materials.graph,
                             source="model")
            return materials.graph, file

        try:
            graph, file = await compile_graph(prompt)
        except ValidationError as exc:
            issues = "。".join(f"{'.'.join(map(str, item['loc'])) or 'graph'} {item['msg']}"
                               for item in exc.errors(include_input=False, include_url=False)[:3])
            graph, file = await compile_graph(prompt + "\n上一次生成未通过校验。请重新生成完整 JSON，修正以下问题：" + issues)
        try:
            # Review is best effort once the first graph is already valid.
            async with asyncio.timeout(6):
                review_input = {"goal": card.goal, "graph": graph.model_dump()}
                review_prompt = REVIEW_PROMPT
                flow_rules = [rule for rule in experiences if rule["kind"] == "flow"]
                if flow_rules:
                    review_input["reviewed_experience"] = flow_rules
                    review_prompt += "\nreviewed_experience 只用于检查适用的流程情境，保留业务必要前置。"
                review = await model_json(self, review_prompt, review_input, DependencyReview, 650, cache=False,
                                          allow_large_context=bool(flow_rules))
            if review.changes or review.remove_merges:
                revised = review.revise(graph)
                return PhoneFile.model_validate({**file.model_dump(), "graph": revised})
        except (ValueError, ProviderError, httpx.HTTPError, OSError, TimeoutError):
            pass  # The original graph already passed structural validation.
        return file

    async def route_phone(self, flow, history):
        from .models import PLAYBOOK_GUIDANCE
        from .phone_file import Route, ROUTE_PROMPT, model_json
        prompt = ROUTE_PROMPT + ("\n" + PLAYBOOK_GUIDANCE if flow.playbook else "")
        return await model_json(self, prompt, {
            **flow.file.briefing(), "leaves": flow.file.graph.leaves(),
            "paths": {leaf: flow.path(leaf) for leaf in flow.file.graph.leaves()},
            "state": flow.context(), "dialogue": history[-10:], "latest_user": history[-1]["text"],
        }, Route, 350)

    async def forecast_clips(self, text):
        """Publish a separately voiced clause as soon as it is available."""
        from .speech_start import reusable_clause
        prefix = reusable_clause(text)
        full = self.cached(text)
        if full is not None:
            if prefix and prefix != text:
                first = self.cached(prefix)
                if first:
                    yield prefix, first
            yield text, full
            return
        if prefix and prefix != text:
            first = await self.warm(prefix)
            if first:
                yield prefix, first
                full = self.cached(text)
                if full:
                    yield text, full
                    return
                rest = await self.warm(text[len(prefix):])
                if rest:
                    yield text, first + rest
                    return
        full = await self.warm(text)
        if full:
            yield text, full

    async def _warm_now(self, text: str) -> bytes | None:
        cached = self.cached(text)
        if cached is not None:
            return cached
        try:
            async with self.forecast_slots:
                return await self.prefetch(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider errors are not fatal here
            return None

    async def close(self):
        if self.warm_tasks:
            for task in list(self.warm_tasks.values()):
                task.cancel()
            await asyncio.gather(*self.warm_tasks.values(), return_exceptions=True)
            self.warm_tasks.clear()
        try:
            if self.owns_client:
                await self.client.aclose()
        finally:
            if self.persistent is not None:
                self.persistent.close()
