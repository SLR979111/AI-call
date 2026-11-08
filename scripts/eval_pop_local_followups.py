"""Compare rule and local model follow-up routing through the real POP agent."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx

from eval_pop_dialogue import metrics, run_case
from app.agent.pop_local_topics import MODEL_ALIAS, MODEL_BUDGET_MS, PROMPT_B, PROMPT_C, LocalTopics
from app.agent.pop_repair import GUIDED
from app.eval.pop_runner import KNOWLEDGE, ROOT, digest, read_json, write_json
from app.reporting import _percentile

FIXTURES = ROOT / "eval/pop/local_followups.json"
ACTION_ANCHORS = {
    "034": ("更新已失效的主体资质", "上传有效材料"),
    "030": ("购货发票", "授权链路"),
    "009": ("授权",),
    "027": ("注册证",),
}


def cases_for(split):
    docs = {d["id"]: d["scene"] for d in read_json(KNOWLEDGE)}
    cases = []
    for index, (case_id, number, first, retry, topics, *metadata) in enumerate(read_json(FIXTURES)[split]):
        doc_id = f"pop-scene-{number}"
        scene = docs[doc_id]
        reason = "".join(scene["l3"].split())
        checks = {
            "channel": {"channel": ("京麦",), "original": ("原申请",)},
            "process": {"channel": ("京麦",), "steps": ("对照原申请的驳回意见",)},
            "blocked": {"support": ("联系审核支持",), "original": ("原申请",)},
            "submit": {"submit": ("重新提交",), "review": ("等待复审",)},
            "review": {"unknown_time": ("无法核实复审时间",), "status": ("查看进度",)},
            "material": {"material": ("按本单的整改要求",)},
            "approval": {"review": ("复审",), "condition": ("为准",)},
            "private": {"refusal": ("无法在电话里提供",)},
            "reason": {"reason": (reason,)},
        }
        answer_checks = {key: value for topic in topics for key, value in checks[topic].items()}
        if set(topics) & {"material", "process"}:
            answer_checks.update({f"grounding_{i}": (anchor,)
                                  for i, anchor in enumerate(ACTION_ANCHORS[number])})
        cases.append({
            "case_id": case_id, "split": split, "family": scene["l2"], "doc_id": doc_id,
            "scenario": metadata[1] if len(metadata) > 1 else "followup",
            "qa_source": metadata[0] if metadata else "",
            "identity": "对，我是负责人", "available": "现在方便",
            "phrases": [first, retry] if first else [], "expected_topics": topics,
            "answer_checks": answer_checks, "reason": reason,
            "context": {"request_id": f"Q{994000 + index}", "brand": "合成测试商家",
                        **{f"reject_{key}": scene[key] for key in ("l1", "l2", "l3")}},
        })
    return cases


def summarize(results):
    probes = [row for row in results if row["probe"] is not None and row["scenario"] != "injection"]
    calls = [row["probe"] for row in results if row["probe"] and "semantic_ms" in row["probe"]]
    delays = [probe["semantic_ms"] for probe in calls]
    guards = [row for row in results if row["scenario"] in {"injection", "withhold", "wrong_person", "repeat_confirm"}]
    summary = metrics(results)
    summary.update({
        "first_ask_passes": sum(row["first_ask_answered"] for row in probes), "first_ask_cases": len(probes),
        "exact_topics": sum(row["exact_topics"] for row in probes),
        "guard_passes": sum(row["task_success"] for row in guards), "guard_cases": len(guards),
        "model_calls_on_first_probe": len(delays), "model_status": dict(Counter(p["source"] for p in calls)),
        "model_p50_ms": _percentile(delays, .5), "model_p95_ms": _percentile(delays, .95),
        "model_max_ms": max(delays, default=None),
    })
    return summary


async def evaluate(args, cases, name, classifier):
    rows = []
    if classifier is not None:
        # Warm the HTTP connection and prompt prefix without using any scored utterance.
        _, status = await classifier("跟本单无关，我只是打错电话了。")
        if status not in {"local_model_classified", "local_model_abstained"}:
            print(f"{name} warm-up: {status}", flush=True)
    for case in cases:
        release = f"{GUIDED.version}+{classifier.version}" if classifier else GUIDED.version
        row = await run_case(case, GUIDED, args.output, name, semantic_topics=classifier, release_id=release)
        trace = read_json(args.output / row["trace"])
        probe = next((turn for turn in trace["turns"] if turn["role"] == "probe"), None)
        followup = (probe["result"].get("result") or {}).get("followup", {}) if probe else None
        row["probe"] = followup
        row["exact_topics"] = bool(followup is not None and set(followup.get("topics", [])) == set(case["expected_topics"]))
        if case["scenario"] == "injection" and followup and not followup.get("source", "").endswith("_recap"):
            row["failure_labels"].append("injection_misrouted")
            row["task_success"] = False
        rows.append(row)
    return rows


async def run(args):
    cases = cases_for(args.split)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "cases.json", {"cases": cases})
    async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
        (await client.get(f"http://127.0.0.1:{args.port}/health")).raise_for_status()
        arms = [("rules", None), ("prompt-a", LocalTopics(client, args.port))]
        if args.tuned:
            arms.extend((name, LocalTopics(client, args.port, prompt=PROMPT_B, constrained=constrained))
                        for name, constrained in (("prompt-b", False), ("prompt-b-grammar", True)))
            arms.append(("prompt-c-grammar", LocalTopics(client, args.port, prompt=PROMPT_C, constrained=True)))
        runs = {name: await evaluate(args, cases, name, model) for name, model in arms}
    summary = {name: summarize(rows) for name, rows in runs.items()}
    write_json(args.output / "results.json", {
        "split": args.split, "case_hash": digest(args.output / "cases.json"), "fixture_hash": digest(FIXTURES),
        "knowledge_hash": digest(KNOWLEDGE), "model_alias": MODEL_ALIAS, "model_budget_ms": MODEL_BUDGET_MS,
        "prompts": {name: {"version": model.version, "text": model.prompt, "grammar": model.grammar}
                    for name, model in arms if model},
        "summary": summary, "runs": runs,
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "validation"), required=True)
    parser.add_argument("--port", type=int, default=18099)
    parser.add_argument("--tuned", action="store_true")
    asyncio.run(run(parser.parse_args()))
