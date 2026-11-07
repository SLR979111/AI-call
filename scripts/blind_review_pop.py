"""Prepare paired blind samples and validate independent LLM review outputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.eval.pop_judge import RUBRIC, RUBRIC_VERSION, conversation, validate_review


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def stable_key(value):
    return hashlib.sha256(f"{RUBRIC_VERSION}:{value}".encode()).hexdigest()


def prepare(run):
    rows = load(run / "results.json")["results"]
    groups = {}
    for row in sorted(rows, key=lambda item: stable_key(item["case_id"])):
        if row["split"] != "dev":
            groups.setdefault((row["split"], row["route"]), row["case_id"])
    chosen = set(groups.values())
    cases = []
    for row in rows:
        if row["case_id"] in chosen:
            cases.append({
                "turns": conversation(load(run / row["trace"])),
                "origin": {key: row[key] for key in ("case_id", "release_id", "split", "route", "task_success")},
            })
    calibration = load(ROOT / "eval/pop/judge_calibration.json")
    for item in calibration:
        cases.append({"turns": item["turns"], "origin": {"calibration": item["id"], "expected": item["expected"]}})
    cases.sort(key=lambda item: stable_key(json.dumps(item["origin"], ensure_ascii=False, sort_keys=True)))
    private = {}
    samples = []
    for index, item in enumerate(cases, start=1):
        blind_id = f"sample-{index:02d}"
        private[blind_id] = item["origin"]
        samples.append({"id": blind_id, "turns": item["turns"]})
    folder = run / "blind"
    folder.mkdir(exist_ok=False)
    save(folder / "mapping.json", private)
    for index in range(2):
        save(folder / f"input-{index + 1}.json", {"rubric": RUBRIC, "samples": samples[index::2]})
    print(f"prepared {len(cases) - len(calibration)} blinded conversations + {len(calibration)} calibration items")


def collect(run):
    folder = run / "blind"
    mapping = load(folder / "mapping.json")
    reviews, calibration = [], []
    matches = evaluated = checks = 0
    for index in (1, 2):
        inputs = load(folder / f"input-{index}.json")["samples"]
        output = load(folder / f"review-{index}.json")
        if set(output) != {item["id"] for item in inputs}:
            raise ValueError(f"review {index} coverage differs from input")
        for item in inputs:
            blind_id = item["id"]
            origin = mapping[blind_id]
            try:
                result = {"status": "scored", "scores": validate_review(output[blind_id], item["turns"])}
            except ValueError as exc:
                result = {"status": "unavailable", "error": str(exc)}
            if "calibration" not in origin:
                reviews.append({**origin, "blind_id": blind_id, **result})
                continue
            observed = result.get("scores", {})
            for dimension, expected in origin["expected"].items():
                actual = observed.get(dimension, {}).get("verdict")
                checks += 1
                evaluated += actual is not None
                matches += actual == expected
            calibration.append({**origin, "blind_id": blind_id, **result})
    save(run / "judge_blind.json", {
        "provider": "Codex 独立盲评代理", "rubric": RUBRIC_VERSION,
        "rubric_sha256": hashlib.sha256(RUBRIC.encode()).hexdigest(),
        "calibration": {"matches": matches, "evaluated": evaluated, "checks": checks, "samples": calibration},
        "reviews": reviews,
        "sampling": "每个分区及业务分支按固定哈希抽一例，保留新旧版本成对记录",
    })
    print(f"blind judge: {sum(row['status'] == 'scored' for row in reviews)}/{len(reviews)}; calibration {matches}/{checks}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "collect"))
    parser.add_argument("run", type=Path)
    arguments = parser.parse_args()
    (prepare if arguments.action == "prepare" else collect)(arguments.run)
