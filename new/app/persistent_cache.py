"""Small SQLite cache shared by conversations and process restarts.

The cache stores opaque bytes so the same store can hold LLM text, ASR text,
TTS PCM and serialized opening forecasts. Each namespace has its own LRU
budget; callers pass the limits they already use in memory.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheRecord:
    key: str
    value: bytes
    size: int
    created_at: float
    accessed_at: float
    access_count: int


class PersistentCache:
    """Thread-safe SQLite-backed cache with per-namespace LRU eviction."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=3.0,
        )
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=3000")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value BLOB NOT NULL,
                    size INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS cache_entries_lru "
                "ON cache_entries(namespace, accessed_at)"
            )
            self._connection.commit()

    def get(self, namespace: str, key: str) -> CacheRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value, size, created_at, accessed_at, access_count "
                "FROM cache_entries WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            self._connection.execute(
                "UPDATE cache_entries SET accessed_at = ?, access_count = access_count + 1 "
                "WHERE namespace = ? AND key = ?",
                (now, namespace, key),
            )
            self._connection.commit()
            return CacheRecord(key, bytes(row[0]), row[1], row[2], now, row[4] + 1)

    def touch(self, namespace: str, key: str) -> None:
        """Refresh recency after an in-memory hit."""
        with self._lock:
            self._connection.execute(
                "UPDATE cache_entries SET accessed_at = ?, access_count = access_count + 1 "
                "WHERE namespace = ? AND key = ?",
                (time.time(), namespace, key),
            )
            self._connection.commit()

    def put(
        self,
        namespace: str,
        key: str,
        value: bytes,
        *,
        max_entries: int,
        max_bytes: int,
        access_count: int = 1,
    ) -> None:
        now = time.time()
        with self._lock:
            self._connection.execute(
                "INSERT INTO cache_entries "
                "(namespace, key, value, size, created_at, accessed_at, access_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value = excluded.value, size = excluded.size, "
                "accessed_at = excluded.accessed_at, access_count = excluded.access_count",
                (namespace, key, value, len(value), now, now, access_count),
            )
            self._connection.commit()
            self._trim(namespace, max_entries=max_entries, max_bytes=max_bytes)

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            self._connection.commit()

    def load(
        self,
        namespace: str,
        *,
        max_entries: int,
        max_bytes: int | None = None,
    ) -> list[CacheRecord]:
        """Load the most recently used records, newest first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT key, value, size, created_at, accessed_at, access_count "
                "FROM cache_entries WHERE namespace = ? "
                "ORDER BY accessed_at DESC LIMIT ?",
                (namespace, max_entries),
            ).fetchall()
            records = [
                CacheRecord(row[0], bytes(row[1]), row[2], row[3], row[4], row[5])
                for row in rows
            ]
            if max_bytes is not None:
                kept: list[CacheRecord] = []
                total = 0
                for record in records:
                    if kept and total + record.size > max_bytes:
                        break
                    kept.append(record)
                    total += record.size
                records = kept
            return records

    def stats(self, namespace: str) -> dict:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) "
                "FROM cache_entries WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return {
            "entries": int(row[0]),
            "size_mb": round(int(row[1]) / (1024 * 1024), 2),
        }

    def clear(self, namespace: str | None = None) -> None:
        with self._lock:
            if namespace is None:
                self._connection.execute("DELETE FROM cache_entries")
            else:
                self._connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ?", (namespace,)
                )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _trim(self, namespace: str, *, max_entries: int, max_bytes: int) -> None:
        rows = self._connection.execute(
            "SELECT key, size FROM cache_entries WHERE namespace = ? "
            "ORDER BY accessed_at ASC",
            (namespace,),
        ).fetchall()
        total = sum(int(row[1]) for row in rows)
        while rows and (len(rows) > max_entries or total > max_bytes):
            key, size = rows.pop(0)
            self._connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            total -= int(size)
        self._connection.commit()
