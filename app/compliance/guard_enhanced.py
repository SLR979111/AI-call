"""增强的合规守卫 - 支持从 YAML 加载规则"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    
    Supports both hardcoded rules (backward compatible) and YAML-based rules.
    """

    def __init__(self, rules_path: Path | str | None = None) -> None:
        """初始化合规守卫
        
        Args:
            rules_path: 可选的 YAML 规则文件路径。如果未提供，使用硬编码规则。
        """
        self.rules_from_yaml = False
        self.text_rules: list[dict[str, Any]] = []
        self.transfer_rules: list[dict[str, Any]] = []
        self.allowed_tools: set[str] = set()
        
        # 尝试从 YAML 加载规则
        if rules_path:
            self._load_from_yaml(Path(rules_path))
        
        # 如果没有从 YAML 加载，使用硬编码规则（向后兼容）
        if not self.rules_from_yaml:
            self._load_hardcoded_rules()
    
    def _load_from_yaml(self, path: Path) -> None:
        """从 YAML 文件加载规则"""
        try:
            from .loader import load_rules
            rules = load_rules(path)
            
            self.text_rules = rules.text_rules
            self.transfer_rules = rules.transfer_rules
            self.allowed_tools = rules.allowed_tools
            self.rules_from_yaml = True
            
        except Exception as e:
            # 加载失败时回退到硬编码规则
            import warnings
            warnings.warn(f"Failed to load rules from {path}: {e}. Using hardcoded rules.")
    
    def _load_hardcoded_rules(self) -> None:
        """加载硬编码规则（向后兼容）"""
        # 文本规则
        self.text_rules = [
            {
                "id": "false_promise",
                "pattern": re.compile(r"保证|一定|绝对|百分百|最低价"),
                "decision": REWRITE,
                "replacement": "以实际结果为准",
                "severity": "warn",
            },
            {
                "id": "privacy_leak",
                "pattern": re.compile(r"身份证号|银行卡号|完整手机号"),
                "decision": BLOCK,
                "replacement": "抱歉，这个问题我无法提供。",
                "severity": "fatal",
            },
        ]
        
        # 转人工规则
        self.transfer_rules = [
            {
                "id": "user_request_human",
                "pattern": re.compile(r"转人工|找人工|投诉"),
                "decision": TRANSFER,
                "severity": "info",
            },
        ]
        
        # 允许的工具
        self.allowed_tools = {
            "order_query", "order_reschedule", "ticket_create", 
            "lead_query", "callback_create"
        }

    def _check_text(self, text: str) -> GuardResult:
        start = time.perf_counter()
        
        # 检查文本规则
        for rule in self.text_rules:
            if rule["pattern"].search(text):
                latency = (time.perf_counter() - start) * 1000
                decision = rule["decision"]
                replacement = rule.get("replacement", text)
                rule_id = rule["id"]
                severity = rule.get("severity", "warn")
                
                if decision == BLOCK:
                    return GuardResult(BLOCK, replacement, [rule_id], severity, latency)
                return GuardResult(REWRITE, replacement, [rule_id], severity, latency)
        
        # 检查转人工规则
        for rule in self.transfer_rules:
            if rule["pattern"].search(text):
                latency = (time.perf_counter() - start) * 1000
                return GuardResult(
                    TRANSFER, text, [rule["id"]], 
                    rule.get("severity", "info"), latency
                )
        
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
