"""把两个接入端（根链路 app/ 与 G2 new/）的对话日志整理成统一落盘格式。

根链路：data/sessions/<sid>/events.jsonl（EventBus 逐事件落盘）
G2：new/runtime/sessions/<sid>.jsonl

输出：
  reports/conversations/<source>/<sid>.json   单会话归一化记录（轮次、文本、延迟）
  reports/conversations/index.json            全部会话索引
  reports/conversations/index.md              人读表格

用法：
  python scripts/export_conversations.py [--out reports/conversations]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


def export_root(root: Path) -> list[dict[str, Any]]:
    sessions = []
    for path in sorted(root.glob("data/sessions/*/events.jsonl")):
        events = _read_jsonl(path)
        if not events:
            continue
        sid = events[0].get("session_id") or path.parent.name
        turns: dict[str, dict[str, Any]] = {}
        started = events[0].get("ts")
        mode = (events[0].get("payload") or {}).get("mode")
        for event in events:
            turn_id = event.get("turn_id")
            if not turn_id:
                continue
            turn = turns.setdefault(turn_id, {"turn_id": turn_id, "user": "", "sentences": []})
            kind, payload = event.get("type"), event.get("payload") or {}
            if kind == "Transcript" and payload.get("final") and payload.get("text"):
                turn["user"] = payload["text"]
            elif kind == "AgentSentence" and payload.get("text"):
                turn["sentences"].append(payload["text"])
            elif kind == "TurnLatency":
                turn["latency"] = {
                    "e2e_ms": payload.get("e2e_ms"),
                    "asr_final_ms": _delta(payload, "t2_asr_final_ms", "t1_endpoint_ms"),
                    "llm_first_token_ms": _delta(payload, "t4_first_token_ms", "t3_agent_start_ms"),
                    "first_sentence_ms": _delta(payload, "t5_first_sentence_ms", "t4_first_token_ms"),
                    "tts_first_chunk_ms": _delta(payload, "t7_tts_first_chunk_ms", "t6_compliance_ms"),
                    "downlink_ms": _delta(payload, "t8_first_downlink_ms", "t7_tts_first_chunk_ms"),
                    "playback_ms": _delta(payload, "t9_first_playback_ms", "t8_first_downlink_ms"),
                }
        record = {
            "source": "root", "session_id": sid, "started_at": started, "mode": mode,
            "turns": [t for t in turns.values() if t["user"] or t["sentences"]],
            "log_path": str(path),
        }
        sessions.append(record)
    return sessions


def export_g2(root: Path) -> list[dict[str, Any]]:
    sessions = []
    for path in sorted(root.glob("new/runtime/sessions/*.jsonl")):
        events = _read_jsonl(path)
        if not events:
            continue
        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        started = None
        for event in events:
            kind = event.get("event")
            at = event.get("at")
            started = started or (datetime.fromtimestamp(at).isoformat(timespec="seconds") if at else None)
            if kind == "user":
                current = {"turn": len(turns) + 1, "user": event.get("text", ""), "sentences": []}
                turns.append(current)
            elif kind == "delivered" and current is not None:
                current["sentences"].append(event.get("text", ""))
            elif kind == "reply_finished" and current is not None:
                current["latency"] = {
                    "voice_to_playback_ms": event.get("voice_to_playback_ms"),
                    "first_text_ms": event.get("first_text_ms"),
                    "first_packet_ms": event.get("first_packet_ms"),
                    "reused_cache": event.get("reused"),
                }
        sessions.append({
            "source": "g2", "session_id": path.stem, "started_at": started,
            "mode": "voice", "turns": turns, "log_path": str(path),
        })
    return sessions


def _delta(payload: dict[str, Any], end: str, start: str) -> float | None:
    try:
        return round(float(payload[end]) - float(payload[start]), 2)
    except (KeyError, TypeError, ValueError):
        return None


def write(sessions: Iterable[dict[str, Any]], out: Path) -> None:
    sessions = sorted(sessions, key=lambda s: s.get("started_at") or "")
    out.mkdir(parents=True, exist_ok=True)
    for session in sessions:
        target = out / session["source"] / f"{session['session_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.json").write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 对话日志索引", "",
             f"生成时间：{datetime.now().isoformat(timespec='seconds')}　会话数：{len(sessions)}", "",
             "| 时间 | 来源 | 会话 | 轮次 | 有延迟数据 | 首轮 user |",
             "| --- | --- | --- | --- | --- | --- |"]
    for session in sessions:
        turns = session.get("turns") or []
        measured = sum(1 for turn in turns if turn.get("latency"))
        first = next((t.get("user") for t in turns if t.get("user")), "")
        first = (first or "").replace("|", "/")[:24]
        lines.append(f"| {session.get('started_at') or '-'} | {session['source']} | `{session['session_id']}` "
                     f"| {len(turns)} | {measured} | {first} |")
    (out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="仓库根目录")
    parser.add_argument("--out", default="reports/conversations")
    args = parser.parse_args()
    root = Path(args.repo)
    sessions = export_root(root) + export_g2(root)
    write(sessions, Path(args.out))
    print(f"exported {len(sessions)} sessions -> {args.out}")


if __name__ == "__main__":
    main()
