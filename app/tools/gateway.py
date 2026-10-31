from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..events import EventBus


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ToolSpec:
    name: str
    version: str
    required: tuple[str, ...]
    patterns: dict[str, str]
    idempotent: bool = True
    timeout_ms: int = 2000


class MockBusiness:
    """Deterministic mock of 名单/订单/工单. No real numbers, no PII."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {
            "A12345678": {
                "order_id": "A12345678",
                "status": "delivering",
                "promised_at": "2026-11-01T10:00:00+08:00",
                "address_masked": "北京市朝阳区***",
            }
        }
        self.tickets: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self.fault: str | None = None

    async def _maybe_fault(self) -> None:
        if self.fault == "timeout":
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()
        if self.fault == "500":
            await asyncio.sleep(0)
            raise ToolError("TOOL_5XX", "mock upstream 500", retryable=True)

    async def order_query(self, order_id: str) -> dict[str, Any]:
        await self._maybe_fault()
        order = self.orders.get(order_id)
        if not order:
            raise ToolError("TOOL_NOT_FOUND", f"order {order_id} not found")
        return dict(order)

    async def order_reschedule(self, order_id: str, new_time: str) -> dict[str, Any]:
        await self._maybe_fault()
        order = self.orders.get(order_id)
        if not order:
            raise ToolError("TOOL_NOT_FOUND", f"order {order_id} not found")
        order["promised_at"] = new_time
        order["status"] = "delivering"
        self._seq += 1
        ticket_id = f"T{self._seq:06d}"
        self.tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "order_id": order_id,
            "type": "reschedule",
            "new_time": new_time,
        }
        return {"ticket_id": ticket_id, "order_id": order_id, "new_time": new_time, "status": "created"}

    async def ticket_create(self, summary: str, order_id: str | None = None) -> dict[str, Any]:
        await self._maybe_fault()
        self._seq += 1
        ticket_id = f"T{self._seq:06d}"
        self.tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "type": "manual",
            "order_id": order_id,
            "summary": summary,
        }
        return {"ticket_id": ticket_id, "status": "created"}

    async def lead_query(self, lead_id: str) -> dict[str, Any]:
        await self._maybe_fault()
        return {"lead_id": lead_id, "stage": "new", "owner": "mock"}


class ToolGateway:
    """Single entry point for business tools: validate, idempotency, timeout, retry, audit."""

    SPECS = {
        "order.query": ToolSpec(
            "order.query", "1.2.0", ("order_id",), {"order_id": r"^[A-Z0-9]{8,20}$"}
        ),
        "order.reschedule": ToolSpec(
            "order.reschedule", "1.1.0", ("order_id", "new_time"),
            {"order_id": r"^[A-Z0-9]{8,20}$", "new_time": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"},
        ),
        "ticket.create": ToolSpec("ticket.create", "1.0.0", ("summary",), {}),
        "lead.query": ToolSpec("lead.query", "1.0.0", ("lead_id",), {"lead_id": r"^L[0-9]{4,12}$"}),
    }

    def __init__(self, data_dir: Path, mock: MockBusiness | None = None, bus: EventBus | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.mock = mock or MockBusiness()
        self.bus = bus
        self._db_path = self.data_dir / "index" / "tools.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    # ---- helpers ----
    @staticmethod
    def _canonical(args: dict[str, Any]) -> str:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _idem_key(self, session_id: str, turn_id: str, tool: str, args: dict[str, Any],
                  provided: str | None) -> str:
        if provided:
            return provided
        raw = f"{session_id}|{turn_id}|{tool}|{self._canonical(args)}"
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _validate(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        for field in spec.required:
            if field not in args:
                raise ToolError("TOOL_INVALID_ARGS", f"missing field: {field}")
        for field, pattern in spec.patterns.items():
            if field in args and not re.fullmatch(pattern, str(args[field])):
                raise ToolError("TOOL_INVALID_ARGS", f"invalid field: {field}")

    def _redact(self, args: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in args.items():
            lowered = key.lower()
            if any(token in lowered for token in ("phone", "id_card", "name", "address")):
                out[key] = "***"
            else:
                out[key] = value
        return out

    # ---- idempotency ----
    def _get_cached(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT response_json, expires_at FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        response_json, expires_at = row
        if float(expires_at) < time.time():
            self._conn.execute("DELETE FROM idempotency WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(response_json)

    def _store(self, key: str, response: dict[str, Any]) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency(key, response_json, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (key, json.dumps(response, ensure_ascii=False), now, now + 24 * 3600),
        )
        self._conn.commit()

    async def _emit(self, **kwargs: Any) -> None:
        if self.bus is not None:
            await self.bus.emit(**kwargs)

    # ---- public ----
    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        deadline_ms: int = 2000,
        session_id: str = "s_dev",
        turn_id: str = "t_0000",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        spec = self.SPECS.get(tool)
        if spec is None:
            return {
                "ok": False,
                "error": {"code": "TOOL_NOT_FOUND", "message": f"unknown tool {tool}", "retryable": False},
                "meta": {"attempt": 0, "cached": False, "latency_ms": 0.0, "tool_version": "unknown"},
            }

        key = self._idem_key(session_id, turn_id, tool, args, idempotency_key)
        await self._emit(
            session_id=session_id, type="ToolCalled", module="tool",
            turn_id=turn_id, trace_id=trace_id,
            payload={"tool": tool, "args_redacted": self._redact(args), "call_id": key[:32],
                     "idempotency_key": key, "deadline_ms": deadline_ms},
        )

        cached = self._get_cached(key)
        if cached is not None:
            cached = json.loads(json.dumps(cached))
            cached.setdefault("meta", {})["cached"] = True
            await self._emit(
                session_id=session_id, type="ToolCachedReplay", module="tool",
                turn_id=turn_id, trace_id=trace_id,
                payload={"tool": tool, "key": key, "original_latency_ms": cached.get("meta", {}).get("latency_ms")},
            )
            return cached

        handler = {
            "order.query": self.mock.order_query,
            "order.reschedule": self.mock.order_reschedule,
            "ticket.create": self.mock.ticket_create,
            "lead.query": self.mock.lead_query,
        }[tool]

        attempts = 0
        last_error: ToolError | None = None
        for attempt in (1, 2):
            attempts = attempt
            start = time.perf_counter()
            try:
                self._validate(spec, args)
                data = await asyncio.wait_for(handler(**args), timeout=deadline_ms / 1000.0)
                latency = (time.perf_counter() - start) * 1000.0
                response = {
                    "ok": True,
                    "data": data,
                    "meta": {"attempt": attempt, "cached": False, "latency_ms": round(latency, 2),
                             "tool_version": f"{spec.name}@{spec.version}"},
                }
                self._store(key, response)
                await self._emit(
                    session_id=session_id, type="ToolReturned", module="tool",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2),
                    payload={"tool": tool, "data_summary": data, "attempt": attempt, "cached": False},
                )
                return response
            except ToolError as exc:
                last_error = exc
                latency = (time.perf_counter() - start) * 1000.0
                if exc.retryable and attempt == 1:
                    await self._emit(
                        session_id=session_id, type="ToolRetried", module="tool",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"tool": tool, "attempt": attempt, "reason": exc.code, "backoff_ms": 200},
                    )
                    await asyncio.sleep(0.2)
                    continue
                await self._emit(
                    session_id=session_id, type="ToolFailed", module="tool",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2), ok=False,
                    error={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                    payload={"tool": tool, "attempt": attempt},
                )
                return {
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                    "meta": {"attempt": attempt, "cached": False, "latency_ms": round(latency, 2),
                             "tool_version": f"{spec.name}@{spec.version}"},
                }
            except asyncio.TimeoutError:
                last_error = ToolError("TOOL_TIMEOUT", "deadline exceeded", retryable=True)
                latency = (time.perf_counter() - start) * 1000.0
                if attempt == 1:
                    await self._emit(
                        session_id=session_id, type="ToolRetried", module="tool",
                        turn_id=turn_id, trace_id=trace_id,
                        payload={"tool": tool, "attempt": attempt, "reason": "TOOL_TIMEOUT", "backoff_ms": 200},
                    )
                    await asyncio.sleep(0.2)
                    continue
                await self._emit(
                    session_id=session_id, type="ToolFailed", module="tool",
                    turn_id=turn_id, trace_id=trace_id, latency_ms=round(latency, 2), ok=False,
                    error={"code": "TOOL_TIMEOUT", "message": "deadline exceeded", "retryable": True},
                    payload={"tool": tool, "attempt": attempt},
                )
                return {
                    "ok": False,
                    "error": {"code": "TOOL_TIMEOUT", "message": "deadline exceeded", "retryable": True},
                    "meta": {"attempt": attempt, "cached": False, "latency_ms": round(latency, 2),
                             "tool_version": f"{spec.name}@{spec.version}"},
                }

        assert last_error is not None
        return {
            "ok": False,
            "error": {"code": last_error.code, "message": last_error.message, "retryable": last_error.retryable},
            "meta": {"attempt": attempts, "cached": False, "latency_ms": 0.0,
                     "tool_version": f"{spec.name}@{spec.version}"},
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
