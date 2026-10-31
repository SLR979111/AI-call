from __future__ import annotations

import time
import uuid

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_B32[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid() -> str:
    """ULID-like sortable id: 48-bit time + 80-bit randomness."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = uuid.uuid4().int & ((1 << 80) - 1)
    return _b32(ts, 10) + _b32(rand, 16)


def session_id() -> str:
    return "s_" + ulid()


def trace_id() -> str:
    return "tr_" + uuid.uuid4().hex[:16]


def event_id() -> str:
    return "evt_" + ulid()


def span_id() -> str:
    return "sp_" + uuid.uuid4().hex[:8]


def call_id() -> str:
    return "c_" + uuid.uuid4().hex


def turn_id(n: int) -> str:
    return f"t_{n:04d}"


def reply_id(turn: str, attempt: int = 1) -> str:
    return f"r_{turn[2:]}_{attempt}"
