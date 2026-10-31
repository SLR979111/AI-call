from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ids import event_id, span_id as new_span_id

CST = timezone(timedelta(hours=8))


class EventBus:
    """Append-only event log + SQLite index.

    One bus per process. `seq` is monotonic per session and assigned here;
    modules must never invent their own sequence numbers.
    """

    def __init__(self, data_dir: Path, release_id: str, clock_id: str, mode: str = "voice") -> None:
        self.data_dir = Path(data_dir)
        self.release_id = release_id
        self.clock_id = clock_id
        self.mode = mode
        self._seq: dict[str, int] = {}
        self._modes: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._db_path = self.data_dir / "index" / "events.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                turn_id TEXT,
                trace_id TEXT,
                type TEXT NOT NULL,
                ts TEXT NOT NULL,
                module TEXT NOT NULL,
                ok INTEGER NOT NULL,
                latency_ms REAL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        self._conn.commit()

    def session_dir(self, session_id: str) -> Path:
        path = self.data_dir / "sessions" / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register_session(self, session_id: str, mode: str) -> None:
        self._modes[session_id] = mode

    def events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    async def emit(
        self,
        *,
        session_id: str,
        type: str,
        module: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        latency_ms: float | None = None,
        ok: bool = True,
        error: dict[str, Any] | None = None,
        mode: str | None = None,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            seq = self._seq.get(session_id, 0) + 1
            self._seq[session_id] = seq
            now = datetime.now(CST)
            envelope: dict[str, Any] = {
                "schema_version": "1.0",
                "event_id": event_id(),
                "seq": seq,
                "type": type,
                "ts": now.isoformat(timespec="milliseconds"),
                "ts_mono_ms": time.perf_counter() * 1000.0,
                "clock_id": self.clock_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "trace_id": trace_id,
                "span_id": span_id or new_span_id(),
                "parent_span_id": parent_span_id,
                "module": module,
                "release_id": release_id or self.release_id,
                "mode": mode or self._modes.get(session_id, self.mode),
                "payload": payload or {},
                "latency_ms": latency_ms,
                "ok": ok,
                "error": error,
            }
            line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            with self.events_path(session_id).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._conn.execute(
                "INSERT INTO events(event_id, session_id, seq, turn_id, trace_id, type, ts, module, ok, latency_ms, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope["event_id"],
                    session_id,
                    seq,
                    turn_id,
                    trace_id,
                    type,
                    envelope["ts"],
                    module,
                    1 if ok else 0,
                    latency_ms,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return envelope

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path(session_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
        return events

    def read_events_since(self, session_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        return [e for e in self.read_events(session_id) if int(e.get("seq", 0)) > since_seq]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
