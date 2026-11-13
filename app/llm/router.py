from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

import httpx

from ..events import EventBus


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_ms: int = 3000
    price_per_1k_cny: float = 0.002
    thinking: str | None = None


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cost_cny: float = 0.0
    error: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamChunk:
    """流式响应的单个 chunk。"""
    type: str  # "first_token" | "token" | "sentence" | "done" | "error"
    text: str = ""
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cost_cny: float = 0.0
    error: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""


# 中文句子边界标点符号
SENTENCE_TERMINATORS = {"。", "！", "？", "；", "\n"}
SENTENCE_SEPARATORS = {"，", "、"}


class LLMRouter:
    """OpenAI-compatible LLM client with primary/secondary failover.

    No key -> `LLM_NOT_CONFIGURED`; callers must fall back to template/rule.
    """

    def __init__(
        self,
        primary: ProviderConfig | None = None,
        secondary: ProviderConfig | None = None,
        *,
        bus: EventBus | None = None,
        daily_budget_cny: float = 100.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.primary = primary or self._from_env("PRIMARY")
        self.secondary = secondary or self._from_env("SECONDARY")
        self.bus = bus
        self.daily_budget_cny = daily_budget_cny
        self.spent_cny = 0.0
        self.transport = transport
        self._failures = 0
        self._open_until = 0.0

    @staticmethod
    def _from_env(prefix: str) -> ProviderConfig | None:
        base = os.getenv(f"LLM_{prefix}_BASE_URL", "").strip()
        key = os.getenv(f"LLM_{prefix}_API_KEY", "").strip()
        model = os.getenv(f"LLM_{prefix}_MODEL", "").strip()
        if not (base and key and model):
            return None
        return ProviderConfig(
            name=os.getenv(f"LLM_{prefix}_NAME", prefix.lower()),
            base_url=base.rstrip("/"),
            api_key=key,
            model=model,
            timeout_ms=int(os.getenv("LLM_TIMEOUT_MS", "3000")),
            price_per_1k_cny=float(os.getenv("LLM_PRICE_PER_1K_CNY", "0.002")),
            thinking=os.getenv(f"LLM_{prefix}_THINKING", "").strip() or None,
        )

    @property
    def configured(self) -> bool:
        return self.primary is not None or self.secondary is not None

    def _providers(self) -> list[ProviderConfig]:
        return [p for p in (self.primary, self.secondary) if p is not None]

    async def _emit(self, **kwargs: Any) -> None:
        if self.bus is not None:
            await self.bus.emit(**kwargs)

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        session_id: str = "s_internal",
        turn_id: str = "t_0000",
        trace_id: str | None = None,
        prompt_version: str = "agent_system@1",
    ) -> LLMResult:
        if self.spent_cny >= self.daily_budget_cny:
            await self._emit(
                session_id=session_id, type="BudgetExceeded", module="agent",
                turn_id=turn_id, trace_id=trace_id, ok=False,
                error={"code": "BUDGET_EXCEEDED", "message": "daily LLM budget exceeded", "retryable": False},
                payload={"scope": "llm", "limit": self.daily_budget_cny, "spent": self.spent_cny},
            )
            return LLMResult(False, error={"code": "BUDGET_EXCEEDED", "message": "daily budget exceeded",
                                           "retryable": False})
        providers = self._providers()
        if not providers:
            return LLMResult(False, error={"code": "LLM_NOT_CONFIGURED",
                                           "message": "set LLM_PRIMARY_* env vars", "retryable": False})

        last_error: dict[str, Any] | None = None
        for index, provider in enumerate(providers):
            if index > 0 and self._open_until > time.time():
                continue
            await self._emit(
                session_id=session_id, type="ProviderCalled", module="agent",
                turn_id=turn_id, trace_id=trace_id,
                payload={"provider": provider.name, "model": provider.model,
                         "request_id": f"req_{int(time.time()*1000)}_{index}",
                         "prompt_version": prompt_version},
            )
            start = time.perf_counter()
            try:
                payload: dict[str, Any] = {
                    "model": provider.model,
                    "messages": list(messages),
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format:
                    payload["response_format"] = response_format
                if provider.thinking:
                    payload["thinking"] = {"type": provider.thinking}
                if tools:
                    payload["tools"] = tools
                    if tool_choice:
                        payload["tool_choice"] = tool_choice
                headers = {"Authorization": f"Bearer {provider.api_key}",
                           "Content-Type": "application/json"}
                async with httpx.AsyncClient(transport=self.transport,
                                             timeout=provider.timeout_ms / 1000.0) as client:
                    resp = await client.post(f"{provider.base_url}/chat/completions",
                                             headers=headers, json=payload)
                latency = (time.perf_counter() - start) * 1000.0
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                data = resp.json()
                message = data["choices"][0]["message"]
                text = message.get("content") or ""
                tool_calls_data = message.get("tool_calls") or []
                usage = data.get("usage") or {}
                cost = (float(usage.get("total_tokens", 0)) / 1000.0) * provider.price_per_1k_cny
                self.spent_cny += cost
                self._failures = 0
                self._open_until = 0.0
                
                # Emit tool call event if tools were used
                if tool_calls_data:
                    await self._emit(
                        session_id=session_id, type="ProviderToolCall", module="agent",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"provider": provider.name, "model": provider.model,
                                "tool_calls": [{"name": tc["function"]["name"], "id": tc.get("id")}
                                             for tc in tool_calls_data]},
                    )
                await self._emit(
                    session_id=session_id, type="ProviderFirstToken", module="agent",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2),
                    payload={"provider": provider.name, "model": provider.model},
                )
                await self._emit(
                    session_id=session_id, type="ProviderReturned", module="agent",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2),
                    payload={"provider": provider.name, "model": provider.model,
                             "usage": usage, "cost_cny": round(cost, 6)},
                )
                return LLMResult(True, text=text, provider=provider.name, model=provider.model,
                                 latency_ms=round(latency, 2), usage=usage, cost_cny=round(cost, 6),
                                 tool_calls=tool_calls_data)
            except Exception as exc:
                latency = (time.perf_counter() - start) * 1000.0
                status = getattr(getattr(exc, "response", None), "status_code", None)
                code = "LLM_TIMEOUT" if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)) else "LLM_UPSTREAM"
                retryable = status in {429, 500, 502, 503, 504} or code == "LLM_TIMEOUT"
                last_error = {"code": code, "message": str(exc), "retryable": retryable,
                              "status": status}
                self._failures += 1
                if self._failures >= 3:
                    self._open_until = time.time() + 60
                await self._emit(
                    session_id=session_id, type="ProviderFailed", module="agent",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2), ok=False,
                    error=last_error, payload={"provider": provider.name, "model": provider.model},
                )
                if index + 1 < len(providers):
                    await self._emit(
                        session_id=session_id, type="ProviderFailover", module="agent",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"from": provider.name, "to": providers[index + 1].name,
                                 "reason": code},
                    )
        return LLMResult(False, error=last_error or {"code": "LLM_UNKNOWN", "message": "no provider",
                                                     "retryable": False})

    async def complete_stream(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        session_id: str = "s_internal",
        turn_id: str = "t_0000",
        trace_id: str | None = None,
        prompt_version: str = "agent_system@1",
    ) -> AsyncIterator[StreamChunk]:
        """流式返回 LLM 响应，首 token 到达后立即开始 yield。
        
        Yields:
            StreamChunk: type 可能是：
                - "first_token": 首个 token 到达（包含 latency_ms）
                - "token": 单个 token（包含 text）
                - "sentence": 完整句子（包含 text）
                - "done": 流式结束（包含 usage, cost_cny）
                - "error": 错误发生（包含 error）
        """
        if self.spent_cny >= self.daily_budget_cny:
            await self._emit(
                session_id=session_id, type="BudgetExceeded", module="agent",
                turn_id=turn_id, trace_id=trace_id, ok=False,
                error={"code": "BUDGET_EXCEEDED", "message": "daily LLM budget exceeded", "retryable": False},
                payload={"scope": "llm", "limit": self.daily_budget_cny, "spent": self.spent_cny},
            )
            yield StreamChunk(type="error", error={"code": "BUDGET_EXCEEDED", "message": "daily budget exceeded"})
            return
        
        providers = self._providers()
        if not providers:
            yield StreamChunk(type="error", error={"code": "LLM_NOT_CONFIGURED", "message": "set LLM_PRIMARY_* env vars"})
            return

        last_error: dict[str, Any] | None = None
        for index, provider in enumerate(providers):
            if index > 0 and self._open_until > time.time():
                continue
            
            await self._emit(
                session_id=session_id, type="ProviderCalled", module="agent",
                turn_id=turn_id, trace_id=trace_id,
                payload={"provider": provider.name, "model": provider.model,
                         "request_id": f"req_{int(time.time()*1000)}_{index}",
                         "prompt_version": prompt_version},
            )
            
            start = time.perf_counter()
            try:
                payload_dict: dict[str, Any] = {
                    "model": provider.model,
                    "messages": list(messages),
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,  # 启用流式
                }
                if response_format:
                    payload_dict["response_format"] = response_format
                if provider.thinking:
                    payload_dict["thinking"] = {"type": provider.thinking}
                if tools:
                    payload_dict["tools"] = tools
                    if tool_choice:
                        payload_dict["tool_choice"] = tool_choice
                
                headers = {"Authorization": f"Bearer {provider.api_key}",
                          "Content-Type": "application/json"}
                
                first_token_sent = False
                sentence_buffer = ""
                accumulated_text = ""
                usage_data: dict[str, Any] = {}
                # OpenAI streams tool calls as deltas keyed by index. Keep one
                # accumulator per index so fragmented function.arguments are
                # concatenated instead of overwritten by the last chunk.
                tool_call_acc: dict[int, dict[str, Any]] = {}
                
                async with httpx.AsyncClient(transport=self.transport,
                                            timeout=provider.timeout_ms / 1000.0) as client:
                    async with client.stream("POST", f"{provider.base_url}/chat/completions",
                                            headers=headers, json=payload_dict) as resp:
                        if resp.status_code >= 400:
                            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", 
                                                       request=resp._request, response=resp)
                        
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            
                            if not line.startswith("data: "):
                                continue
                            
                            if line == "data: [DONE]":
                                break
                            
                            try:
                                chunk_data = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            
                            choices = chunk_data.get("choices") or []
                            if not choices:
                                # Ark/OpenAI-compatible providers send a final
                                # usage-only chunk with choices=[].
                                if "usage" in chunk_data:
                                    usage_data = chunk_data.get("usage") or {}
                                continue
                            choice = choices[0] or {}
                            delta = choice.get("delta") or {}
                            finish_reason = choice.get("finish_reason")
                            
                            # 首 token 计时
                            if not first_token_sent and delta:
                                latency = (time.perf_counter() - start) * 1000.0
                                await self._emit(
                                    session_id=session_id, type="ProviderFirstToken", module="agent",
                                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2),
                                    payload={"provider": provider.name, "model": provider.model},
                                )
                                yield StreamChunk(type="first_token", latency_ms=round(latency, 2),
                                                provider=provider.name, model=provider.model)
                                first_token_sent = True
                            
                            # 处理 content
                            if "content" in delta and delta["content"]:
                                text = delta["content"]
                                accumulated_text += text
                                sentence_buffer += text
                                
                                # Token 流式返回
                                yield StreamChunk(type="token", text=text,
                                                provider=provider.name, model=provider.model)
                                
                                # 句子边界检测
                                if self._detect_sentence_boundary(sentence_buffer):
                                    yield StreamChunk(type="sentence", text=sentence_buffer.strip(),
                                                    provider=provider.name, model=provider.model)
                                    sentence_buffer = ""
                            
                            # 处理 tool_calls
                            if "tool_calls" in delta:
                                for position, tc in enumerate(delta["tool_calls"] or []):
                                    if not isinstance(tc, dict):
                                        continue
                                    index = tc.get("index", position)
                                    if not isinstance(index, int):
                                        index = position
                                    item = tool_call_acc.setdefault(index, {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    })
                                    if tc.get("id"):
                                        item["id"] = tc["id"]
                                    if tc.get("type"):
                                        item["type"] = tc["type"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        item["function"]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        item["function"]["arguments"] += fn["arguments"]
                            
                            # 处理 usage（某些 provider 在最后一个 chunk 返回）
                            if "usage" in chunk_data:
                                usage_data = chunk_data.get("usage") or {}
                            
                            # 结束标志
                            if finish_reason:
                                break
                
                # 处理剩余的句子缓冲
                if sentence_buffer.strip():
                    yield StreamChunk(type="sentence", text=sentence_buffer.strip(),
                                    provider=provider.name, model=provider.model)
                
                # 计算成本
                cost = (float(usage_data.get("total_tokens", 0)) / 1000.0) * provider.price_per_1k_cny
                self.spent_cny += cost
                self._failures = 0
                self._open_until = 0.0
                tool_calls_data = [tool_call_acc[i] for i in sorted(tool_call_acc)]
                
                # Emit tool call event
                if tool_calls_data:
                    await self._emit(
                        session_id=session_id, type="ProviderToolCall", module="agent",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"provider": provider.name, "model": provider.model,
                                "tool_calls": [{"name": tc["function"]["name"], "id": tc.get("id")}
                                             for tc in tool_calls_data]},
                    )
                
                latency = (time.perf_counter() - start) * 1000.0
                await self._emit(
                    session_id=session_id, type="ProviderReturned", module="agent",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2),
                    payload={"provider": provider.name, "model": provider.model,
                             "usage": usage_data, "cost_cny": round(cost, 6)},
                )
                
                # 流式结束
                yield StreamChunk(type="done", text=accumulated_text, 
                                usage=usage_data, cost_cny=round(cost, 6),
                                tool_calls=tool_calls_data,
                                provider=provider.name, model=provider.model)
                return
                
            except Exception as exc:
                latency = (time.perf_counter() - start) * 1000.0
                status = getattr(getattr(exc, "response", None), "status_code", None)
                code = "LLM_TIMEOUT" if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)) else "LLM_UPSTREAM"
                retryable = status in {429, 500, 502, 503, 504} or code == "LLM_TIMEOUT"
                last_error = {"code": code, "message": str(exc), "retryable": retryable, "status": status}
                self._failures += 1
                if self._failures >= 3:
                    self._open_until = time.time() + 60
                
                await self._emit(
                    session_id=session_id, type="ProviderFailed", module="agent",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2), ok=False,
                    error=last_error, payload={"provider": provider.name, "model": provider.model},
                )
                
                if index + 1 < len(providers):
                    await self._emit(
                        session_id=session_id, type="ProviderFailover", module="agent",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"from": provider.name, "to": providers[index + 1].name, "reason": code},
                    )
                    continue
                else:
                    yield StreamChunk(type="error", error=last_error,
                                    provider=provider.name, model=provider.model)
                    return
        
        # 所有 provider 都失败
        yield StreamChunk(type="error", error=last_error or {"code": "LLM_UNKNOWN", "message": "no provider"})
    
    @staticmethod
    def _detect_sentence_boundary(text: str) -> bool:
        """检测句子边界。"""
        if not text:
            return False
        
        # 1. 完整句标点
        if any(t in text for t in SENTENCE_TERMINATORS):
            return True
        
        # 2. 逗号 + 足够长度（避免过短分句）
        if any(s in text for s in SENTENCE_SEPARATORS) and len(text) >= 15:
            return True
        
        return False


def messages(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from an LLM reply."""
    text = text.strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None
