from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

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
                text = data["choices"][0]["message"].get("content") or ""
                usage = data.get("usage") or {}
                cost = (float(usage.get("total_tokens", 0)) / 1000.0) * provider.price_per_1k_cny
                self.spent_cny += cost
                self._failures = 0
                self._open_until = 0.0
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
                                 latency_ms=round(latency, 2), usage=usage, cost_cny=round(cost, 6))
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
