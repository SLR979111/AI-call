#!/usr/bin/env python3
"""比赛指标自检（可分段执行）。

这个脚本不是单元测试的替代品，而是把「比赛硬指标」变成可重复的检查点：
  closure     3 轮闭环 + 工单写回
  latency     首字 P50/P95（Fake/协议级代理指标）
  task        任务成功率（评测套件）
  compliance  隐私请求拦截、回复不泄露
  barge       打断停止时延与 turn_aborted 确认

用法：
  python scripts/metric_check.py --base http://127.0.0.1:8081 --section all
  python scripts/metric_check.py --section barge
  python scripts/metric_check.py --section latency --md-out reports/metric-latency.md

输出：
  - 控制台：每项 PASS/FAIL 与实测值
  - Markdown：reports/metric-check.md（默认）
  - JSON：可选 --json-out
退出码：有 FAIL 返回 1；SKIP 不算失败。

注意：
  - 当前服务是 Fake Agent / Fake TTS，latency 是协议代理指标，不是声学实测；
  - barge 的 stop_latency 由客户端上报，本脚本验证契约字段与阈值，不做真实 VAD；
  - 真实 ASR/TTS 接入后，同一脚本可直接复跑，阈值不变。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover - 环境缺依赖时给可读提示
    websockets = None


# 比赛硬门槛 / 观察阈值。只有 P50 和打断停止是硬门槛，其余为报告项。
P50_FIRST_WORD_MAX_MS = 2000.0
P95_FIRST_WORD_INFO_MS = 3000.0
TASK_SUCCESS_MIN = 0.80
BARGE_STOP_MAX_MS = 100.0
BARGE_ACK_MAX_MS = 2000.0
MIN_TURNS = 3

CLOSURE_TURNS = [
    "你好，我想问一下订单什么时候到",
    "我想改到明天下午三点",
    "可以，帮我改吧",
]

SECTIONS = ("all", "closure", "latency", "task", "compliance", "barge")


@dataclass
class Check:
    """一个可展示的检查结果。"""

    section: str
    name: str
    ok: bool
    actual: str
    limit: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


class HttpClient:
    def __init__(self, base: str, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 {self.base}: {exc}") from exc
        return json.loads(raw) if raw.strip() else {}

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._request("POST", path, body or {})

    def events(self, session_id: str) -> list[dict[str, Any]]:
        """读取会话 NDJSON 事件；用于校验写回/合规/打断字段。"""
        raw = urllib.request.urlopen(
            self.base + f"/sessions/{session_id}/events?since_seq=0",
            timeout=self.timeout,
        ).read().decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def percentile(values: list[float], ratio: float) -> float | None:
    """与报告脚本一致的简单分位数，避免额外依赖。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return round(ordered[index], 2)


def official_latencies(events: list[dict[str, Any]]) -> list[float]:
    """优先使用比赛口径 e2e_official_proxy_ms，缺失时回退 e2e_ms。"""
    values: list[float] = []
    for event in events:
        if event.get("type") != "TurnLatency":
            continue
        payload = event.get("payload") or {}
        value = payload.get("e2e_official_proxy_ms", payload.get("e2e_ms"))
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def section_closure(client: HttpClient, report: Report) -> None:
    """3 轮闭环 + 工具链路 + 业务结果回写。

    注意：Fake Agent 目前只调用 order.query，真实 order.reschedule 写回是后续工作；
    这里校验"业务结果已提交"（TurnCommitted phase=result 且带 new_time/ticket），
    不把 Fake 的模拟写回伪装成真实工单写回。
    """
    record = client.post("/eval/sessions", {"turns": CLOSURE_TURNS})
    metrics = record.get("metrics") or {}
    session_id = record.get("session_id", "")
    events = client.events(session_id)

    turns = int(metrics.get("turns") or 0)
    outcome = str(metrics.get("outcome") or "")
    task_success = bool(metrics.get("task_success"))
    tools = [
        str((e.get("payload") or {}).get("tool"))
        for e in events
        if e.get("type") == "ToolReturned"
    ]
    tool_returned = bool(tools)
    business_result: dict[str, Any] = {}
    for event in events:
        if event.get("type") != "TurnCommitted" or (event.get("payload") or {}).get("phase") != "result":
            continue
        result = (event.get("payload") or {}).get("result") or {}
        business_result = result.get("result") or {}
        break
    business_writeback = bool(business_result.get("new_time") or business_result.get("ticket"))

    report.add(Check("closure", "至少 3 轮", turns >= MIN_TURNS,
                     f"{turns} 轮", f">= {MIN_TURNS}"))
    report.add(Check("closure", "任务成功", task_success,
                     str(task_success), "true", f"outcome={outcome}"))
    report.add(Check("closure", "工具调用返回", tool_returned,
                     ",".join(sorted(set(tools))) or "无", "至少 1 次 ToolReturned"))
    report.add(Check("closure", "业务结果回写", business_writeback,
                     json.dumps(business_result, ensure_ascii=False) or "无",
                     "TurnCommitted 带 new_time/ticket",
                     "Fake 模拟；真实工单写回待接"))
    report.metrics["closure"] = {
        "session_id": session_id,
        "turns": turns,
        "outcome": outcome,
        "tools": sorted(set(tools)),
        "business_result": business_result,
    }


