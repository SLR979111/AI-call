"""Run one development-driven POP closure iteration and replay its rollback."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_scene_followup import load_scene_policy
from app.eval.pop_closure import KNOWLEDGE, load_cases, replay, summarize
from app.eval.pop_judge import DIMENSIONS, JudgeConfig, conversation, review
from app.eval.pop_runner import digest, read_json, write_json
from app.rsi.pop_closure import RULES, load_policy, propose, release


async def evaluate(cases, profile, scene_policy, path):
    rows = [await replay(case, index, load_policy(profile), scene_policy, path)
            for index, case in enumerate(cases, 1)]
    results = {"summary": summarize(rows), "rows": rows}
    write_json(path / "score.json", results)
    print(path.name, results["summary"], flush=True)
    return results


def compare(before, after):
    b = {row["case"]: row for row in before["rows"]}
    a = {row["case"]: row for row in after["rows"]}
    if not b or b.keys() != a.keys():
        raise ValueError("Unpaired closure results")
    gains = [key for key in b if not b[key]["passed"] and a[key]["passed"]]
    losses = [key for key in b if b[key]["passed"] and not a[key]["passed"]]
    return {"gains": gains, "losses": losses, "net_gain": len(gains) - len(losses),
            "correct_write_gain": after["summary"]["closed_correctly"] - before["summary"]["closed_correctly"],
            "passes_gate": len(gains) - len(losses) >= 2 and not losses
                           and after["summary"]["closed_correctly"] > before["summary"]["closed_correctly"]
                           and after["summary"]["severe_cases"] == 0}


async def blind_review(before, after, output):
    config = JudgeConfig.from_env()
    packets = [(arm, row) for arm, report in (("before", before), ("after", after)) for row in report["rows"]]
    random.Random(20261122).shuffle(packets)
    async with httpx.AsyncClient(timeout=20) as client:
        slots = asyncio.Semaphore(4)

        async def grade(arm, row):
            async with slots:
                evidence = await review(client, config, conversation(read_json(Path(row["trace"]))))
            return {"arm": arm, "case": row["case"], "review": evidence}

        rows = await asyncio.gather(*(grade(arm, row) for arm, row in packets))
    counts = {arm: {key: {} for key in DIMENSIONS} for arm in ("before", "after")}
    for row in rows:
        reviews = row["review"].get("scores", {})
        for dimension in DIMENSIONS:
            verdict = (reviews.get(dimension) or {}).get("verdict", "unknown")
            group = counts[row["arm"]][dimension]
            group[verdict] = group.get(verdict, 0) + 1
    write_json(output / "blind-review.json", {"model": config.model, "summary": counts, "rows": rows})
    return counts


async def run(args):
    cases = load_cases(args.cases)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.cases, root / "cases.json")
    scene_policy = load_scene_policy(ROOT)
    base = release([])
    write_json(root / "baseline.json", base)
    write_json(root / "manifest.json", {
        "dataset": digest(root / "cases.json"), "knowledge": digest(KNOWLEDGE),
        "scene_policy": scene_policy.version if scene_policy else None,
        "baseline": base, "split_cases": {key: [row["id"] for row in rows] for key, rows in cases.items()},
        "scorer": digest(ROOT / "app/eval/pop_closure.py"),
        "agent": digest(ROOT / "app/agent/pop_competition.py"),
        "confirmation_rules": digest(ROOT / "app/agent/pop_confirmation.py"),
        "gate": {"minimum_additional_correct_calls": 2, "require_more_correct_writes": True,
                 "new_regressions_allowed": 0, "severe_cases_allowed": 0},
        "scope": "same competition agent and local SQLite tool, synthetic text replay. Voice tracked separately.",
    })
    dev = await evaluate(cases["dev"], base, scene_policy, root / "dev-before")
    candidates = propose(dev["rows"])
    trials = []
    for profile in candidates:
        policy = root / "candidates" / (profile["version"] + ".json")
        write_json(policy, profile)
        output = await evaluate(cases["dev"], profile, scene_policy, root / ("dev-" + profile["version"]))
        trials.append({"release": profile, "summary": output["summary"], "comparison": compare(dev, output),
                       "changes": {flag: RULES[flag][1] for flag in profile["enabled"]}})
    write_json(root / "dev-trials.json", {"baseline": dev["summary"], "trials": trials})
    eligible = [trial for trial in trials if trial["comparison"]["passes_gate"]]
    if not eligible or args.dev_only:
        write_json(root / "summary.json", {"baseline": base, "development": dev["summary"],
                   "text_gate_passed": False, "voice_gate": "not_run",
                   "decision": "development_only" if args.dev_only else "retain_baseline"})
        print("Development exploration complete", flush=True)
        return
    selected = max(eligible, key=lambda trial: (trial["comparison"]["net_gain"],
                                                trial["comparison"]["correct_write_gain"],
                                                -len(trial["release"]["enabled"])))
    profile = selected["release"]
    write_json(root / "selected-before-validation.json", selected)
    write_json(root / "candidate.json", profile)
    validation_before = await evaluate(cases["validation"], base, scene_policy, root / "validation-before")
    validation_after = await evaluate(cases["validation"], profile, scene_policy, root / "validation-after")
    validation = compare(validation_before, validation_after)
    write_json(root / "locked-selection.json", {"candidate": profile, "validation": validation,
                                                "role": "promotion_check" if validation["passes_gate"] else "observation"})
    test_before = await evaluate(cases["test"], base, scene_policy, root / "test-before")
    test_after = await evaluate(cases["test"], profile, scene_policy, root / "test-after")
    holdout = compare(test_before, test_after)
    rollback = await evaluate(cases["validation"], load_saved_release(root / "baseline.json"),
                              scene_policy, root / "rollback")
    keys = ("case", "passed", "failure_labels", "record_count")
    restored = sum(all(left[key] == right[key] for key in keys)
                   for left, right in zip(validation_before["rows"], rollback["rows"], strict=True))
    text_gate = validation["passes_gate"] and holdout["passes_gate"] and restored == len(cases["validation"])
    summary = {
        "release": profile, "selected_changes": selected["changes"],
        "development": {"before": dev["summary"], "after": selected["summary"]},
        "validation": {"before": validation_before["summary"], "after": validation_after["summary"], **validation},
        "test": {"before": test_before["summary"], "after": test_after["summary"], **holdout},
        "rollback": {"reproduced": restored, "cases": len(cases["validation"])},
        "text_gate_passed": text_gate, "voice_gate": "pending",
    }
    write_json(root / "summary.json", summary)
    if args.judge:
        summary["blind_review"] = await blind_review(test_before, test_after, root)
        write_json(root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def load_saved_release(path):
    payload = read_json(path)
    load_policy(payload)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "eval/pop/closure_cases.v1.json")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--dev-only", action="store_true")
    asyncio.run(run(parser.parse_args()))
