#!/usr/bin/env python3
"""Compare a baseline and candidate eval report and apply the regression gate.

Usage:
  python scripts/rsi_compare.py --baseline data/baseline.json --candidate data/candidate.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rsi.gate import GateConfig, compare


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--min-delta", type=float, default=0.03)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    result = compare(baseline, candidate, GateConfig(min_delta=args.min_delta))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"decision: {result['decision']}")
        print(f"delta:    {result['delta']:+.3f}  CI95={result['ci95']}")
        print(f"baseline: {result['baseline_success']}  candidate: {result['candidate_success']}")
        for reason in result["reasons"]:
            print(f"  - {reason}")
    return 0 if result["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
