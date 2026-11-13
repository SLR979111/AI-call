"""质检报告生成器"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SessionReport:
    """会话质检报告"""
    session_id: str
    start_time: datetime
    end_time: datetime | None = None
    turn_count: int = 0
    compliance_violations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    response_times: list[float] = field(default_factory=list)
    task_completion: str | None = None
    outcome: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "turn_count": self.turn_count,
            "compliance_violations": self.compliance_violations,
            "tool_calls": self.tool_calls,
            "response_times": self.response_times,
            "avg_response_time": sum(self.response_times) / len(self.response_times) if self.response_times else 0,
            "task_completion": self.task_completion,
            "outcome": self.outcome,
        }
    
    def to_markdown(self) -> str:
        """生成 Markdown 格式报告"""
        lines = [
            f"# 质检报告 - {self.session_id}",
            "",
            "## 基本信息",
            f"- **会话 ID**: {self.session_id}",
            f"- **开始时间**: {self.start_time.strftime('%Y-%m-%d %H:%M:%S') if self.start_time else 'N/A'}",
            f"- **结束时间**: {self.end_time.strftime('%Y-%m-%d %H:%M:%S') if self.end_time else '进行中'}",
            f"- **对话轮次**: {self.turn_count}",
            f"- **任务完成**: {self.task_completion or 'N/A'}",
            f"- **最终结果**: {self.outcome or 'N/A'}",
            "",
            "## 性能指标",
            f"- **平均响应时间**: {sum(self.response_times) / len(self.response_times) if self.response_times else 0:.2f} ms",
            f"- **响应时间范围**: {min(self.response_times) if self.response_times else 0:.2f} - {max(self.response_times) if self.response_times else 0:.2f} ms",
            "",
            "## 合规检查",
            f"- **违规次数**: {len(self.compliance_violations)}",
        ]
        
        if self.compliance_violations:
            lines.append("\n### 违规详情")
            for i, violation in enumerate(self.compliance_violations, 1):
                lines.append(f"\n{i}. **{violation.get('rule_id', 'unknown')}**")
                lines.append(f"   - 严重程度: {violation.get('severity', 'unknown')}")
                lines.append(f"   - 决策: {violation.get('decision', 'unknown')}")
                lines.append(f"   - 违规内容: {violation.get('text', 'N/A')[:100]}...")
        
        lines.append("\n## 工具调用")
        lines.append(f"- **调用次数**: {len(self.tool_calls)}")
        
        if self.tool_calls:
            lines.append("\n### 调用详情")
            for i, call in enumerate(self.tool_calls, 1):
                lines.append(f"\n{i}. **{call.get('tool', 'unknown')}**")
                lines.append(f"   - 结果: {call.get('result', 'unknown')}")
                lines.append(f"   - 时延: {call.get('latency_ms', 0):.2f} ms")
        
        return "\n".join(lines)


class QualityReporter:
    """质检报告生成器"""
    
    def __init__(self, output_dir: Path | str = "data/quality_reports") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, SessionReport] = {}
    
    def start_session(self, session_id: str) -> None:
        """开始会话"""
        self.sessions[session_id] = SessionReport(
            session_id=session_id,
            start_time=datetime.now()
        )
    
    def record_turn(self, session_id: str) -> None:
        """记录对话轮次"""
        if session_id in self.sessions:
            self.sessions[session_id].turn_count += 1
    
    def record_compliance_violation(
        self, session_id: str, rule_id: str, decision: str, 
        text: str, severity: str
    ) -> None:
        """记录合规违规"""
        if session_id in self.sessions:
            self.sessions[session_id].compliance_violations.append({
                "rule_id": rule_id,
                "decision": decision,
                "text": text,
                "severity": severity,
                "timestamp": datetime.now().isoformat(),
            })
    
    def record_tool_call(
        self, session_id: str, tool: str, result: str, latency_ms: float
    ) -> None:
        """记录工具调用"""
        if session_id in self.sessions:
            self.sessions[session_id].tool_calls.append({
                "tool": tool,
                "result": result,
                "latency_ms": latency_ms,
                "timestamp": datetime.now().isoformat(),
            })
    
    def record_response_time(self, session_id: str, time_ms: float) -> None:
        """记录响应时间"""
        if session_id in self.sessions:
            self.sessions[session_id].response_times.append(time_ms)
    
    def end_session(
        self, session_id: str, outcome: str, task_completion: str | None = None
    ) -> None:
        """结束会话"""
        if session_id in self.sessions:
            report = self.sessions[session_id]
            report.end_time = datetime.now()
            report.outcome = outcome
            report.task_completion = task_completion
    
    def generate_report(
        self, session_id: str, formats: list[str] | None = None
    ) -> dict[str, Path]:
        """生成报告
        
        Args:
            session_id: 会话 ID
            formats: 输出格式列表，可选 ["json", "markdown"]
            
        Returns:
            生成的文件路径字典
        """
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        
        report = self.sessions[session_id]
        formats = formats or ["json", "markdown"]
        output_files = {}
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"quality_{session_id}_{timestamp}"
        
        if "json" in formats:
            json_path = self.output_dir / f"{base_name}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
            output_files["json"] = json_path
        
        if "markdown" in formats:
            md_path = self.output_dir / f"{base_name}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(report.to_markdown())
            output_files["markdown"] = md_path
        
        return output_files


class ComplianceReporter:
    """合规违规报告生成器"""
    
    def __init__(self, output_dir: Path | str = "data/compliance_reports") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.violations: list[dict[str, Any]] = []
    
    def record_violation(
        self,
        session_id: str,
        turn_id: str,
        rule_id: str,
        violation_text: str,
        decision: str,
        severity: str,
    ) -> None:
        """记录合规违规"""
        self.violations.append({
            "session_id": session_id,
            "turn_id": turn_id,
            "rule_id": rule_id,
            "violation_text": violation_text,
            "decision": decision,
            "severity": severity,
            "timestamp": datetime.now().isoformat(),
        })
    
    def generate_report(
        self, start_time: datetime | None = None, end_time: datetime | None = None,
        formats: list[str] | None = None
    ) -> dict[str, Path]:
        """生成报告
        
        Args:
            start_time: 开始时间过滤
            end_time: 结束时间过滤
            formats: 输出格式列表，可选 ["json", "markdown"]
            
        Returns:
            生成的文件路径字典
        """
        formats = formats or ["json", "markdown"]
        filtered_violations = self.violations
        
        if start_time or end_time:
            filtered_violations = [
                v for v in self.violations
                if (not start_time or datetime.fromisoformat(v["timestamp"]) >= start_time)
                and (not end_time or datetime.fromisoformat(v["timestamp"]) <= end_time)
            ]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"compliance_{timestamp}"
        output_files = {}
        
        if "json" in formats:
            json_path = self.output_dir / f"{base_name}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({
                    "total_violations": len(filtered_violations),
                    "violations": filtered_violations,
                }, f, indent=2, ensure_ascii=False)
            output_files["json"] = json_path
        
        if "markdown" in formats:
            md_path = self.output_dir / f"{base_name}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(self._to_markdown(filtered_violations))
            output_files["markdown"] = md_path
        
        return output_files
    
    def _to_markdown(self, violations: list[dict[str, Any]]) -> str:
        """生成 Markdown 格式报告"""
        lines = [
            "# 合规违规报告",
            "",
            f"## 概览",
            f"- **违规总数**: {len(violations)}",
            f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 违规详情",
        ]
        
        for i, v in enumerate(violations, 1):
            lines.append(f"\n### {i}. {v['rule_id']}")
            lines.append(f"- **会话**: {v['session_id']}")
            lines.append(f"- **轮次**: {v['turn_id']}")
            lines.append(f"- **严重程度**: {v['severity']}")
            lines.append(f"- **决策**: {v['decision']}")
            lines.append(f"- **时间**: {v['timestamp']}")
            lines.append(f"- **违规内容**: {v['violation_text'][:200]}...")
        
        return "\n".join(lines)
