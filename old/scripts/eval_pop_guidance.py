"""Replay practical remediation queries against previous and guided POP policies."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from eval_pop_dialogue import metrics, run_case
from app.agent.pop_repair import GUIDED, REPAIRED
from app.eval.pop_guidance_cases import cases_for
from app.eval.pop_runner import KNOWLEDGE, digest, read_json, write_json


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    cases = cases_for(read_json(KNOWLEDGE))
    cases_path = args.output / "cases.json"
    write_json(cases_path, {"cases": cases})
    prior = [await run_case(case, REPAIRED, args.output, "previous") for case in cases]
    revised = [await run_case(case, GUIDED, args.output, "guided") for case in cases]
    rollback = [await run_case(case, REPAIRED, args.output, "rollback")
                for case in cases if case["split"] == "validation"]
    summary = {}
    for split in ("dev", "validation", "holdout"):
        summary[split] = {name: metrics([row for row in rows if row["split"] == split])
                          for name, rows in (("previous", prior), ("guided", revised))}
    fields = ("case_id", "first_ask_answered", "task_success", "failure_labels")
    expected = [row for row in prior if row["split"] == "validation"]
    matched = sum(all(a[key] == b[key] for key in fields) for a, b in zip(expected, rollback, strict=True))
    payload = {"case_hash": digest(cases_path), "knowledge_hash": digest(KNOWLEDGE), "summary": summary,
               "policies": {policy.version: asdict(policy) for policy in (REPAIRED, GUIDED)},
               "results": prior + revised, "rollback": {"matches": matched, "cases": len(rollback), "results": rollback}}
    if args.regression:
        frozen = read_json(args.regression)
        regression = [await run_case(case, GUIDED, args.output, "regression") for case in frozen["cases"]]
        payload["regression"] = {"case_hash": digest(args.regression), "summary": metrics(regression),
                                 "results": regression}
    write_json(args.output / "results.json", payload)
    print(json.dumps({"summary": summary, "rollback": f"{matched}/{len(rollback)}",
                      "regression": (payload.get("regression") or {}).get("summary")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--regression", type=Path, help="Optional frozen cases.json from the previous round")
    asyncio.run(run(parser.parse_args()))
