from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass
class GateConfig:
    min_delta: float = 0.03
    max_p95_regression: float = 0.05
    max_p50_regression: float = 0.05
    min_compliance: float = 1.0


def _values(report: dict[str, Any], key: str) -> list[float]:
    results = report.get("results") or []
    values = [1.0 if item.get("task_success") else 0.0 for item in results]
    if values:
        return values
    if key in report:
        return [float(report[key])]
    return []


def bootstrap_delta_ci(baseline: list[float], candidate: list[float], *,
                       iterations: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Paired bootstrap over case-level success values.

    Returns (delta, ci_low, ci_high). If either side is empty, falls back to
    the point difference with a conservative [-1, 1] interval.
    """
    if not baseline or not candidate:
        delta = (sum(candidate) / len(candidate) if candidate else 0.0) - (
            sum(baseline) / len(baseline) if baseline else 0.0
        )
        return delta, -1.0, 1.0
    rng = random.Random(seed)
    n = min(len(baseline), len(candidate))
    deltas: list[float] = []
    for _ in range(iterations):
        idx = [rng.randrange(n) for _ in range(n)]
        b = sum(baseline[i] for i in idx) / n
        c = sum(candidate[i] for i in idx) / n
        deltas.append(c - b)
    deltas.sort()
    low = deltas[int(0.025 * len(deltas))]
    high = deltas[int(0.975 * len(deltas)) - 1]
    point = (sum(candidate[:n]) / n) - (sum(baseline[:n]) / n)
    return point, low, high


def compare(baseline: dict[str, Any], candidate: dict[str, Any],
            config: GateConfig | None = None) -> dict[str, Any]:
    cfg = config or GateConfig()
    base_values = _values(baseline, "task_success_rate")
    cand_values = _values(candidate, "task_success_rate")
    delta, ci_low, ci_high = bootstrap_delta_ci(base_values, cand_values)

    reasons: list[str] = []
    if delta < cfg.min_delta:
        reasons.append(f"delta {delta:+.3f} < min_delta {cfg.min_delta:.3f}")
    if ci_low <= 0:
        reasons.append(f"CI95 lower bound {ci_low:+.3f} <= 0")

    base_p95 = baseline.get("p95_ms")
    cand_p95 = candidate.get("p95_ms")
    if isinstance(base_p95, (int, float)) and isinstance(cand_p95, (int, float)) and base_p95 > 0:
        regression = (cand_p95 - base_p95) / base_p95
        if regression > cfg.max_p95_regression:
            reasons.append(f"P95 regression {regression:+.1%} > {cfg.max_p95_regression:.0%}")

    base_p50 = baseline.get("p50_ms")
    cand_p50 = candidate.get("p50_ms")
    if isinstance(base_p50, (int, float)) and isinstance(cand_p50, (int, float)) and base_p50 > 0:
        regression = (cand_p50 - base_p50) / base_p50
        if regression > cfg.max_p50_regression:
            reasons.append(f"P50 regression {regression:+.1%} > {cfg.max_p50_regression:.0%}")

    base_compliance = baseline.get("compliance_pass_rate")
    cand_compliance = candidate.get("compliance_pass_rate")
    if isinstance(base_compliance, (int, float)) and isinstance(cand_compliance, (int, float)):
        if cand_compliance < max(cfg.min_compliance, base_compliance):
            reasons.append(f"compliance {cand_compliance:.3f} < baseline {base_compliance:.3f}")

    return {
        "decision": "pass" if not reasons else "fail",
        "delta": round(delta, 4),
        "ci95": [round(ci_low, 4), round(ci_high, 4)],
        "baseline_success": round(sum(base_values) / len(base_values), 4) if base_values else None,
        "candidate_success": round(sum(cand_values) / len(cand_values), 4) if cand_values else None,
        "reasons": reasons,
    }
