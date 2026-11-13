"""合规规则加载器 - 从 YAML 配置文件加载合规规则"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class RuleLoadError(Exception):
    """规则加载失败"""
    pass


class ComplianceRules:
    """合规规则容器"""
    
    def __init__(self, config: dict[str, Any]) -> None:
        self.version = config.get("version", "1.0")
        self.text_rules: list[dict[str, Any]] = []
        self.transfer_rules: list[dict[str, Any]] = []
        self.allowed_tools: set[str] = set()
        self.tool_parameter_rules: dict[str, dict[str, Any]] = {}
        self.quality_metrics: list[dict[str, Any]] = []
        self.task_completion: list[dict[str, Any]] = []
        self.quality_report_config: dict[str, Any] = {}
        self.compliance_report_config: dict[str, Any] = {}
        
        self._load_text_rules(config.get("text_rules", []))
        self._load_transfer_rules(config.get("transfer_rules", []))
        self._load_tool_rules(config.get("tool_rules", {}))
        self._load_quality_check(config.get("quality_check", {}))
        self._load_reporting(config.get("reporting", {}))
    
    def _load_text_rules(self, rules: list[dict[str, Any]]) -> None:
        """加载文本合规规则"""
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            
            try:
                compiled_pattern = re.compile(rule["pattern"])
                self.text_rules.append({
                    "id": rule["id"],
                    "name": rule.get("name", ""),
                    "pattern": compiled_pattern,
                    "decision": rule["decision"],
                    "replacement": rule.get("replacement", ""),
                    "severity": rule.get("severity", "warn"),
                    "description": rule.get("description", ""),
                })
            except re.error as e:
                raise RuleLoadError(f"Invalid regex pattern in rule {rule.get('id')}: {e}")
    
    def _load_transfer_rules(self, rules: list[dict[str, Any]]) -> None:
        """加载转人工规则"""
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            
            try:
                compiled_pattern = re.compile(rule["pattern"])
                self.transfer_rules.append({
                    "id": rule["id"],
                    "name": rule.get("name", ""),
                    "pattern": compiled_pattern,
                    "decision": rule["decision"],
                    "severity": rule.get("severity", "info"),
                    "description": rule.get("description", ""),
                })
            except re.error as e:
                raise RuleLoadError(f"Invalid regex pattern in transfer rule {rule.get('id')}: {e}")
    
    def _load_tool_rules(self, rules: dict[str, Any]) -> None:
        """加载工具调用规则"""
        self.allowed_tools = set(rules.get("allowed_tools", []))
        self.tool_parameter_rules = rules.get("parameter_rules", {})
    
    def _load_quality_check(self, config: dict[str, Any]) -> None:
        """加载质检规则"""
        self.quality_metrics = [
            metric for metric in config.get("metrics", [])
            if metric.get("enabled", True)
        ]
        self.task_completion = config.get("task_completion", [])
    
    def _load_reporting(self, config: dict[str, Any]) -> None:
        """加载报告配置"""
        self.quality_report_config = config.get("quality_report", {})
        self.compliance_report_config = config.get("compliance_report", {})


def load_rules(path: Path | str) -> ComplianceRules:
    """从 YAML 文件加载合规规则
    
    Args:
        path: YAML 配置文件路径
        
    Returns:
        ComplianceRules 实例
        
    Raises:
        RuleLoadError: 加载失败
    """
    if yaml is None:
        raise RuleLoadError("PyYAML is not installed. Run: pip install pyyaml")
    
    path = Path(path)
    if not path.exists():
        raise RuleLoadError(f"Rules file not found: {path}")
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RuleLoadError(f"Failed to parse YAML: {e}")
    except Exception as e:
        raise RuleLoadError(f"Failed to read rules file: {e}")
    
    if not isinstance(config, dict):
        raise RuleLoadError("Invalid rules file: expected a YAML dictionary")
    
    return ComplianceRules(config)
