"""Enhanced cache system with intelligent prefetching and LFU eviction."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .persistent_cache import PersistentCache


@dataclass
class CacheEntry:
    """Cache entry with frequency and recency tracking."""
    value: str
    size: int
    created_at: float
    last_accessed: float
    access_count: int
    
    @property
    def lfu_score(self) -> float:
        """Calculate LFU score (higher = keep longer)."""
        # Combine frequency and recency
        age_hours = (time.time() - self.created_at) / 3600
        decay_factor = 0.9 ** age_hours  # Decay older entries
        return self.access_count * decay_factor


class _TextCache:
    """Frequency-weighted RAM cache backed by an independently bounded SQLite LRU."""

    namespace: str

    def __init__(self, max_entries: int, max_size_mb: float, store: PersistentCache | None):
        self.max_entries = max_entries
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self.store = store
        self.cache: dict[str, CacheEntry] = {}
        self.hits = self.misses = self.evictions = 0
        if store is not None:
            for record in store.load(self.namespace, max_entries=max_entries,
                                     max_bytes=self.max_size_bytes):
                self._restore(record)

    def _restore(self, record) -> CacheEntry | None:
        try:
            value = record.value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        entry = CacheEntry(value, record.size, record.created_at,
                           record.accessed_at, record.access_count)
        self._retain(record.key, entry)
        return entry

    def _retain(self, key, entry):
        self.cache.pop(key, None)
        if self.max_entries < 1 or entry.size > self.max_size_bytes:
            return
        size = sum(item.size for item in self.cache.values()) + entry.size
        while len(self.cache) >= self.max_entries or size > self.max_size_bytes:
            victim = min(self.cache, key=lambda k: self.cache[k].lfu_score)
            size -= self.cache.pop(victim).size
            self.evictions += 1
        self.cache[key] = entry

    def _read(self, key):
        entry = self.cache.get(key)
        if entry is not None:
            entry.last_accessed = time.time()
            entry.access_count += 1
            if self.store is not None:
                self.store.touch(self.namespace, key)
        elif self.store is not None:
            record = self.store.get(self.namespace, key)
            if record is not None:
                entry = self._restore(record)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def _write(self, key, value):
        encoded = value.encode("utf-8")
        now = time.time()
        self._retain(key, CacheEntry(value, len(encoded), now, now, 1))
        if self.store is not None:
            self.store.put(self.namespace, key, encoded, max_entries=self.max_entries,
                           max_bytes=self.max_size_bytes)

    def stats(self) -> dict:
        total = self.hits + self.misses
        size = sum(item.size for item in self.cache.values())
        report = {"hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                  "hit_rate": round(self.hits / total, 3) if total else 0.0,
                  "entries": len(self.cache), "size_mb": round(size / (1024 * 1024), 2),
                  "persistent": self.store is not None}
        if self.store is not None:
            report["disk"] = self.store.stats(self.namespace)
        return report


class EnhancedLLMCache(_TextCache):
    namespace = "llm"

    def __init__(self, max_entries: int = 500, max_size_mb: float = 10.0,
                 store: PersistentCache | None = None):
        super().__init__(max_entries, max_size_mb, store)
        self.prefetch_hits = 0

    def _make_key(self, messages: list[dict], model: str, temperature: float,
                  max_tokens: int | None = None) -> str:
        """Create a key over the full conversation, not just the last turn."""
        payload = {
            "messages": messages,
            "model": model,
            "temp": round(temperature, 2),
            "max_tokens": max_tokens,
        }
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def get(self, messages: list[dict], model: str, temperature: float,
            *, max_tokens: int | None = None) -> str | None:
        return self._read(self._make_key(messages, model, temperature, max_tokens))

    def put(self, messages: list[dict], model: str, temperature: float, response: str,
            *, max_tokens: int | None = None):
        self._write(self._make_key(messages, model, temperature, max_tokens), response)

    def prefetch(self, predictions: list[dict]):
        """Mark predicted responses for priority retention."""
        for pred in predictions:
            messages = pred.get("messages", [])
            model = pred.get("model", "")
            temp = pred.get("temperature", 0.2)
            key = self._make_key(messages, model, temp, pred.get("max_tokens"))
            
            if key in self.cache:
                # Boost access count for predicted entries
                self.cache[key].access_count += 2
                self.prefetch_hits += 1
                if self.store is not None:
                    self.store.touch(self.namespace, key)

    def stats(self):
        return {**super().stats(), "prefetch_hits": self.prefetch_hits}

    def clear_stats(self):
        self.hits = self.misses = self.evictions = self.prefetch_hits = 0


class EnhancedASRCache(_TextCache):
    namespace = "asr"

    def __init__(self, max_entries: int = 200, max_size_mb: float = 20.0,
                 store: PersistentCache | None = None):
        super().__init__(max_entries, max_size_mb, store)

    def _make_key(self, pcm: bytes) -> str:
        """Hash the complete PCM; a prefix/suffix fingerprint can collide."""
        return hashlib.sha256(pcm).hexdigest()[:32]

    def get(self, pcm: bytes) -> str | None:
        return self._read(self._make_key(pcm))

    def put(self, pcm: bytes, text: str):
        self._write(self._make_key(pcm), text)


class EnhancedCacheManager:
    """Unified cache manager with LFU eviction and prefetching."""
    
    def __init__(self, persistent: PersistentCache | None = None):
        self.persistent = persistent
        self.llm = EnhancedLLMCache(max_entries=500, max_size_mb=10.0, store=persistent)
        self.asr = EnhancedASRCache(max_entries=200, max_size_mb=20.0, store=persistent)
    
    def report(self) -> dict:
        """Generate comprehensive cache report."""
        llm_stats = self.llm.stats()
        asr_stats = self.asr.stats()
        
        # Calculate miss punishment
        llm_total = llm_stats["hits"] + llm_stats["misses"]
        asr_total = asr_stats["hits"] + asr_stats["misses"]
        
        # Estimate latency impact (miss adds ~2s for LLM, ~300ms for ASR)
        llm_miss_penalty_ms = llm_stats["misses"] * 2000
        asr_miss_penalty_ms = asr_stats["misses"] * 300
        
        return {
            "llm": {
                **llm_stats,
                "miss_penalty_ms": llm_miss_penalty_ms,
                "avg_miss_penalty_ms": round(llm_miss_penalty_ms / llm_total, 1) if llm_total > 0 else 0,
            },
            "asr": {
                **asr_stats,
                "miss_penalty_ms": asr_miss_penalty_ms,
                "avg_miss_penalty_ms": round(asr_miss_penalty_ms / asr_total, 1) if asr_total > 0 else 0,
            },
            "total_miss_penalty_ms": llm_miss_penalty_ms + asr_miss_penalty_ms,
        }
