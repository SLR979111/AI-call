"""Verify WebRTC turns, match the persisted receipts, and decide local adoption."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.eval.pop_closure import score
from app.eval.pop_runner import digest, read_json, write_json


def audit(folder, plan):
    browser = read_json(folder / "browser-evidence.json")
    runtime = read_json(folder / "contest-runtime.json")
    calls = browser.get("closure_checks", [])
    if browser.get("failure") or browser.get("errors") or browser["transport"] != "rtc_pcm":
        raise ValueError(f"Invalid WebRTC browser evidence in {folder}")
    if [call["id"] for call in calls] != [case["id"] for case in plan["calls"]]:
        raise ValueError("Browser did not run the frozen call list")
    wire_results = {}
    active_session = None
    for event in browser["server"]:
        if event["type"] == "session_ready":
            active_session = event["session_id"]
        elif event["type"] == "turn_result":
            key = (active_session, event["turn_id"])
            if active_session is None or key in wire_results:
                raise ValueError("Ambiguous browser result")
            wire_results[key] = event["result"]
    results = []
    audio_hashes = {name: digest(folder / "input_audio" / name) for name in plan["samples"]}
    times = []
    with sqlite3.connect((folder / "index/pop_contacts.db").resolve().as_uri() + "?mode=ro", uri=True) as db:
        for spec, call in zip(plan["calls"], calls, strict=True):
            session = call["session_id"]
            path = folder / "sessions" / session / "events.jsonl"
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            stage = next(e for e in events if e["type"] == "BusinessStageReached"
                         and e["payload"].get("stage") == "OPEN")
            switched = any(e["type"] == "TransportSwitched" and e["payload"].get("to") == "rtc_pcm" for e in events)
            if not switched or call["uploads"]["rtc_pcm"] == 0 or call["uploads"]["ws_pcm"] != 0:
                raise ValueError("WebRTC-only audio transport was not demonstrated")
            opening = next(row for row in browser["openings"] if row["session_id"] == session)
            if opening["turn"] != stage["turn_id"]:
                raise ValueError("Opening trace mismatch")
            spoken = {}
            for event in events:
                if event["type"] == "AgentSentence":
                    spoken.setdefault(event["turn_id"], []).append(event["payload"]["text"])
            turns = [{"turn_id": opening["turn"], "user_text": "", "outcome": "pending_identity",
                      "sentences": spoken.get(opening["turn"], []), "error": None}]
            for measured, (name, *_) in zip(call["rows"], spec["turns"], strict=True):
                if measured["input_file"] != name or measured["session_id"] != session:
                    raise ValueError("Voice case changed between plan and browser")
                value = measured["last_voiced_to_browser_playback_proxy_ms"]
                if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("Missing voice-to-playback measurement")
                times.append(value)
                turn_id = measured["turn"]
                result = wire_results[session, turn_id]
                if result.get("outcome") != measured["outcome"]:
                    raise ValueError("Browser result differs from its timing record")
                turns.append({"turn_id": turn_id, "user_text": measured["asr"],
                              "sentences": spoken.get(turn_id, []), "outcome": measured["outcome"],
                              "result": result.get("result") or {}, "error": None})
            records = [json.loads(record) for (record,) in db.execute(
                "SELECT record FROM contacts WHERE session_id=?", (session,))]
            expected = {"turns": [[plan["samples"][name], *rest] for name, *rest in spec["turns"]],
                        "followup_checks": spec.get("followup_checks", {})}
            doc = spec["doc"]
            graded = score(expected, {"doc_id": doc, "request_id": "Q984" + doc.rsplit("-", 1)[-1]},
                           turns, events, records)
            results.append({"id": spec["id"], "doc": doc, "session": session, "turns": turns,
                            "events": str(path), "records": records, **graded})
    return {"calls": results, "audio_sha256": audio_hashes, "turns": len(times),
            "passed": sum(call["passed"] for call in results), "records": sum(call["record_count"] for call in results),
            "severe_cases": sum(call["severe_cases"] for call in results),
            "first_sound_p50_ms": round(statistics.median(times), 2),
            "first_sound_p95_ms": round(sorted(times)[math.ceil(.95 * len(times)) - 1], 2),
            "first_sound_within_2s": sum(value <= 2000 for value in times),
            "lane": f"real WebRTC DataChannel / {runtime['asr']} ASR and {runtime['tts']} TTS"
                    f" / synthetic customer WAV"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="Directory of the locked text RSI run")
    parser.add_argument("--plan", type=Path, default=ROOT / "eval/pop/closure_voice.v2.json")
    args = parser.parse_args()
    plan = read_json(args.plan)
    before, after = [audit(folder, plan) for folder in (args.before, args.after)]
    if before["audio_sha256"] != after["audio_sha256"]:
        raise ValueError("Before and after must use identical recordings")
    same_transcripts = all(
        [t["user_text"] for t in left["turns"]] == [t["user_text"] for t in right["turns"]]
        for left, right in zip(before["calls"], after["calls"], strict=True))
    summary_path = args.evidence / "summary.json"
    summary = read_json(summary_path)
    manifest = read_json(args.evidence / "manifest.json")
    if manifest["agent"] != digest(ROOT / "app/agent/pop_competition.py") or (
            manifest["confirmation_rules"] != digest(ROOT / "app/agent/pop_confirmation.py")):
        raise ValueError("Agent or closure rules changed after the text comparison")
    runtimes = [read_json(folder / "contest-runtime.json") for folder in (args.before, args.after)]
    for runtime, profile in zip(runtimes, (manifest["baseline"], summary["release"]), strict=True):
        if (runtime["dialogue_policy"] != profile["version"] or not runtime["rtc_pcm"]
                or runtime["confirmation_rules"] != manifest["confirmation_rules"]
                or runtime["scene_policy"] != manifest["scene_policy"] or runtime["writeback_fault"]):
            raise ValueError("WebRTC release or conditions differ from the locked comparison")
    if any(runtimes[0][key] != runtimes[1][key] for key in ("asr", "tts", "tts_speed", "language_model_enabled")):
        raise ValueError("WebRTC arms changed the speech providers or model setting")
    adopted = (summary["text_gate_passed"] and same_transcripts and before["passed"] >= 1
               and after["passed"] > before["passed"] and after["passed"] == len(plan["calls"])
               and after["severe_cases"] == 0 and after["first_sound_p50_ms"] <= 2000)
    results = {"plan_sha256": digest(args.plan), "before": before, "after": after, "runtime": runtimes,
               "same_transcripts": same_transcripts,
               "decision": "verified_for_local_replay" if adopted else "retain_baseline"}
    write_json(args.evidence / "voice-audit.json", results)
    summary["voice_gate"] = results["decision"]
    write_json(summary_path, summary)
    if adopted:
        write_json(args.evidence / "verified-release.json", read_json(args.evidence / "candidate.json"))
    print(json.dumps({arm: {k: results[arm][k] for k in (
        "passed", "records", "turns", "first_sound_p50_ms", "first_sound_p95_ms", "severe_cases")}
        for arm in ("before", "after")}, ensure_ascii=False))
    print(results["decision"])


if __name__ == "__main__":
    main()