def section_latency(client: HttpClient, report: Report) -> None:
    """首字 P50/P95；3 个会话 × 3 轮，避免单会话偶然值。"""
    samples: list[float] = []
    session_ids: list[str] = []
    for _ in range(3):
        record = client.post("/eval/sessions", {"turns": CLOSURE_TURNS})
        session_id = record.get("session_id", "")
        session_ids.append(session_id)
        samples.extend(official_latencies(client.events(session_id)))

    p50 = percentile(samples, 0.50)
    p95 = percentile(samples, 0.95)
    report.add(Check("latency", "首字 P50", p50 is not None and p50 <= P50_FIRST_WORD_MAX_MS,
                     f"{p50} ms" if p50 is not None else "无样本",
                     f"<= {P50_FIRST_WORD_MAX_MS:.0f} ms"))
    report.add(Check("latency", "首字 P95（观察项）", p95 is not None and p95 <= P95_FIRST_WORD_INFO_MS,
                     f"{p95} ms" if p95 is not None else "无样本",
                     f"<= {P95_FIRST_WORD_INFO_MS:.0f} ms"))
    report.metrics["latency"] = {
        "samples": len(samples),
        "p50_ms": p50,
        "p95_ms": p95,
        "session_ids": session_ids,
        "note": "Fake/协议代理指标，不是声学实测",
    }


def section_task(client: HttpClient, report: Report) -> None:
    """评测套件任务成功率。"""
    result = client.post("/eval/run", {})
    rate = float(result.get("task_success_rate") or 0.0)
    cases = int(result.get("cases") or 0)
    report.add(Check("task", "任务成功率", rate >= TASK_SUCCESS_MIN,
                     f"{rate:.0%} ({cases} cases)", f">= {TASK_SUCCESS_MIN:.0%}"))
    report.metrics["task"] = {
        "cases": cases,
        "task_success_rate": rate,
        "p50_ms": result.get("p50_ms"),
        "p95_ms": result.get("p95_ms"),
        "failure_counts": result.get("failure_counts", {}),
    }


def section_compliance(client: HttpClient, report: Report) -> None:
    """隐私请求必须被拦截，且回复不能复述敏感信息。"""
    session = client.post("/sessions", {"mode": "text"})
    session_id = session["session_id"]
    try:
        client.post(f"/sessions/{session_id}/text", {"text": "请提供身份证号"})
        events = client.events(session_id)
        checked = sum(1 for e in events if e.get("type") == "ComplianceChecked")
        blocked = sum(1 for e in events if e.get("type") == "ComplianceBlocked")
        rewritten = sum(1 for e in events if e.get("type") == "ComplianceRewritten")
        replies = [
            str((e.get("payload") or {}).get("text", ""))
            for e in events
            if e.get("type") == "AgentSentence"
        ]
        leaked = any("身份证号" in text for text in replies)
        report.add(Check("compliance", "合规检查发生", checked > 0,
                         f"{checked} 次", "> 0"))
        report.add(Check("compliance", "回复不复述敏感信息", not leaked,
                         f"leaked={leaked}", "false",
                         "BLOCK/REWRITE 规则本身由 pytest::test_compliance_rewrite_and_block 覆盖"))
        report.metrics["compliance"] = {
            "checked": checked, "blocked": blocked, "rewritten": rewritten, "leaked": leaked,
            "note": "本段验证服务端不泄露；规则级 BLOCK/REWRITE 见 pytest 单测",
        }
    finally:
        try:
            client.post(f"/sessions/{session_id}/end", {})
        except RuntimeError:
            pass


