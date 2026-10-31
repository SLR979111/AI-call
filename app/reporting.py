from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return round(ordered[index], 2)


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    latencies: list[float] = []
    outcomes: list[str] = []
    for event in events:
        kind = str(event.get("type", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
        payload = event.get("payload") or {}
        if kind == "TurnLatency" and isinstance(payload.get("e2e_ms"), (int, float)):
            latencies.append(float(payload["e2e_ms"]))
        if kind == "TurnCommitted" and payload.get("phase") == "result":
            outcome = (payload.get("result") or {}).get("outcome")
            if outcome:
                outcomes.append(str(outcome))
    return {
        "events": len(events),
        "turns": counts.get("TurnStarted", 0),
        "latency_count": len(latencies),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "max_ms": round(max(latencies), 2) if latencies else None,
        "compliance_checked": counts.get("ComplianceChecked", 0),
        "compliance_blocked": counts.get("ComplianceBlocked", 0),
        "compliance_rewritten": counts.get("ComplianceRewritten", 0),
        "tool_calls": counts.get("ToolCalled", 0),
        "tool_failures": counts.get("ToolFailed", 0),
        "barge_ins": counts.get("BargeInDetected", 0),
        "aborted": counts.get("TurnAborted", 0),
        "errors": counts.get("AgentError", 0) + counts.get("SessionFailed", 0),
        "outcomes": outcomes,
    }


def load_session_events(data_dir: Path, session_id: str) -> list[dict[str, Any]]:
    path = Path(data_dir) / "sessions" / session_id / "events.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def render_markdown(data_dir: Path, sessions: list[tuple[str, list[dict[str, Any]]]]) -> str:
    lines: list[str] = []
    lines.append("# 自测报告（自动生成）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 数据目录：`{data_dir}`")
    lines.append(f"- 会话数：{len(sessions)}")
    lines.append("")
    lines.append("## 1. 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    total = {"events": 0, "turns": 0, "tool_calls": 0, "tool_failures": 0,
             "compliance_blocked": 0, "barge_ins": 0, "aborted": 0, "errors": 0}
    latencies: list[float] = []
    for sid, events in sessions:
        summary = summarize_events(events)
        for key in total:
            total[key] += int(summary.get(key) or 0)
        for event in events:
            if event.get("type") == "TurnLatency":
                value = (event.get("payload") or {}).get("e2e_ms")
                if isinstance(value, (int, float)):
                    latencies.append(float(value))
    lines.append(f"| 事件数 | {total['events']} |")
    lines.append(f"| 轮次数 | {total['turns']} |")
    lines.append(f"| P50 首字时延 | {_percentile(latencies, 0.50)} ms |")
    lines.append(f"| P95 首字时延 | {_percentile(latencies, 0.95)} ms |")
    lines.append(f"| 工具调用 | {total['tool_calls']} |")
    lines.append(f"| 工具失败 | {total['tool_failures']} |")
    lines.append(f"| 合规拦截 | {total['compliance_blocked']} |")
    lines.append(f"| 打断 | {total['barge_ins']} |")
    lines.append(f"| 异常中断 | {total['aborted']} |")
    lines.append("")
    lines.append("## 2. 每会话")
    lines.append("")
    lines.append("| session_id | 轮次 | P50 | P95 | 工具失败 | 合规拦截 | 打断 | outcomes |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for sid, events in sessions:
        summary = summarize_events(events)
        lines.append(
            f"| `{sid}` | {summary['turns']} | {summary['p50_ms']} | {summary['p95_ms']} | "
            f"{summary['tool_failures']} | {summary['compliance_blocked']} | "
            f"{summary['barge_ins']} | {', '.join(summary['outcomes']) or '-'} |"
        )
    lines.append("")
    lines.append("## 3. 原始日志")
    lines.append("")
    for sid, _ in sessions:
        lines.append(f"- `{data_dir}/sessions/{sid}/events.jsonl`")
    lines.append("")
    lines.append("> 本报告由 `scripts/report.py` 从事件日志生成；提交前请脱敏并附上运行命令。")
    return "\n".join(lines) + "\n"
