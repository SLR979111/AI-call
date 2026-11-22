"""Short candidate-selection model used after the lexical rules are unsure.

It only chooses a forecast id or asks for a fresh reply. It never writes a new
reply, so the candidate text/audio pair stays intact.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any

import httpx

from .decision_relevance import worth_sending
from .models import Candidate
from .providers import Cloud, ProviderError

SYSTEM = """你是候选回复选择器。只判断候选回复是否已经完整、直接地回答了用户本轮这句话。
只输出一个 JSON：
{"decision":"reuse","id":"候选id"}
或
{"decision":"generate"}
不要输出新回复，不要输出解释，不要输出 Markdown。
候选只要回答了用户这句话的核心问题，即使措辞与候选的 examples 不完全一样，也应输出 reuse。
不要因为候选措辞不同、比模型想说的更短或更长就 generate。
只有以下情况才输出 generate：
- 用户提出了新问题
- 候选遗漏了用户明确问到的关键一项
- 用户明确要求重新回答
- 所有候选都不适用"""


class DecisionModel:
    """A short, separate model request. It is deliberately last after rules."""

    def __init__(self, cloud: Cloud, *, timeout: float = 0.9):
        self.cloud = cloud
        self.timeout = timeout

    @classmethod
    def from_env(cls, cloud):
        if os.getenv("DECISION_ENABLED", "0") != "1" or getattr(cloud, "is_local", False):
            return None
        try:
            timeout = float(os.getenv("DECISION_TIMEOUT_MS", "900")) / 1000
        except ValueError:
            return None
        return cls(cloud, timeout=timeout) if math.isfinite(timeout) and timeout > 0 else None

    @staticmethod
    def _payload(user_input: str, assistant_last: str, candidates: list[Candidate]) -> dict[str, Any]:
        return {
            "user": user_input,
            "previous_assistant": assistant_last,
            "candidates": [
                {"id": c.id, "when": c.when, "examples": c.examples,
                 "reply": c.reply.text}
                for c in candidates
            ],
        }

    async def decide(self, user_input: str, assistant_last: str,
                     candidates: list[Candidate]) -> Candidate | None:
        if not candidates:
            return None
        if not worth_sending(user_input, candidates):
            return None
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(
                self._payload(user_input, assistant_last, candidates),
                ensure_ascii=False)},
        ]
        async def run():
            chunks = []
            async for chunk in self.cloud.chat(messages, max_tokens=60):
                chunks.append(chunk)
            text = "".join(chunks).strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return None
            if not isinstance(data, dict) or data.get("decision") != "reuse":
                return None
            wanted = data.get("id")
            return next((c for c in candidates if c.id == wanted), None)
        try:
            return await asyncio.wait_for(run(), timeout=self.timeout)
        except (TimeoutError, ProviderError, httpx.HTTPError, OSError, ValueError):
            return None
