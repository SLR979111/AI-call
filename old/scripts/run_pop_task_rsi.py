"""Evaluate the prior POP release and subsequent candidates on new calls."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_scene_followup import load_scene_policy
from app.agent.pop_turn_intent import PopTurnIntent
from app.eval.pop_closure import load_cases, replay, summarize
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_runner import digest, read_json, write_json
from app.rsi.pop_closure import VERIFIED_CLOSURE_RELEASE, load_policy, release

SOURCE = ROOT / "eval/pop/closure_cases.v3.txt"
PREVIOUS = read_json(VERIFIED_CLOSURE_RELEASE)
TOPICS = {"channel", "review", "private", "approval", "unknown", "action", "repeat", "confusion"}
IDENTITY, NEED, OFFER, WRITE, STOP = ("pending_identity", "pending_need", "pending_result",
                                      "pop_contact_recorded", "safe_exit")


def build_suite(source):
    cases, index, seen = [], 0, set()
    old = read_json(ROOT / "eval/pop/closure_cases.v1.json")["cases"]
    old_turns = {identity_key(turn[0]) for call in old for turn in call["turns"]}
    split = family = None
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("["):
            split, family = line.strip("[]").split()
            index = 0
            continue
        index += 1
        parts = [part.strip() for part in line.split("|")]
        mode = "normal" if index <= 4 else "pressure"
        doc, identity = parts[:2]
        turn_checks = {}
        if identity.startswith("!wrong:"):
            turns = [[identity.removeprefix("!wrong:"), STOP]]
        else:
            doc, identity, availability, followup, ending = parts
            turns = [[identity, NEED], [availability, OFFER]]
            if followup.startswith("!stop:"):
                turns.append([followup.removeprefix("!stop:"), STOP])
            else:
                if followup:
                    topic, question = followup.split(":", 1)
                    if topic not in TOPICS:
                        raise ValueError(f"{source}:{line_number}: unknown follow-up type")
                    turns.append([question, OFFER])
                    turn_checks[str(len(turns))] = topic
                if not ending:
                    raise ValueError(f"{source}:{line_number}: confirmation required")
                turns.append([ending, WRITE, "returned"])
        for utterance, *_ in turns:
            key = identity_key(utterance)
            if key in old_turns or key in seen:
                raise ValueError(f"{source}:{line_number}: repeated utterance {utterance!r}")
            seen.add(key)
        cases.append({"id": f"{split}-{family}-{index}", "split": split, "family": family, "doc": doc,
                      "mode": mode, "turns": turns, "followup_checks": turn_checks, "seed_line": line_number})
    for partition in ("dev", "validation", "test"):
        groups = {case["family"] for case in cases if case["split"] == partition}
        if len(groups) != 4 or any(sum(case["family"] == family for case in cases) != 6 for family in groups):
            raise ValueError(f"Incorrect family coverage in {partition}")
    return {"schema": 3, "cases": cases}


def identity_key(utterance):
    return re.sub(r"[\W_]+", "", utterance)


async def evaluate(cases, profile, scene, path, *, turn_intent=None, resolve_followups=False):
    slots = asyncio.Semaphore(5)
    policy = load_policy(profile)

    async def run_case(index, case):
        async with slots:
            result = await replay(case, index, policy, scene, path, turn_intent=turn_intent,
                                  resolve_followups=resolve_followups)
            return {**result, "mode": case["mode"]}

    rows = await asyncio.gather(*(run_case(index, case) for index, case in enumerate(cases, 1)))
    report = {"groups": {key: summarize([row for row in rows if row["mode"] == key])
                         for key in ("normal", "pressure")}, "rows": rows}
    write_json(path / "score.json", report)
    return report


def paired(before, after):
    left = {row["case"]: row for row in before["rows"]}
    right = {row["case"]: row for row in after["rows"]}
    if left.keys() != right.keys():
        raise ValueError("Unpaired case reports")
    return {mode: {
        "before": before["groups"][mode], "after": after["groups"][mode],
        "gains": [i for i, row in left.items() if row["mode"] == mode and not row["passed"] and right[i]["passed"]],
        "losses": [i for i, row in left.items() if row["mode"] == mode and row["passed"] and not right[i]["passed"]],
    } for mode in ("normal", "pressure")}


async def baseline_run(source, output, splits):
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "cases.json"
    write_json(frozen, build_suite(source))
    cases = load_cases(frozen)
    scene = load_scene_policy(ROOT)
    write_json(output / "manifest.json", {"cases_sha256": digest(frozen), "source_sha256": digest(source),
        "scorer_sha256": digest(ROOT / "app/eval/pop_closure.py"), "scene_policy": scene.version if scene else None,
        "previous": PREVIOUS, "baseline": release([]), "scope": "POP text and local SQLite, audio verified separately"})
    comparisons = {}
    for split in splits:
        before = await evaluate(cases[split], release([]), scene, output / split / "baseline")
        after = await evaluate(cases[split], PREVIOUS, scene, output / split / "previous")
        comparisons[split] = paired(before, after)
        for mode, values in comparisons[split].items():
            left, right = values["before"], values["after"]
            print(f"{split}/{mode}: {left['passed']}/{left['cases']} -> {right['passed']}/{right['cases']}", flush=True)
        write_json(output / "prior-comparison.json", comparisons)


async def experiment(prior, output, modes, splits, budget_ms):
    output.mkdir(parents=True, exist_ok=False)
    frozen = prior / "cases.json"
    manifest = read_json(prior / "manifest.json")
    if digest(frozen) != manifest["cases_sha256"] or manifest["previous"] != PREVIOUS:
        raise ValueError("Prior dataset or policy changed")
    cases = load_cases(frozen)
    scene = load_scene_policy(ROOT)
    config = JudgeConfig.from_env()
    async with httpx.AsyncClient(timeout=budget_ms / 1000, trust_env=False) as client:
        intent = PopTurnIntent(client, config, budget_ms=budget_ms)
        write_json(output / "manifest.json", {"cases_sha256": digest(frozen), "intent": intent.version,
            "model": config.model, "budget_ms": budget_ms, "modes": modes, "splits": splits,
            "agent_sha256": digest(ROOT / "app/agent/pop_competition.py"),
            "resolver_sha256": digest(ROOT / "app/agent/pop_turn_intent.py"),
            "scorer_sha256": digest(ROOT / "app/eval/pop_closure.py"),
            "baseline": PREVIOUS, "scope": "text plus actual local SQLite receipts"})
        summary = {}
        for split in splits:
            before = read_json(prior / split / "previous/score.json")
            summary[split] = {}
            for mode in modes:
                after = await evaluate(cases[split], PREVIOUS, scene, output / split / mode,
                                       turn_intent=intent, resolve_followups=mode == "grounded")
                comparison = paired(before, after)
                summary[split][mode] = comparison
                for group, row in comparison.items():
                    a, b = row["before"], row["after"]
                    print(f"{split}/{mode}/{group}: {a['passed']}/{a['cases']} -> {b['passed']}/{b['cases']}; "
                          f"severe cases {a['severe_cases']} -> {b['severe_cases']}", flush=True)
                write_json(output / "comparisons.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--splits", nargs="+", default=["dev", "validation", "test"],
                        choices=["dev", "validation", "test"])
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--modes", nargs="+", choices=["stance", "grounded"], default=["stance", "grounded"])
    parser.add_argument("--budget-ms", type=int, default=1800)
    args = parser.parse_args()
    if args.prior:
        asyncio.run(experiment(args.prior, args.output, args.modes, args.splits, args.budget_ms))
    else:
        asyncio.run(baseline_run(args.source, args.output, args.splits))
