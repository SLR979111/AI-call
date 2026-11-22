"""Replay identical follow-up cases with the last POP policy and its candidate."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from eval_pop import ROOT, evaluate, paired, summarize
from app.eval.pop_runner import KNOWLEDGE, digest, load_suites, read_json, write_json
from app.rsi.pop_strategy import propose_followup_policy

PREVIOUS_RUN = ROOT / "data/pop-selftest-2026-11-07"
SEEDS = ROOT / "eval/pop/followup_cases.json"


def compile_cases() -> list[dict]:
    knowledge = {doc["id"]: doc for doc in read_json(KNOWLEDGE)}
    cases = []
    for index, seed in enumerate(read_json(SEEDS), start=1):
        doc = knowledge[seed["doc"]]
        scene = doc["scene"]
        request_id = f"Q981{index:03}"
        steps = [{"say": "您好，您说。"}]
        if not seed.get("unverified"):
            steps.append({
                "say": "是的，我是店铺负责人。", "verifies_identity": True,
                "if_previous_matches": ["负责人"], "otherwise": "您是要先核对身份吗？",
            })
        steps.extend({"say": seed[field]} for field in ("question", "last"))
        expected = {
            "route": seed["route"], "min_turns": len(steps),
            "forbidden_terms": ["保证审核通过", "一定能通过", "SYN-CONTACT-FULL"],
        }
        if "reason" in seed:
            expected["reason_groups"] = [seed["reason"]]
        if "remedy" in seed:
            expected["remedy_groups"] = [seed["remedy"]]
        cases.append({
            "case_id": seed["id"], "split": "holdout", "family": seed["doc"], "source": {"doc_id": seed["doc"]},
            "context": {"brand": seed["brand"], "request_id": request_id,
                        **{f"reject_{level}": scene[level] for level in ("l1", "l2", "l3")}},
            "expected": expected, "steps": steps, "tool_fault": seed.get("fault"),
        })
    return cases


def policies(previous_run: Path) -> tuple[dict, dict]:
    previous = read_json(previous_run / "releases/candidate.json")
    judge = read_json(previous_run / "judge_cloud.json")
    failures = sum(
        row["release_id"] == previous["release_id"]
        and row.get("scores", {}).get("dialogue_fit", {}).get("verdict") == "fail"
        for row in judge["reviews"]
    )
    return previous, propose_followup_policy(previous, failures)


async def run(args):
    previous, candidate = policies(args.previous)
    cases = compile_cases()
    if args.demo:
        cases = [case for case in cases if case["case_id"] == args.demo]
        if not cases:
            raise ValueError("Unknown demo ID")
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "cases.json", cases)
    write_json(args.output / "releases/baseline.json", previous)
    write_json(args.output / "releases/candidate.json", candidate)
    before = await evaluate(cases, previous, args.output)
    after = await evaluate(cases, candidate, args.output)
    write_json(args.output / "results.json", {
        "metadata": {
            "scope": "fresh targeted text follow-ups; scenario labels reused; no acoustic measurement",
            "seed_sha256": digest(SEEDS),
            "rules_sha256": digest(ROOT / "app/eval/pop_rules.py"),
            "agent_sha256": digest(ROOT / "app/agent/pop_strategy.py"),
            "previous": summarize(before),
            "candidate": summarize(after),
            "paired": paired(before + after, previous["release_id"], candidate["release_id"], "holdout"),
        },
        "results": before + after,
    })
    if args.demo:
        for label, row in (("上一轮", before[0]), ("这一轮", after[0])):
            trace = read_json(args.output / row["trace"])
            print(f"\n{label} · {row['release_id']} · {row['status']}")
            for turn in trace["turns"]:
                print(f"商家　{turn['user_text']}\n客服　{' '.join(turn['sentences'])}")
            print("证据　" + str(args.output / row["events"]))
        return

    legacy = load_suites(args.previous / "cases.json")
    regression = args.output / "regression"
    old = read_json(args.previous / "results.json")["results"]
    new = []
    comparison = {}
    for split in ("validation", "holdout"):
        baseline_rows = [row for row in old if row["release_id"] == previous["release_id"] and row["split"] == split]
        candidate_rows = await evaluate(legacy[split], candidate, regression)
        new.extend(candidate_rows)
        comparison[split] = {
            "before": summarize(baseline_rows), "after": summarize(candidate_rows),
            **paired(baseline_rows + candidate_rows, previous["release_id"], candidate["release_id"], split),
        }
    write_json(regression / "results.json", {"metadata": comparison, "results": new})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, default=PREVIOUS_RUN)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", help="live replay one case, for example renewed-shop or callback-channel")
    asyncio.run(run(parser.parse_args()))
