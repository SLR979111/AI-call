"""Persistent disk cache shared by the LLM and TTS layers.

Both layers are deterministic for a given key (model + messages, or text +
speed + sample rate), so their outputs survive a process restart. Keeping them
on disk turns a repeated conversation into memory-speed work: an LLM turn stops
paying 0.6-1.0 s of cloud TTFT and TTS stops paying ~0.3 s per sentence.

Layout: ``<dir>/<ns>/<aa>/<sha256><.json|.bin>``. Writes are atomic
(``tmp`` + ``os.replace``) so a crash cannot leave a half-written entry that the
next start would load. ``prune()`` keeps the whole tree under ``max_mb`` by
deleting the least recently used entries.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def cache_key(*parts: Any) -> str:
    """Stable sha256 over JSON-serialisable parts."""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DiskCache:
    """Small namespaced key/value store with an LRU size budget."""

    def __init__(self, root: Path | str, *, max_mb: int = 256, enabled: bool = True) -> None:
        self.root = Path(root)
        self.max_bytes = max(1, max_mb) * 1024 * 1024
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, namespace: str = "cache") -> "DiskCache":
        raw = os.getenv("DISK_CACHE", "1").strip().lower()
        enabled = raw not in {"0", "off", "false", "no"}
        root = Path(os.getenv("DISK_CACHE_DIR", "") or f"data/{namespace}")
        return cls(root, max_mb=int(os.getenv("DISK_CACHE_MAX_MB", "256")), enabled=enabled)

    # ---- paths ----
    def _path(self, namespace: str, key: str, suffix: str) -> Path:
        return self.root / namespace / key[:2] / f"{key[2:]}{suffix}"

    # ---- reads ----
    def get_bytes(self, namespace: str, key: str) -> bytes | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key, ".bin")
        try:
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            self.misses += 1
            return None
        if not data:
            self.misses += 1
            return None
        self.hits += 1
        try:
            os.utime(path, None)  # LRU bookkeeping
        except OSError:
            pass
        return data

    def get_json(self, namespace: str, key: str) -> Any:
        raw = self.get_bytes(namespace, key)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    # ---- writes ----
    def put_bytes(self, namespace: str, key: str, data: bytes) -> None:
        if not self.enabled or not data:
            return
        path = self._path(namespace, key, ".bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        self.writes += 1
        if self.writes % 32 == 0:
            self.prune()

    def put_json(self, namespace: str, key: str, value: Any) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return
        self.put_bytes(namespace, key, payload)

    # ---- housekeeping ----
    def stats(self) -> dict[str, int]:
        total = 0
        count = 0
        if self.enabled and self.root.exists():
            for path in self.root.rglob("*.bin"):
                try:
                    total += path.stat().st_size
                    count += 1
                except OSError:
                    continue
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes,
                "entries": count, "bytes": total}

    def prune(self) -> int:
        """Drop least recently used entries until the tree fits the budget."""
        if not self.enabled or not self.root.exists():
            return 0
        files: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.root.rglob("*.bin"):
            try:
                info = path.stat()
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, path))
            total += info.st_size
        if total <= self.max_bytes:
            return 0
        removed = 0
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def clear(self) -> None:
        if self.enabled and self.root.exists():
            for path in self.root.rglob("*.bin"):
                try:
                    path.unlink()
                except OSError:
                    pass


_LLM_CACHE: DiskCache | None = None
_TTS_CACHE: DiskCache | None = None


def llm_cache() -> DiskCache:
    """Process-wide LLM completion cache (loaded from disk on first use)."""
    global _LLM_CACHE
    if _LLM_CACHE is None:
        _LLM_CACHE = DiskCache.from_env("cache")
    return _LLM_CACHE


def tts_cache() -> DiskCache:
    """Process-wide TTS audio cache (loaded from disk on first use)."""
    global _TTS_CACHE
    if _TTS_CACHE is None:
        # Audio is far larger than text, so it gets its own budget.
        raw = os.getenv("DISK_CACHE", "1").strip().lower()
        enabled = raw not in {"0", "off", "false", "no"}
        root = Path(os.getenv("DISK_CACHE_DIR", "") or "data/cache")
        _TTS_CACHE = DiskCache(root / "audio", max_mb=int(os.getenv("DISK_CACHE_AUDIO_MAX_MB", "512")),
                               enabled=enabled)
    return _TTS_CACHE


def reset_caches() -> None:
    """Test hook: forget the process-wide instances."""
    global _LLM_CACHE, _TTS_CACHE
    _LLM_CACHE = None
    _TTS_CACHE = None


__all__ = ["DiskCache", "cache_key", "llm_cache", "tts_cache", "reset_caches"]
