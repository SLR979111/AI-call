"""Check browser evidence against the event trail and durable mock business record."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from pathlib import Path

from eval_pop import ROOT
from app.agent.pop_competition import AUDIT_RESULT, OPENING, RECORDING, STAGES, identity_answer, result_confirmed
from app.eval.pop_runner import read_json, write_json
from app.tools.pop_writeback import TOOL


def audit(run: Path) -> dict:
    browser = read_json(run / "browser-evidence.json")
    rows = browser["measurements"]
    if browser["failure"] or browser["errors"] or len(rows) < 3:
        raise ValueError(f"Browser replay incomplete: {browser['failure']} / {browser['errors']}")
    session_id = rows[0]["session_id"]
    if any(row["session_id"] != session_id for row in rows) or len({row["turn"] for row in rows}) != len(rows):
        raise ValueError("Expected unique turns in the same call")
    raw = run / "sessions" / session_id / "events.jsonl"
    events = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
    transport = browser.get("transport", "ws_pcm")
    if transport == "rtc_pcm":
        switched = any(event["type"] == "TransportSwitched" and event["payload"].get("to") == transport
                       for event in events)
        upload = browser["uploads"][0]
        if not switched or upload["rtc_pcm"] < 1 or upload["ws_pcm"] != 0:
            raise ValueError("Expected WebRTC PCM upload without WebSocket audio fallback")
    stages = [event for event in events if event["type"] == "BusinessStageReached"]
    if tuple(event["payload"]["stage"] for event in stages) != STAGES:
        raise ValueError("Six business stages are missing or out of order")
    confirmation, writeback = stages[-2:]
    calls = [event for event in events if event["type"] == "ToolCalled" and event["payload"].get("tool") == TOOL]
    returns = [event for event in events if event["type"] == "ToolReturned" and event["payload"].get("tool") == TOOL]
    if len(calls) != 1 or len(returns) != 1:
        raise ValueError("Expected exactly one contact write")
    call, receipt = calls[0], returns[0]
    if not confirmation["seq"] < call["seq"] < receipt["seq"] < writeback["seq"]:
        raise ValueError("Write and receipt must follow merchant confirmation")
    if len({confirmation["turn_id"], call["turn_id"], receipt["turn_id"], writeback["turn_id"]}) != 1:
        raise ValueError("Write evidence must be correlated with the confirmed turn")
    confirmed_text = next(row["asr"] for row in rows if row["turn"] == confirmation["turn_id"])
    if not result_confirmed(confirmed_text):
        raise ValueError("Confirmation transcript did not express understanding")
    db_path = (run / "index/pop_contacts.db").resolve()
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as connection:
        stored = connection.execute("SELECT record FROM contacts WHERE session_id=?", (session_id,)).fetchone()
    if not stored or json.loads(stored[0]) != receipt["payload"]["data_summary"]:
        raise ValueError("Returned receipt does not match persisted contact record")
    spoken = [event for event in events if event["type"] == "AgentSentence"]
    if any(("审核问题是" in event["payload"]["text"] or AUDIT_RESULT == event["payload"]["text"])
           for event in spoken if event["seq"] < stages[1]["seq"]):
        raise ValueError("The agent explained the case before identity confirmation")
    if not any("沟通结果已记录" in event["payload"]["text"] for event in spoken if event["seq"] > receipt["seq"]):
        raise ValueError("The agent did not confirm the saved result")
    if any("沟通结果已记录" in event["payload"]["text"] for event in spoken if event["seq"] < receipt["seq"]):
        raise ValueError("The agent claimed a successful write before the receipt")
    times = [row["last_voiced_to_browser_playback_proxy_ms"] for row in rows
             if isinstance(row["last_voiced_to_browser_playback_proxy_ms"], (float, int))
             and row["last_voiced_to_browser_playback_proxy_ms"] >= 0]
    abort = next((msg for msg in browser["server"] if msg["type"] == "cancel" and msg["reason"] == "BARGE_IN"), None)
    manual_mute = any(row["type"] == "local_muted" and row.get("reason") == "user_barge_button"
                      for batch in browser["client"] for row in batch)
    if not abort or not manual_mute:
        raise ValueError("Manual browser interruption evidence missing")
    severe = [event for event in events if event["type"] == "ComplianceBlocked"
              and event["payload"].get("severity") in {"high", "critical", "fatal"}]
    if severe:
        raise ValueError("Severe compliance block in the completed call")
    user_turns = {row["turn"] for row in rows}
    decisions = [event["latency_ms"] for event in events if event["type"] == "AgentFirstToken"
                 and event["turn_id"] in user_turns]
    opening = next((row for row in browser.get("openings", []) if row["session_id"] == session_id), None)
    if opening:
        first_turn = next(event for event in events if event["type"] == "TurnStarted")
        opening_events = [event for event in events if event["turn_id"] == opening["turn"]]
        opening_speech = [event["payload"]["text"] for event in opening_events if event["type"] == "AgentSentence"]
        if (first_turn["payload"]["trigger"] != "outbound_opening"
                or first_turn["turn_id"] != stages[0]["turn_id"] or first_turn["turn_id"] != opening["turn"]
                or any(event["type"] in {"Transcript", "TurnLatency"} for event in opening_events)
                or opening_speech != [OPENING, RECORDING] or any(opening["uploads_before_opening"].values())):
            raise ValueError("Expected an outbound greeting with recording notice before user input")
        if identity_answer(rows[0]["asr"]) != "confirmed" or rows[0]["turn"] != stages[1]["turn_id"]:
            raise ValueError("Identity stage must follow the first customer's actual reply")
        for row in browser["identity_checks"]:
            evidence = run / "sessions" / row["session_id"] / "events.jsonl"
            checked = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
            allowed = ["OPEN", "IDENTITY_CONFIRM"] if row["outcome"] == "pending_need" else ["OPEN"]
            if ([e["payload"]["stage"] for e in checked if e["type"] == "BusinessStageReached"] != allowed
                    or any(e["type"] == "ToolCalled" for e in checked)):
                raise ValueError("Identity-only call must not disclose the case or write a contact")
    no_match = browser.get("no_match_retry")
    if no_match:
        evidence = run / "sessions" / no_match["session_id"] / "events.jsonl"
        checked = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
        retry = [e for e in checked if e["type"] == "Transcript"]
        misses = [e for e in checked if e["type"] == "AsrNoMatch"]
        if (len(misses) != 1 or len(retry) != 1 or misses[0]["seq"] >= retry[0]["seq"]
                or retry[0]["turn_id"] != no_match["retry_turn"]):
            raise ValueError("An empty transcription must stay separate from the next recognized reply")
    return {
        "transport": transport,
        "session_id": session_id,
        "completed_voice_turns": len(rows),
        "stages": list(STAGES),
        "writeback": json.loads(stored[0]),
        "tool_receipts": len(returns),
        "severe_compliance_blocks": len(severe),
        "review_notice_spoken": any("以复审为准" in e["payload"]["text"] for e in spoken),
        "event_file": raw.relative_to(run).as_posix(),
        "rule_scope": "local mock; no official sandbox or hidden dataset",
        "browser_timing_proxy": {"n": len(times), "missing": len(rows) - len(times),
                                 "p50_ms": round(statistics.median(times), 2) if times else None,
                                 "p95_ms": sorted(times)[math.ceil(.95 * len(times)) - 1] if times else None},
        "timing_scope": "Browser VAD last voiced to Web Audio first-frame notification; synthetic WAV, no physical microphone or acoustic measurement",
        "agent_first_sentence_decision_ms": decisions,
        "manual_barge_in": True,
        **({"outbound_opening": opening, "identity_checks": browser["identity_checks"]} if opening else {}),
        **({"no_match_retry": no_match} if no_match else {}),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, nargs="?", default=ROOT / "data/pop-contest-primed")
    args = parser.parse_args()
    result = audit(args.run)
    write_json(args.run / "contest-audit.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
