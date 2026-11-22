"""Small SQLite cache shared by conversations and process restarts.

The cache stores opaque bytes so the same store can hold LLM text, ASR text,
TTS PCM and serialized opening forecasts. Each namespace has its own LRU
budget; callers pass the limits they already use in memory.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
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

    def __init__(self, path: str | Path, *, best_effort: bool = False):
        self.best_effort = best_effort
        self._failed = False
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # A live lookup runs on the audio event loop. Under write contention
        # it can use RAM immediately and retry disk on the next operation.
        timeout = 0.05 if best_effort else 3.0
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=timeout,
        )
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
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
        except sqlite3.Error:
            self._connection.close()
            raise

    @classmethod
    def for_live_calls(cls, path):
        if not path:
            return None
        try:
            return cls(path, best_effort=True)
        except (OSError, sqlite3.Error) as exc:
            logging.getLogger(__name__).warning("Using in-memory caches: %s", exc)
            return None

    @contextmanager
    def _operation(self):
        with self._lock:
            try:
                with self._connection:
                    yield
            except sqlite3.Error as exc:
                if not self.best_effort:
                    raise
                if not self._failed:
                    logging.getLogger(__name__).warning("Persistent cache unavailable: %s", exc)
                self._failed = True
            else:
                self._failed = False

    def get(self, namespace: str, key: str) -> CacheRecord | None:
        with self._operation():
            updated = self._connection.execute(
                "UPDATE cache_entries SET accessed_at = ?, access_count = access_count + 1 "
                "WHERE namespace = ? AND key = ?",
                (time.time(), namespace, key),
            )
            if updated.rowcount:
                row = self._connection.execute(
                    "SELECT value, size, created_at, accessed_at, access_count "
                    "FROM cache_entries WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
                return CacheRecord(key, *row)
        return None

    def touch(self, namespace: str, key: str) -> None:
        """Refresh recency after an in-memory hit."""
        with self._operation():
            self._connection.execute(
                "UPDATE cache_entries SET accessed_at = ?, access_count = access_count + 1 "
                "WHERE namespace = ? AND key = ?",
                (time.time(), namespace, key),
            )

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
        if max_entries < 1 or len(value) > max_bytes:
            self.delete(namespace, key)
            return
        now = time.time()
        with self._operation():
            self._connection.execute(
                "INSERT INTO cache_entries "
                "(namespace, key, value, size, created_at, accessed_at, access_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value = excluded.value, size = excluded.size, "
                "accessed_at = excluded.accessed_at, access_count = excluded.access_count",
                (namespace, key, value, len(value), now, now, access_count),
            )
            self._trim(namespace, max_entries=max_entries, max_bytes=max_bytes)

    def delete(self, namespace: str, key: str) -> None:
        with self._operation():
            self._connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                (namespace, key),
            )

    def load(
        self,
        namespace: str,
        *,
        max_entries: int,
        max_bytes: int | None = None,
    ) -> list[CacheRecord]:
        """Load the most recently used records, newest first."""
        with self._operation():
            rows = self._connection.execute(
                "SELECT key, value, size, created_at, accessed_at, access_count "
                "FROM cache_entries WHERE namespace = ? AND (? IS NULL OR size <= ?) "
                "ORDER BY accessed_at DESC, rowid DESC",
                (namespace, max_bytes, max_bytes),
            )
            records = []
            total = 0
            for row in rows:
                if len(records) >= max_entries:
                    break
                if max_bytes is None or total + row[2] <= max_bytes:
                    records.append(CacheRecord(*row))
                    total += row[2]
            return records
        return []

    def stats(self, namespace: str) -> dict:
        with self._operation():
            entries, size = self._usage(namespace)
            return {"entries": entries, "size_mb": round(size / (1024 * 1024), 2)}
        return {"entries": 0, "size_mb": 0, "available": False}

    def _usage(self, namespace):
        return self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache_entries WHERE namespace = ?",
            (namespace,),
        ).fetchone()

    def clear(self, namespace: str | None = None) -> None:
        with self._operation():
            if namespace is None:
                self._connection.execute("DELETE FROM cache_entries")
            else:
                self._connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ?", (namespace,)
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _trim(self, namespace: str, *, max_entries: int, max_bytes: int) -> None:
        count, total = self._usage(namespace)
        if count <= max_entries and total <= max_bytes:
            return
        victims = []
        rows = self._connection.execute(
            "SELECT key, size FROM cache_entries WHERE namespace = ? "
            "ORDER BY accessed_at, rowid", (namespace,),
        )
        for key, size in rows:
            victims.append((namespace, key))
            count -= 1
            total -= size
            if count <= max_entries and total <= max_bytes:
                break
        self._connection.executemany(
            "DELETE FROM cache_entries WHERE namespace = ? AND key = ?", victims,
        )
