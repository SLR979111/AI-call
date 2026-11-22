"""Enhanced cache system with intelligent prefetching and LFU eviction."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .persistent_cache import PersistentCache


@dataclass
class CacheEntry:
    """Cache entry with frequency and recency tracking."""
    value: Any
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


class EnhancedLLMCache:
    """LLM response cache with LFU eviction and optional disk backing."""

    namespace = "llm"

    def __init__(self, max_entries: int = 500, max_size_mb: float = 10.0,
                 store: PersistentCache | None = None):
        self.max_entries = max_entries
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self.cache: dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.prefetch_hits = 0
        self.store = store
        self._load()

    def _load(self):
        if self.store is None:
            return
        for record in self.store.load(
            self.namespace, max_entries=self.max_entries,
            max_bytes=self.max_size_bytes,
        ):
            try:
                value = record.value.decode("utf-8")
            except UnicodeDecodeError:
                continue
            self.cache[record.key] = CacheEntry(
                value=value,
                size=record.size,
                created_at=record.created_at,
                last_accessed=record.accessed_at,
                access_count=record.access_count,
            )
    
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
        """Get a cached response, promoting disk entries on demand."""
        key = self._make_key(messages, model, temperature, max_tokens)
        entry = self.cache.get(key)

        if entry is None and self.store is not None:
            record = self.store.get(self.namespace, key)
            if record is not None:
                try:
                    value = record.value.decode("utf-8")
                except UnicodeDecodeError:
                    value = None
                if value is not None:
                    while (len(self.cache) >= self.max_entries or
                           self._total_size() + record.size > self.max_size_bytes):
                        if not self.cache:
                            break
                        self._evict_lfu()
                    entry = CacheEntry(
                        value=value,
                        size=record.size,
                        created_at=record.created_at,
                        last_accessed=record.accessed_at,
                        access_count=record.access_count,
                    )
                    self.cache[key] = entry

        if entry is None:
            self.misses += 1
            return None

        # Update access stats
        entry.last_accessed = time.time()
        entry.access_count += 1
        self.hits += 1
        if self.store is not None:
            self.store.touch(self.namespace, key)

        return entry.value
    
    def put(self, messages: list[dict], model: str, temperature: float, response: str,
            *, max_tokens: int | None = None):
        """Cache a response with LFU tracking and write-through persistence."""
        key = self._make_key(messages, model, temperature, max_tokens)
        encoded = response.encode('utf-8')
        size = len(encoded)
        
        # Evict if necessary
        while (len(self.cache) >= self.max_entries or 
               self._total_size() + size > self.max_size_bytes):
            if not self.cache:
                break
            self._evict_lfu()
        
        # Store entry
        now = time.time()
        self.cache[key] = CacheEntry(
            value=response,
            size=size,
            created_at=now,
            last_accessed=now,
            access_count=1,
        )
        if self.store is not None:
            self.store.put(
                self.namespace, key, encoded,
                max_entries=self.max_entries, max_bytes=self.max_size_bytes,
            )
    
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
    
    def _evict_lfu(self):
        """Evict entry with lowest LFU score."""
        if not self.cache:
            return
        
        # Find entry with lowest score
        victim_key = min(self.cache.keys(), 
                        key=lambda k: self.cache[k].lfu_score)
        del self.cache[victim_key]
        if self.store is not None:
            self.store.delete(self.namespace, victim_key)
        self.evictions += 1
    
    def _total_size(self) -> int:
        """Calculate total cache size in bytes."""
        return sum(entry.size for entry in self.cache.values())
    
    def stats(self) -> dict:
        """Return cache statistics."""
        total = self.hits + self.misses
        hit_rate = self.hits / total if total > 0 else 0.0
        report = {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "prefetch_hits": self.prefetch_hits,
            "hit_rate": round(hit_rate, 3),
            "entries": len(self.cache),
            "size_mb": round(self._total_size() / (1024 * 1024), 2),
            "persistent": self.store is not None,
        }
        if self.store is not None:
            report["disk"] = self.store.stats(self.namespace)
        return report
    
    def clear_stats(self):
        """Reset statistics."""
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.prefetch_hits = 0


class EnhancedASRCache:
    """ASR cache with full-audio fingerprinting and LFU eviction."""

    namespace = "asr"

    def __init__(self, max_entries: int = 200, max_size_mb: float = 20.0,
                 store: PersistentCache | None = None):
        self.max_entries = max_entries
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self.cache: dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.store = store
        self._load()

    def _load(self):
        if self.store is None:
            return
        for record in self.store.load(
            self.namespace, max_entries=self.max_entries,
            max_bytes=self.max_size_bytes,
        ):
            try:
                value = record.value.decode("utf-8")
            except UnicodeDecodeError:
                continue
            self.cache[record.key] = CacheEntry(
                value=value,
                size=record.size,
                created_at=record.created_at,
                last_accessed=record.accessed_at,
                access_count=record.access_count,
            )
    
    def _make_key(self, pcm: bytes) -> str:
        """Hash the complete PCM; a prefix/suffix fingerprint can collide."""
        return hashlib.sha256(pcm).hexdigest()[:32]
    
    def get(self, pcm: bytes) -> str | None:
        """Get a cached transcription, promoting disk entries on demand."""
        key = self._make_key(pcm)
        entry = self.cache.get(key)

        if entry is None and self.store is not None:
            record = self.store.get(self.namespace, key)
            if record is not None:
                try:
                    value = record.value.decode("utf-8")
                except UnicodeDecodeError:
                    value = None
                if value is not None:
                    while (len(self.cache) >= self.max_entries or
                           self._total_size() + record.size > self.max_size_bytes):
                        if not self.cache:
                            break
                        self._evict_lfu()
                    entry = CacheEntry(
                        value=value,
                        size=record.size,
                        created_at=record.created_at,
                        last_accessed=record.accessed_at,
                        access_count=record.access_count,
                    )
                    self.cache[key] = entry

        if entry is None:
            self.misses += 1
            return None
        
        # Update access stats
        entry.last_accessed = time.time()
        entry.access_count += 1
        self.hits += 1
        if self.store is not None:
            self.store.touch(self.namespace, key)
        
        return entry.value
    
    def put(self, pcm: bytes, text: str):
        """Cache a transcription with LFU tracking and write-through persistence."""
        key = self._make_key(pcm)
        size = len(pcm)
        encoded = text.encode("utf-8")
        
        # Evict if necessary
        while (len(self.cache) >= self.max_entries or 
               self._total_size() + size > self.max_size_bytes):
            if not self.cache:
                break
            self._evict_lfu()
        
        # Store entry
        now = time.time()
        self.cache[key] = CacheEntry(
            value=text,
            size=size,
            created_at=now,
            last_accessed=now,
            access_count=1,
        )
        if self.store is not None:
            self.store.put(
                self.namespace, key, encoded,
                max_entries=self.max_entries, max_bytes=self.max_size_bytes,
            )
    
    def _evict_lfu(self):
        """Evict entry with lowest LFU score."""
        if not self.cache:
            return
        
        victim_key = min(self.cache.keys(), 
                        key=lambda k: self.cache[k].lfu_score)
        del self.cache[victim_key]
        if self.store is not None:
            self.store.delete(self.namespace, victim_key)
        self.evictions += 1
    
    def _total_size(self) -> int:
        """Calculate total cache size in bytes."""
        return sum(entry.size for entry in self.cache.values())
    
    def stats(self) -> dict:
        """Return cache statistics."""
        total = self.hits + self.misses
        hit_rate = self.hits / total if total > 0 else 0.0
        report = {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(hit_rate, 3),
            "entries": len(self.cache),
            "size_mb": round(self._total_size() / (1024 * 1024), 2),
            "persistent": self.store is not None,
        }
        if self.store is not None:
            report["disk"] = self.store.stats(self.namespace)
        return report


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
