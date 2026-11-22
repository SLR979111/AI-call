"""Rank speech preparation from development and isolate ASR effects with paired replay."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_scene_followup import load_scene_policy
from app.agent.pop_turn_intent import PopTurnIntent
from app.agent.pop_turn_plan import PopTurnPlan, plan_reply
from app.eval.pop_closure import replay
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_reactive import answers, text_of
from app.eval.pop_runner import digest, read_json, write_json
from app.rsi.pop_closure import VERIFIED_CLOSURE_RELEASE, load_policy
from audit_pop_closure_voice import audit

TOPICS = ("action", "channel", "restate", "review")


def lock_development(candidates, output):
    eligible = []
    for candidate in candidates:
        manifest = read_json(candidate / "manifest.json")
        manifest.setdefault("plan_sha256", digest(ROOT / "app/agent/pop_turn_plan.py"))
        comparison = read_json(candidate / "comparison.json")
        if set(comparison) != {"dev"}:
            raise ValueError("Candidate selection accepts development evidence exclusively")
        normal = comparison["dev"]["normal"]
        pressure = comparison["dev"]["pressure"]
        left, right = normal["before"], normal["after"]
        if (right["passed"] < left["passed"] or right["complete_first_answers"] < left["complete_first_answers"] + 3
                or any(group["after"]["severe_cases"] or group["regressed"] for group in (normal, pressure))):
            continue
        calls = read_json(candidate / "dev/score.json")["model_calls"]["count"]
        eligible.append((right["passed"], right["complete_first_answers"],
                         pressure["after"]["closed_correctly"], -calls, manifest["arm"], candidate, manifest))
    if not eligible:
        raise SystemExit("No eligible candidate on the prespecified development criteria")
    *_, candidate, manifest = max(eligible, key=lambda item: item[:-2])
    keys = ("cases", "source", "scorer", "simulator", "agent", "arm", "intent", "model", "plan_sha256")
    write_json(output, {"fingerprint": {key: manifest[key] for key in keys},
                        "development": str(candidate), "development_comparison_sha256": digest(candidate / "comparison.json"),
                        "criteria": "no paired losses or severe cases; at least three more complete first answers on normal development calls"})
    print(manifest["arm"], flush=True)


def rank_demand(report, cases):
    case_map = {row["id"]: row for row in cases}
    demand = []
    for row in report["rows"]:
        if row["mode"] != "normal":
            continue
        case = case_map[row["case"]]
        turns = read_json(Path(row["trace"]))["turns"]
        index = next((i for i, turn in enumerate(turns) if turn["user_text"] == case["question"]), None)
        if index is None:
            demand.append({"case": row["case"], "asked": False})
            continue
        before, turn = turns[:index], turns[index]
        delivered = {sentence for earlier in before for sentence in earlier["sentences"]}
        first = next(iter(turn["sentences"]), "")
        remedy = turn["result"].get("remediation", "")
        reason = next((text.removeprefix("审核问题是").rstrip("。") for text in delivered
                       if text.startswith("审核问题是")), "")
        matched = [topic for topic in TOPICS if (lines := plan_reply([topic], reason, remedy)) and lines[0] == first]
        demand.append({"case": row["case"], "asked": True,
                       "baseline_pcm": bool(first and first in delivered),
                       "topics": matched if row["simulator"]["first_answer_complete"] else [],
                       "complete_first_answer": bool(row["simulator"]["first_answer_complete"])})
    return demand


def ranking(dev, final, output):
    source = read_json(dev / "cases.json")
    final_cases = read_json(final / "cases.json")
    if digest(dev / "cases.json") != digest(final / "cases.json"):
        raise ValueError("Development and holdout case inventory differs")
    first = rank_demand(read_json(dev / "dev/score.json"), source["dev"])
    counts = Counter(topic for row in first if row["asked"] and not row["baseline_pcm"] for topic in row["topics"])
    ranked = sorted(TOPICS, key=lambda topic: (-counts[topic], topic))
    report = {"ranking": ranked, "development_eligible_targets": dict(counts),
              "scope": "post-offer TTS slot replay; only complete first answers and exact rendered sentences count"}
    for split in ("validation", "test"):
        demand = rank_demand(read_json(final / split / "score.json"), final_cases[split])
        reachable = [row for row in demand if row["asked"]]
        report[split] = {"asked": len(reachable), "rows": demand, "strategies": {
            str(k): {
                "slots": len(reachable) * k,
                "additional_covered_first_answers": sum(
                    not row["baseline_pcm"] and bool(set(ranked[:k]).intersection(row["topics"])) for row in reachable
                ),
                "already_reusable_first_answers": sum(
                    row["baseline_pcm"] and row["complete_first_answer"] for row in reachable
                ),
            } for k in range(len(ranked) + 1)
        }}
    write_json(output, report)
    print({split: report[split]["strategies"] for split in ("validation", "test")}, flush=True)


def coverage(spec, turns):
    offer = turns[2]
    cause = next((line.removeprefix("审核问题是") for line in offer["sentences"]
                  if line.startswith("审核问题是")), "")
    return sorted(answers(spec["topics"], text_of(turns[3]), cause, offer.get("result", {}).get("remediation", "")))


async def diagnose(folder, plan_path, output):
    plan = read_json(plan_path)
    live = audit(folder, plan)
    policy = load_policy(read_json(VERIFIED_CLOSURE_RELEASE))
    scene = load_scene_policy(ROOT)
    config = JudgeConfig.from_env()
    runtime = read_json(folder / "contest-runtime.json")
    mode = runtime["turn_intent"]
    budget = runtime.get("intent_budget_ms", 1800)
    report = {key: live[key] for key in ("audio_sha256", "first_sound_p50_ms", "first_sound_p95_ms", "passed", "records")}
    async with httpx.AsyncClient(timeout=budget / 1000, trust_env=False) as client:
        resolver = (PopTurnPlan(client, config, budget_ms=budget, retry_writes=mode == "plan-retry")
                    if mode in {"plan", "plan-retry"} else PopTurnIntent(client, config, budget_ms=budget))
        if resolver.version != runtime["intent_version"]:
            raise ValueError("Text counterfactual uses a different strategy")
        rows = []
        for index, (spec, call) in enumerate(zip(plan["calls"], live["calls"], strict=True), 1):
            heard = [turn["user_text"] for turn in call["turns"][1:]]
            reference = [plan["samples"][step[0]] for step in spec["turns"]]
            expected = set(spec["topics"])
            voice_covered = coverage(spec, call["turns"])
            row = {"id": spec["id"], "voice_closed": call["passed"], "voice_covered": voice_covered,
                   "voice_complete": call["passed"] and set(voice_covered) == expected, "expected": sorted(expected)}
            for label, words in (("reference", reference), ("asr", heard)):
                steps = [[user, *step[1:]] for user, step in zip(words, spec["turns"], strict=True)]
                case = {"id": spec["id"], "family": spec["doc"], "doc": spec["doc"][-3:], "turns": steps}
                result = await replay(case, index, policy, scene, output.parent / output.stem / label,
                                      turn_intent=resolver, resolve_followups=True)
                trace = read_json(Path(result["trace"]))
                covered = coverage(spec, trace["turns"])
                row[label] = {"closed": result["passed"], "covered": covered,
                              "complete": result["passed"] and set(covered) == expected,
                              "failures": result["failure_labels"], "trace": result["trace"]}
            row["reference_words"], row["asr_words"] = reference, heard
            if row["voice_complete"]:
                row["diagnosis"] = "complete"
            elif row["reference"]["complete"] and not row["asr"]["complete"]:
                row["diagnosis"] = "ASR-sensitive"
            elif not row["reference"]["complete"]:
                row["diagnosis"] = "fails_with_reference"
            else:
                row["diagnosis"] = "voice_or_model_variance"
            rows.append(row)
    report["calls"] = rows
    write_json(output, report)
    print([(row["id"], row["voice_complete"], row["diagnosis"]) for row in rows], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    lock = sub.add_parser("lock")
    lock.add_argument("--candidates", type=Path, nargs="+", required=True)
    lock.add_argument("--output", type=Path, required=True)
    rank = sub.add_parser("rank")
    rank.add_argument("--dev", type=Path, required=True)
    rank.add_argument("--final", type=Path, required=True)
    rank.add_argument("--output", type=Path, required=True)
    voice = sub.add_parser("voice")
    voice.add_argument("--run", type=Path, required=True)
    voice.add_argument("--plan", type=Path, required=True)
    voice.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "lock":
        lock_development(args.candidates, args.output)
    elif args.command == "rank":
        ranking(args.dev, args.final, args.output)
    else:
        asyncio.run(diagnose(args.run, args.plan, args.output))