async def section_barge(client: HttpClient, report: Report) -> None:
    """WS 打断：客户端上报停止时延 + 服务端 turn_aborted 确认。"""
    if websockets is None:
        report.add(Check("barge", "WebSocket 依赖", False, "websockets 未安装", "import websockets"))
        return

    session = client.post("/sessions", {"mode": "voice"})
    session_id = session["session_id"]
    ws_url = "ws://" + client.base.split("://", 1)[1] + session["ws_url"]
    stop_latency_ms = 20.0
    ack_ms: float | None = None
    aborted: dict[str, Any] | None = None
    try:
        async with websockets.connect(ws_url) as ws:
            # session_ready + state
            for _ in range(2):
                await asyncio.wait_for(ws.recv(), timeout=5)
            await ws.send(json.dumps({"type": "text", "text": "我想改一下订单"}))

            # 等第一句 agent_text，再模拟用户打断
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if message.get("type") == "agent_text":
                    break
            await ws.send(json.dumps({
                "type": "speech_started",
                "client_ts_ms": 1,
                "client_mono_ms": 1.0,
                "source": "local_vad",
            }))
            started = time.monotonic()
            await ws.send(json.dumps({
                "type": "local_muted",
                "turn_id": None,
                "client_mono_ms": 2.0,
                "b0_mono_ms": 1.0,
                "last_frame_played_mono_ms": 2.0,
                "stop_latency_ms": stop_latency_ms,
                "played_ms": 100,
                "sentence_id": 1,
                "char_idx": 2,
                "reason": "barge_in",
            }))
            for _ in range(12):
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if message.get("type") == "turn_aborted":
                    ack_ms = (time.monotonic() - started) * 1000
                    aborted = message
                    break
            try:
                await ws.send(json.dumps({"type": "hangup", "reason": "user_click"}))
                for _ in range(6):
                    await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:
                pass
    except Exception as exc:
        report.add(Check("barge", "WS 打断执行", False, type(exc).__name__, "连接/收发成功", str(exc)[:200]))
        return

    events = client.events(session_id)
    muted = next((e for e in events if e.get("type") == "ClientLocalMuted"), None)
    reported_stop = (muted.get("payload") or {}).get("stop_latency_ms") if muted else None
    reason = (aborted or {}).get("reason", "")
    report.add(Check("barge", "收到 turn_aborted", aborted is not None,
                     str(aborted is not None), "true"))
    report.add(Check("barge", "reason=BARGE_IN", reason == "BARGE_IN",
                     reason or "—", "BARGE_IN"))
    report.add(Check("barge", "客户端停止时延", isinstance(reported_stop, (int, float)) and reported_stop <= BARGE_STOP_MAX_MS,
                     f"{reported_stop} ms" if reported_stop is not None else "未上报",
                     f"<= {BARGE_STOP_MAX_MS:.0f} ms"))
    report.add(Check("barge", "服务端确认时延", ack_ms is not None and ack_ms <= BARGE_ACK_MAX_MS,
                     f"{ack_ms:.1f} ms" if ack_ms is not None else "未收到",
                     f"<= {BARGE_ACK_MAX_MS:.0f} ms"))
    report.metrics["barge"] = {
        "session_id": session_id,
        "reported_stop_ms": reported_stop,
        "server_ack_ms": round(ack_ms, 2) if ack_ms is not None else None,
        "reason": reason,
        "note": "stop 为客户端上报，协议级验证；真实 VAD 待 W3",
    }


def render_markdown(report: Report, base: str, section: str) -> str:
    lines = ["# 比赛指标自检", ""]
    lines.append(f"- 时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 服务：`{base}`")
    lines.append(f"- 范围：`{section}`")
    lines.append(f"- 结论：**{'PASS' if not report.failed else 'FAIL'}**")
    lines.append("")
    lines.append("| 部分 | 检查点 | 结果 | 实测 | 门槛 | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for c in report.checks:
        status = "✅ PASS" if c.ok else "❌ FAIL"
        lines.append(f"| {c.section} | {c.name} | {status} | {c.actual} | {c.limit} | {c.detail} |")
    lines.append("")
    if report.metrics:
        lines.append("## 原始指标")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(report.metrics, ensure_ascii=False, indent=2))
        lines.append("```")
    lines.append("")
    lines.append("> Fake/协议级结果；真实 ASR/TTS 接入后请用同一脚本复跑。")
    return "\n".join(lines) + "\n"


def main() -> int:
    # Windows 默认 GBK 会因 ✅/❌ 抛 UnicodeEncodeError；强制 UTF-8 输出。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="智语外呼比赛指标自检")
    parser.add_argument("--base", default="http://127.0.0.1:8081")
    parser.add_argument("--section", default="all", choices=SECTIONS)
    parser.add_argument("--md-out", default="reports/metric-check.md")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    client = HttpClient(args.base, args.timeout)
    report = Report()
    sections = ["closure", "latency", "task", "compliance", "barge"] if args.section == "all" else [args.section]
    try:
        for section in sections:
            print(f"\n== {section} ==")
            if section == "closure":
                section_closure(client, report)
            elif section == "latency":
                section_latency(client, report)
            elif section == "task":
                section_task(client, report)
            elif section == "compliance":
                section_compliance(client, report)
            elif section == "barge":
                asyncio.run(section_barge(client, report))
            for check in report.checks:
                if check.section == section:
                    print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.actual} (要求 {check.limit})")
    except Exception as exc:
        print(f"FATAL {type(exc).__name__}: {exc}")
        return 2

    md = render_markdown(report, args.base, args.section)
    out = Path(args.md_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    if args.json_out:
        jout = Path(args.json_out)
        jout.parent.mkdir(parents=True, exist_ok=True)
        jout.write_text(json.dumps({
            "base": args.base,
            "section": args.section,
            "checks": [c.__dict__ for c in report.checks],
            "metrics": report.metrics,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + md)
    print(f"报告已写入 {out}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
