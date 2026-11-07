"""Locate rejected identity replies without exporting user transcripts."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.pop_competition import identity_answer


def audit(path: Path) -> dict:
    counts = Counter()
    evidence = []
    sessions = set()
    for log in sorted((path / "sessions").glob("*/events.jsonl")):
        identity_pending = False
        candidates = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            payload = event["payload"]
            event_type, turn = event["type"], event.get("turn_id")
            if event_type == "BusinessStageReached":
                if payload["stage"] == "OPEN":
                    identity_pending = True
                elif payload["stage"] == "IDENTITY_CONFIRM":
                    identity_pending = False
            elif event_type == "Transcript" and identity_pending and payload.get("final"):
                candidates[turn] = (identity_answer(payload["text"]), event["seq"])
            elif event_type == "AgentCompleted" and turn in candidates:
                if payload["outcome"] == "safe_exit":
                    identity_pending = False
                if payload["outcome"] != "pending_identity":
                    continue
                verdict, transcript_seq = candidates[turn]
                counts[verdict] += 1
                if verdict != "unclear":
                    sessions.add(event["session_id"])
                    evidence.append({"session_id": event["session_id"], "turn_id": turn,
                                     "transcript_seq": transcript_seq, "agent_seq": event["seq"],
                                     "before": "pending_identity", "candidate_interpretation": verdict})
    return {
        "run": path.name, "affected_sessions": len(sessions),
        "old_pending_replies_by_candidate_interpretation": dict(counts),
        "evidence": evidence,
        "scope": "reclassifies saved transcripts within identity phase; no raw audio or user words exported",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(args.run), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    print(result, end="")
