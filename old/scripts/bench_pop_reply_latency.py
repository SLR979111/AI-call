"""Replay one frozen case set on loopback with identical Agent and judge settings."""
from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import Counter
from pathlib import Path

import httpx

from run_pop_auto_rsi import calibrate, evaluate
from app.agent.pop_grounded_answer import GroundedAnswer, LIVE_BUDGET_MS
from app.agent.pop_repair import FAST
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_runner import KNOWLEDGE, digest, read_json, write_json


def timing(rows):
    followups = [row["followup"] or {} for row in rows]
    provider = [row.get("provider_timings", {}) for row in followups]
    measures = {"wall_ms": [r["generation_ms"] for r in followups if "generation_ms" in r]}
    measures["first_reply_text_ms"] = [r["first_reply_text_ms"] for r in rows if "first_reply_text_ms" in r]
    for name in ("prompt_ms", "prompt_n", "predicted_ms", "predicted_n"):
        measures[name] = [r[name] for r in provider if name in r]
    return {"status": dict(Counter(r.get("generation_status", "fast_path") for r in followups)),
            **{name: {"p50": statistics.median(values), "max": max(values), "n": len(values)}
               for name, values in measures.items() if values}}


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = read_json(args.cases)
    write_json(args.output / "cases.json", frozen)
    cases = frozen["cases"]
    docs = {doc["id"].rsplit("-", 1)[-1]: doc["scene"] for doc in read_json(KNOWLEDGE) if "l3" in doc["scene"]}
    advice = args.advice.read_text(encoding="utf-8").strip()
    results = {}
    async with httpx.AsyncClient(trust_env=False, timeout=10) as local, httpx.AsyncClient(timeout=25) as judge:
        config = JudgeConfig.from_env()
        await calibrate(judge, config, args.output)
        if args.fast_only:
            for name, model in (("fast-rules", None), ("fast-live", GroundedAnswer.local(local, args.port, advice=advice))):
                score, rows = await evaluate(judge, config, args.output, name, cases, docs, model, policy=FAST)
                results[name] = {**score, **timing(rows)}
        else:
            for name, budget, compact in (("full-measure", 5000, False), ("compact-measure", 5000, True),
                                          ("compact-live", LIVE_BUDGET_MS, True), ("full-live", LIVE_BUDGET_MS, False)):
                if args.cloud:
                    model = GroundedAnswer(judge, config.endpoint, config.model, key=config.api_key,
                                           advice=advice, budget_ms=budget, compact=compact)
                else:
                    model = GroundedAnswer.local(local, args.port, advice=advice, budget_ms=budget, compact=compact,
                                                cache_prompt=not args.no_cache)
                score, rows = await evaluate(judge, config, args.output, name, cases, docs, model)
                results[name] = {**score, "budget_ms": budget, **timing(rows)}
    write_json(args.output / "results.json", {"case_hash": digest(args.cases), "port": None if args.cloud else args.port,
               "cloud": config.model if args.cloud else None,
               "no_cache": args.no_cache, "results": results})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--advice", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18099)
    path = parser.add_mutually_exclusive_group()
    path.add_argument("--fast-only", action="store_true")
    path.add_argument("--cloud", action="store_true", help="Measure the configured cloud model with identical limits")
    parser.add_argument("--no-cache", action="store_true", help="Request a full prefill for every comparison")
    asyncio.run(run(parser.parse_args()))
