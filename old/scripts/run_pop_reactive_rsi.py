"""Run paired POP strategies against a merchant that can ask for missing information."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
import re
from statistics import median
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_scene_followup import load_scene_policy
from app.agent.pop_turn_intent import PopTurnIntent
from app.eval.pop_closure import KNOWLEDGE, replay, summarize
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_reactive import Merchant, QUESTIONS, normalize
from app.eval.pop_runner import digest, read_json, write_json
from app.rsi.pop_closure import VERIFIED_CLOSURE_RELEASE, load_policy

SOURCE = ROOT / "eval/pop/reactive_calls.v1.txt"
SPLITS = ("dev", "validation", "test")
PRESSURE = {"fault", "stop", "wrong"}


def load_suite():
    docs = {doc["id"][-3:]: doc for doc in read_json(KNOWLEDGE) if doc["id"].startswith("pop-scene-")}
    groups = {split: [] for split in SPLITS}
    case_families, assignments, seen = set(), {}, set()
    split = family = None
    for line in SOURCE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("["):
            split, family = line.strip("[]").split()
            if family in case_families:
                raise ValueError(f"Repeated family: {family}")
            case_families.add(family)
            index = 0
            continue
        doc, title, spec, user = [part.strip() for part in line.split("|")]
        topics = [] if spec in PRESSURE else spec.split("+")
        key = normalize(user)
        scene = docs[doc]["scene"]
        if (key in seen or assignments.setdefault(doc, split) != split or
                scene.get("mode") != "电话" or not scene.get("l3") or set(topics) - QUESTIONS.keys()):
            raise ValueError(f"Invalid or overlapping case: {line}")
        seen.add(key)
        index += 1
        case = {"id": f"{split}-{family}-{index}", "split": split, "family": family,
                "doc": doc, "title": title, "question": user, "topics": topics,
                "kind": spec if spec in PRESSURE else "normal", "profile": (index - 1) % 3,
                "mode": "pressure" if spec in PRESSURE else "normal", "turns": []}
        groups[split].append(case)
    for split, rows in groups.items():
        counts = Counter((row["family"], row["mode"]) for row in rows)
        if len(counts) != 8 or len(rows) != 20 or any(
            count != (3 if mode == "normal" else 2) for (_, mode), count in counts.items()
        ):
            raise ValueError(f"Unbalanced split: {split}")
    return groups


def metrics(rows):
    review = [row["simulator"] for row in rows]
    first_answer = [item["first_answer_complete"] for item in review if item["first_answer_complete"] is not None]
    return {**summarize(rows), "complete_first_answers": sum(first_answer),
            "first_answers_measured": len(first_answer),
            "requested_first_answers": sum(row["mode"] == "normal" for row in rows),
            "followup_questions": sum(item["questions"] for item in review),
            "repair_prompts": sum(len(item["retries"]) for item in review),
            "mean_questions_on_passed": round(sum(item["questions"] for row, item in zip(rows, review)
                                                if row["passed"]) / max(1, sum(row["passed"] for row in rows)), 2)}


def compare(control, candidate):
    old = {row["case"]: row for row in control["rows"]}
    new = {row["case"]: row for row in candidate["rows"]}
    if old.keys() != new.keys():
        raise ValueError("Unpaired cases")
    return {mode: {
        "before": control["groups"][mode], "after": candidate["groups"][mode],
        "improved": [key for key, row in old.items() if row["mode"] == mode and not row["passed"] and new[key]["passed"]],
        "regressed": [key for key, row in old.items() if row["mode"] == mode and row["passed"] and not new[key]["passed"]],
    } for mode in ("normal", "pressure")}


async def evaluate(rows, root, resolver, *, phone_style=None):
    policy = load_policy(read_json(VERIFIED_CLOSURE_RELEASE))
    scene = load_scene_policy(ROOT)
    workers = asyncio.Semaphore(4)

    async def one(index, case):
        async with workers:
            row = await replay(case, index, policy, scene, root, turn_intent=resolver,
                               resolve_followups=True, simulator=Merchant(case), phone_style=phone_style)
            return {**row, "mode": case["mode"], "kind": case["kind"]}

    evidence = await asyncio.gather(*(one(i, row) for i, row in enumerate(rows, 1)))
    calls = [turn["result"]["turn_intent"] for row in evidence for turn in read_json(Path(row["trace"]))["turns"]
             if turn["result"].get("turn_intent")]
    return {"rows": evidence, "groups": {mode: metrics([row for row in evidence if row["mode"] == mode])
                                            for mode in ("normal", "pressure")},
            "model_calls": {"count": len(calls), "status": dict(Counter(call["status"] for call in calls)),
                            "p50_ms": median(call["ms"] for call in calls) if calls else None}}


async def run(args):
    cases = load_suite()
    args.output.mkdir(parents=True, exist_ok=False)
    suite = args.output / "cases.json"
    write_json(suite, cases)
    manifest = {"cases": digest(suite), "source": digest(SOURCE),
                "scorer": digest(ROOT / "app/eval/pop_closure.py"),
                "simulator": digest(ROOT / "app/eval/pop_reactive.py"),
                "agent": digest(ROOT / "app/agent/pop_competition.py"),
                "arm": args.arm, "splits": args.splits}
    if args.compare:
        previous = read_json(args.compare / "manifest.json")
        if any(previous[key] != manifest[key] for key in ("cases", "source", "scorer", "simulator")):
            raise ValueError("Control evidence is from a different suite or scoring contract")
    if args.arm != "baseline" and any(split != "dev" for split in args.splits) and not args.lock:
        raise ValueError("Independent splits require a frozen development selection")
    config = JudgeConfig.from_env()
    resolver_type = PopTurnIntent
    if args.arm != "baseline":
        from app.agent.pop_turn_plan import PopTurnPlan

        resolver_type = PopTurnPlan
    async with httpx.AsyncClient(timeout=1.8, trust_env=False) as client:
        options = {"retry_writes": args.arm == "plan-retry"} if args.arm != "baseline" else {}
        resolver = resolver_type(client, config, budget_ms=1800, **options)
        manifest.update(intent=resolver.version, model=config.model,
                        plan_sha256=digest(ROOT / "app/agent/pop_turn_plan.py"))
        if args.lock:
            frozen = read_json(args.lock)
            current = {key: manifest[key] for key in frozen["fingerprint"]}
            if frozen["fingerprint"] != current:
                raise ValueError("Candidate, cases, or scoring contract changed after lock")
            manifest["lock_sha256"] = digest(args.lock)
        write_json(args.output / "manifest.json", manifest)
        comparisons = {}
        for split in args.splits:
            evidence = await evaluate(cases[split], args.output / split, resolver)
            write_json(args.output / split / "score.json", evidence)
            print(split, {k: (g["passed"], g["cases"], g["complete_first_answers"], g["repair_prompts"])
                          for k, g in evidence["groups"].items()}, evidence["model_calls"], flush=True)
            if args.compare:
                comparisons[split] = compare(read_json(args.compare / split / "score.json"), evidence)
                write_json(args.output / "comparison.json", comparisons)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "plan", "plan-retry"), default="baseline")
    parser.add_argument("--splits", nargs="+", choices=SPLITS, default=["dev"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--lock", type=Path)
    asyncio.run(run(parser.parse_args()))
