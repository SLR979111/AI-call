"""Check approved POP phone wording against the previously locked task strategy."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from run_pop_reactive_rsi import compare, evaluate, load_suite
from app.agent.pop_phone_style import SOURCES, load_phone_style
from app.agent.pop_turn_plan import PopTurnPlan
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_runner import read_json, write_json


async def run(args):
    style = load_phone_style(args.style)
    suite = load_suite()
    if suite != read_json(args.control / "cases.json"):
        raise ValueError("The regression corpus differs from the control")
    spec = read_json(args.control / "manifest.json")
    config = JudgeConfig.from_env()
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"style": style.version, "wording": {key: {
        "before": line, "after": style.replacements[line]} for key, line in SOURCES.items()}, "splits": {}}
    async with httpx.AsyncClient(timeout=1.8, trust_env=False) as client:
        resolver = PopTurnPlan(client, config, budget_ms=1800, retry_writes=True)
        if resolver.version != spec["intent"] or spec["arm"] != "plan-retry":
            raise ValueError("The existing control must be the locked plan-retry strategy")
        for split in ("validation", "test"):
            current = await evaluate(suite[split], args.output / split, resolver, phone_style=style)
            write_json(args.output / split / "score.json", current)
            result = compare(read_json(args.control / split / "score.json"), current)
            summary["splits"][split] = result
            write_json(args.output / "summary.json", summary)
            for group, values in result.items():
                old, new = values["before"], values["after"]
                print(split, group, f"{old['passed']}/{old['cases']} -> {new['passed']}/{new['cases']}",
                      "first_complete", old["complete_first_answers"], new["complete_first_answers"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--style", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
