"""Reproducible, opt-in POP text evaluation and offline RSI experiment."""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.eval.pop_runner import KNOWLEDGE, SAFE_ID, SPLITS, digest, load_suites, read_json, run_case, write_json
from app.rsi.pop_strategy import baseline_policy, propose_policy, strategy_prompt

UPSTREAM = "c430d49314e955db59d28b761960e408512e0285"
DEFAULT_CASES = ROOT / "eval/pop/cases.json"


async def evaluate(cases: list[dict], policy: dict, output: Path) -> list[dict]:
    release = policy["release_id"]
    if not SAFE_ID.fullmatch(release):
        raise ValueError("invalid release ID")
    rows = []
    for case in cases:
        rows.append(await run_case(case, policy, output / "raw"))
    successes = sum(row["task_success"] for row in rows)
    print(f"{release} {cases[0]['split']}: {successes}/{len(cases)}", flush=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    return {
        "cases": len(rows),
        "passed": sum(row["task_success"] for row in rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "serious_violations": sum(row["serious_violations"] for row in rows),
        "failure_clusters": dict(Counter(label for row in rows for label in set(row["failure_labels"])).most_common()),
    }


def paired(rows: list[dict], baseline: str, candidate: str, split: str) -> dict:
    before = {row["case_id"]: row for row in rows if row["split"] == split and row["release_id"] == baseline}
    after = {row["case_id"]: row for row in rows if row["split"] == split and row["release_id"] == candidate}
    if not before or set(before) != set(after):
        raise ValueError(f"unpaired cases: {split}")
    improved = [key for key in before if not before[key]["task_success"] and after[key]["task_success"]]
    regressed = [key for key in before if before[key]["task_success"] and not after[key]["task_success"]]
    return {"improved": improved, "regressed": regressed, "cases": len(before),
            "delta_percentage_points": round((len(improved) - len(regressed)) * 100 / len(before), 2)}


async def run(args):
    suites = load_suites(args.cases)
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.cases, args.output / "cases.json")
    baseline = baseline_policy()
    baseline_id = baseline["release_id"]
    baseline_path = args.output / "releases" / "baseline.json"
    write_json(baseline_path, baseline)
    rollback = None
    if args.policy:
        policy = read_json(args.policy)
        rows = await evaluate(suites[args.split], policy, args.output)
        paths = [baseline_path]
        if policy != baseline:
            selected_path = args.output / "releases" / "selected.json"
            write_json(selected_path, policy)
            paths.append(selected_path)
        comparison = {}
    else:
        dev = await evaluate(suites["dev"], baseline, args.output)
        candidate = propose_policy(dev, baseline)
        candidate_id = candidate["release_id"]
        if candidate_id == baseline_id:
            raise ValueError("candidate did not create a distinct release")
        candidate_path = args.output / "releases" / "candidate.json"
        write_json(candidate_path, candidate)
        prompt_path = args.output / "releases" / "candidate_prompt.md"
        prompt_path.write_text(strategy_prompt(candidate) + "\n", encoding="utf-8")
        paths = [baseline_path, candidate_path]
        rows = list(dev)
        for split in SPLITS[1:]:
            for path in paths:
                rows.extend(await evaluate(suites[split], read_json(path), args.output))
        comparison = {split: paired(rows, baseline_id, candidate_id, split) for split in SPLITS[1:]}
        restored_rows = await evaluate(suites["validation"], read_json(baseline_path), args.output / "rollback")
        original = {row["case_id"]: row for row in rows
                    if row["split"] == "validation" and row["release_id"] == baseline_id}
        restored = {row["case_id"]: row for row in restored_rows}
        compared_fields = ("status", "task_success", "serious_violations", "failure_labels")
        identical = [key for key in original
                     if all(original[key][field] == restored[key][field] for field in compared_fields)]
        rollback = {"restored_release": baseline_id, "identical_cases": len(identical),
                    "cases": len(original), "different_case_ids": sorted(set(original) - set(identical))}
        write_json(args.output / "rollback/results.json", restored_rows)

    metadata = {
        "upstream_commit": UPSTREAM,
        "scope": "text / synthetic merchants / local mock business / no acoustic measurements",
        "dataset_sha256": digest(args.output / "cases.json"),
        "knowledge_sha256": digest(KNOWLEDGE),
        "rules_sha256": digest(ROOT / "app/eval/pop_rules.py"),
        "strategy_sha256": digest(ROOT / "app/rsi/pop_strategy.py"),
        "implementation": {path: digest(ROOT / path) for path in (
            "scripts/eval_pop.py", "app/eval/pop_runner.py", "app/orchestrator.py",
            "app/agent/pop_audit.py", "app/agent/pop_strategy.py",
        )},
        "releases": {str(path.relative_to(args.output).as_posix()): digest(path) for path in paths},
        "suites": {split: {"cases": len(suite), "families": len({case["family"] for case in suite})}
                   for split, suite in suites.items()},
        "summaries": {f"{release}/{split}": summarize([row for row in rows if row["release_id"] == release
                                                     and row["split"] == split])
                      for release, split in dict.fromkeys((row["release_id"], row["split"]) for row in rows)},
        "comparison": comparison,
        "rollback_replay": rollback,
    }
    write_json(args.output / "results.json", {"metadata": metadata, "results": rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, required=True, help="new directory for logs and scores")
    parser.add_argument("--policy", type=Path, help="replay one saved release, including the baseline")
    parser.add_argument("--split", choices=SPLITS, default="validation", help="partition for --policy")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
