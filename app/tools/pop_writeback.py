"""Persistent local adapter for a confirmed POP contact result."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

TOOL = "pop_contact.record"


class PopContactStore:
    def __init__(self, path: Path, bus, *, fault: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self.fault = fault
        self.conn = sqlite3.connect(path)
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS contacts (session_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )

    async def record(self, request: dict, *, request_id: str, doc_id: str, route: str) -> dict | None:
        session_id = request["session_id"]
        event = {"session_id": session_id, "turn_id": request["turn_id"],
                 "trace_id": request["trace_id"], "module": "tool"}
        args = {"request_id": request_id, "knowledge_id": doc_id,
                "handling_mode": route, "contact_outcome": "merchant_understood"}
        await self.bus.emit(**event, type="ToolCalled", payload={"tool": TOOL, "args_redacted": args})
        if self.fault or not re.fullmatch(r"Q[0-9]{6,}", request_id) or route != "电话" or not doc_id:
            await self.bus.emit(**event, type="ToolFailed", ok=False, payload={"tool": TOOL},
                                error={"code": "POP_WRITE_FAILED", "message": "Mock write unavailable",
                                       "retryable": self.fault})
            return None
        record = {"record_id": f"contact-{session_id}", "session_id": session_id,
                  "confirmed_turn_id": request["turn_id"], "backend": "local_mock", **args}
        encoded = json.dumps(record, ensure_ascii=False)
        with self.conn:
            cursor = self.conn.execute("INSERT OR IGNORE INTO contacts VALUES (?, ?)", (session_id, encoded))
            saved = self.conn.execute("SELECT record FROM contacts WHERE session_id=?", (session_id,)).fetchone()
        result = json.loads(saved[0])
        if any(result[key] != value for key, value in args.items()):
            await self.bus.emit(**event, type="ToolFailed", ok=False, payload={"tool": TOOL},
                                error={"code": "POP_WRITE_CONFLICT", "message": "Contact already recorded",
                                       "retryable": False})
            return None
        await self.bus.emit(**event, type="ToolReturned",
                            payload={"tool": TOOL, "data_summary": result, "cached": cursor.rowcount == 0})
        return result

    def close(self) -> None:
        self.conn.close()
