#!/usr/bin/env python3
"""Pretty-print one session's event timeline.

Usage:
  python scripts/replay.py --session s_...
  python scripts/replay.py --file data/sessions/s_.../events.jsonl
  python scripts/replay.py --session s_... --type TurnLatency
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def summarize(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if event["type"] == "TurnLatency":
        return f"e2e={payload.get('e2e_ms')}ms official={payload.get('e2e_official_proxy_ms')}ms"
    if event["type"] in {"AgentSentence", "Transcript"}:
        return str(payload.get("text", ""))[:60]
    if event["type"] in {"ToolCalled", "ToolReturned", "ToolFailed"}:
        return f"{payload.get('tool')} {payload.get('attempt', '')}"
    if event["type"] in {"ComplianceChecked", "ComplianceBlocked"}:
        return f"{payload.get('decision', 'blocked')} {payload.get('rule_ids', '')}"
    if event["type"] in {"BargeInDetected", "CancelRequested", "TurnAborted"}:
        return str(payload.get("reason", ""))
    keys = ",".join(list(payload)[:4])
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", help="session_id under data/sessions/")
    parser.add_argument("--file", help="explicit events.jsonl path")
    parser.add_argument("--data-dir", default="data", help="data directory (default: data)")
    parser.add_argument("--type", help="only show this event type")
    parser.add_argument("--json", action="store_true", help="print raw JSON lines")
    args = parser.parse_args()

    if not args.session and not args.file:
        parser.error("provide --session or --file")

    path = Path(args.file) if args.file else Path(args.data_dir) / "sessions" / args.session / "events.jsonl"
    if not path.exists():
        parser.error(f"events file not found: {path}")

    events = load(path)
    if args.type:
        events = [e for e in events if e["type"] == args.type]

    if args.json:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        return 0

    print(f"{path}  ({len(events)} events)")
    for event in events:
        turn = event.get("turn_id") or "-"
        latency = event.get("latency_ms")
        lat = f" {latency:7.1f}ms" if isinstance(latency, (int, float)) else " " * 10
        print(f"{event['seq']:>4} {event['ts'][11:23]} {event['type']:<22} {turn:<7}{lat}  {summarize(event)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
