"""Run the independent semantic judge on calibration samples or saved conversations."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.eval.pop_judge import DIMENSIONS, RUBRIC, RUBRIC_VERSION, JudgeConfig, conversation, review


async def score(args):
    config = JudgeConfig.from_env()
    calibration = json.loads((ROOT / "eval/pop/judge_calibration.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        sample_reviews = await asyncio.gather(*(review(client, config, item["turns"]) for item in calibration))
        evaluated, matches, sample_records = 0, 0, []
        for sample, result in zip(calibration, sample_reviews, strict=True):
            observed = result.get("scores", {})
            for dimension, expected in sample["expected"].items():
                actual = observed.get(dimension, {}).get("verdict")
                evaluated += actual is not None
                matches += actual == expected
            sample_records.append({"id": sample["id"], "expected": sample["expected"], "result": result})
        checks = sum(len(item["expected"]) for item in calibration)
        payload = {
            "rubric": RUBRIC_VERSION,
            "rubric_sha256": hashlib.sha256(RUBRIC.encode()).hexdigest(),
            "provider": urlsplit(config.endpoint).hostname,
            "model": config.model,
            "calibration": {"matches": matches, "evaluated": evaluated, "checks": checks,
                            "samples": sample_records},
            "reviews": [],
        }
        if args.run_dir and evaluated:
            results = json.loads((args.run_dir / "results.json").read_text(encoding="utf-8"))["results"]
            selected = [row for row in results if row["split"] in args.splits]
            semaphore = asyncio.Semaphore(5)

            async def review_row(row):
                trace = json.loads((args.run_dir / row["trace"]).read_text(encoding="utf-8"))
                async with semaphore:
                    result = await review(client, config, conversation(trace))
                return {**{key: row[key] for key in ("case_id", "release_id", "split")},
                        "rule_pass": row["task_success"], **result}

            payload["reviews"] = await asyncio.gather(*(review_row(row) for row in selected))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    scored = sum(item["status"] == "scored" for item in payload["reviews"])
    print(f"calibration: {matches}/{checks}; usable: {evaluated}/{checks}; reviewed: {scored}/{len(payload['reviews'])}")
    return 0 if evaluated else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="saved offline experiment; omit to only calibrate")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--splits", nargs="+", choices=("dev", "validation", "holdout"),
                        default=("validation", "holdout"))
    return asyncio.run(score(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
