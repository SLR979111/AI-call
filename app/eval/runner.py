from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..events import EventBus
from ..orchestrator import Orchestrator


@dataclass
class EvalCase:
    case_id: str
    turns: list[str]
    expected_outcome: str = "any"
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    case_id: str
    session_id: str
    outcome: str | None
    task_success: bool
    turns: int
    e2e_ms: list[float]
    failure_labels: list[str]
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "session_id": self.session_id,
            "outcome": self.outcome,
            "task_success": self.task_success,
            "turns": self.turns,
            "e2e_ms": self.e2e_ms,
            "failure_labels": self.failure_labels,
            "tags": self.tags,
        }


class EvalRunner:
    def __init__(self, orchestrator: Orchestrator, bus: EventBus) -> None:
        self.orchestrator = orchestrator
        self.bus = bus

    @staticmethod
    def load_cases(path: Path) -> list[EvalCase]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data.get("cases", data) if isinstance(data, dict) else data
        return [
            EvalCase(
                case_id=str(item["case_id"]),
                turns=list(item.get("turns", [])),
                expected_outcome=str(item.get("expected_outcome", "any")),
                tags=list(item.get("tags", [])),
            )
            for item in items
        ]

    async def run_case(self, case: EvalCase, release_id: str | None = None) -> EvalResult:
        session = await self.orchestrator.create_session("text")
        results: list[dict[str, Any]] = []
        for text in case.turns:
            results.append(await self.orchestrator.handle_text(session.session_id, text, None))
        events = self.bus.read_events(session.session_id)
        latencies = [
            float(e["payload"]["e2e_ms"])
            for e in events
            if e.get("type") == "TurnLatency" and e.get("payload", {}).get("e2e_ms") is not None
        ]
        last = results[-1] if results else {}
        outcome = (last.get("result") or {}).get("outcome") if isinstance(last, dict) else None
        failures: list[str] = []
        if any(e.get("type") == "ComplianceBlocked" for e in events):
            failures.append("compliance_blocked")
        if any(e.get("type") == "ToolFailed" for e in events):
            failures.append("tool_failed")
        if any(e.get("type") == "TurnAborted" for e in events):
            failures.append("turn_aborted")
        if outcome in {"tool_failed", "error", None}:
            failures.append("task_incomplete")
        success = case.expected_outcome in {"any", outcome}
        if not success:
            failures.append(f"outcome_mismatch:{case.expected_outcome}!={outcome}")
        return EvalResult(
            case_id=case.case_id,
            session_id=session.session_id,
            outcome=outcome,
            task_success=success,
            turns=len(results),
            e2e_ms=latencies,
            failure_labels=sorted(set(failures)),
            tags=case.tags,
        )

    async def run(self, cases: list[EvalCase], release_id: str | None = None) -> dict[str, Any]:
        results = [await self.run_case(case, release_id) for case in cases]
        latencies = [value for item in results for value in item.e2e_ms]
        latencies_sorted = sorted(latencies)
        p50 = statistics.median(latencies_sorted) if latencies_sorted else None
        p95 = latencies_sorted[int(len(latencies_sorted) * 0.95) - 1] if latencies_sorted else None
        successes = sum(1 for item in results if item.task_success)
        failure_counts: dict[str, int] = {}
        for item in results:
            for label in item.failure_labels:
                failure_counts[label] = failure_counts.get(label, 0) + 1
        return {
            "release_id": release_id,
            "cases": len(results),
            "task_success_rate": round(successes / len(results), 4) if results else 0.0,
            "p50_ms": round(p50, 2) if p50 is not None else None,
            "p95_ms": round(p95, 2) if p95 is not None else None,
            "failure_counts": failure_counts,
            "results": [item.to_dict() for item in results],
        }
