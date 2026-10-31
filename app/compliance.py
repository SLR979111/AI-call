from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

ALLOW = "ALLOW"
REWRITE = "REWRITE"
BLOCK = "BLOCK"
TRANSFER = "TRANSFER"


@dataclass
class GuardResult:
    decision: str
    text: str
    rule_ids: list[str] = field(default_factory=list)
    severity: str = "info"
    latency_ms: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


class ComplianceGuard:
    """Three-point guard: input, sentence, tool.

    Rules are intentionally small for the fake skeleton. They will be loaded
    from rules.yaml in the next iteration; the interface stays the same.
    """

    def __init__(self) -> None:
        self.forbidden = [
            ("false_promise", re.compile(r"保证|一定|绝对|百分百|最低价"), REWRITE,
             "以实际结果为准"),
            ("privacy_leak", re.compile(r"身份证号|银行卡号|完整手机号"), BLOCK,
             "抱歉，这个问题我无法提供。"),
        ]
        self.transfer = re.compile(r"转人工|找人工|投诉")
        self.allowed_tools = {"order.query", "order.reschedule", "ticket.create", "lead.query"}

    def _check_text(self, text: str) -> GuardResult:
        start = time.perf_counter()
        for rule_id, pattern, decision, replacement in self.forbidden:
            if pattern.search(text):
                latency = (time.perf_counter() - start) * 1000
                if decision == BLOCK:
                    return GuardResult(BLOCK, replacement, [rule_id], "fatal", latency)
                return GuardResult(REWRITE, replacement, [rule_id], "warn", latency)
        if self.transfer.search(text):
            latency = (time.perf_counter() - start) * 1000
            return GuardResult(TRANSFER, text, ["user_request_human"], "info", latency)
        latency = (time.perf_counter() - start) * 1000
        return GuardResult(ALLOW, text, [], "info", latency)

    async def check_input(self, text: str, ctx: dict[str, Any] | None = None) -> GuardResult:
        return self._check_text(text)

    async def check_sentence(self, text: str, ctx: dict[str, Any] | None = None) -> GuardResult:
        return self._check_text(text)

    async def check_tool(self, tool: str, args: dict[str, Any], ctx: dict[str, Any] | None = None) -> GuardResult:
        start = time.perf_counter()
        latency = (time.perf_counter() - start) * 1000
        if tool not in self.allowed_tools:
            return GuardResult(BLOCK, "", ["tool_not_allowed"], "fatal", latency)
        return GuardResult(ALLOW, "", [], "info", latency)
