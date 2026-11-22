"""Multi-layer cache system for reducing API latency and improving hit rates."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheStats:
    """Cache performance metrics."""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    
    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
    
    @property
    def total_requests(self) -> int:
        return self.hits + self.misses


class LLMCache:
    """LRU cache for LLM chat responses with conversation context awareness.
    
    Cache key: hash(messages + model + temperature)
    Value: full response text
    Strategy: LRU eviction when size or count exceeded
    """
    
    def __init__(self, max_entries: int = 200, max_bytes: int = 5_000_000):
        self.cache: OrderedDict[str, str] = OrderedDict()
        self.cache_bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.stats = CacheStats()
    
    def _make_key(self, messages: list[dict], model: str, temperature: float) -> str:
        """Generate cache key from conversation context."""
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "temperature": temperature,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()
    
    def get(self, messages: list[dict], model: str, temperature: float) -> str | None:
        """Retrieve cached response if available."""
        key = self._make_key(messages, model, temperature)
        response = self.cache.get(key)
        if response is not None:
            self.cache.move_to_end(key)
            self.stats.hits += 1
            return response
        self.stats.misses += 1
        return None
    
    def put(self, messages: list[dict], model: str, temperature: float, response: str):
        """Store response in cache with LRU eviction."""
        if len(response) > 50_000:  # Skip extremely long responses
            return
        
        key = self._make_key(messages, model, temperature)
        previous = self.cache.pop(key, "")
        self.cache_bytes += len(response.encode()) - len(previous.encode())
        self.cache[key] = response
        
        # Evict old entries
        while len(self.cache) > self.max_entries or self.cache_bytes > self.max_bytes:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed.encode())
            self.stats.evictions += 1
    
    def prefetch(self, common_exchanges: list[tuple[list[dict], str]]):
        """Warm cache with common Q&A pairs."""
        for messages, expected_response in common_exchanges:
            # In production, you'd call the LLM here and cache the result
            # For now, we just store the expected response
            self.put(messages, "deepseek-flash", 0.2, expected_response)


class ASRCache:
    """Cache for ASR transcription results.
    
    Cache key: hash(audio PCM bytes)
    Value: transcribed text
    """
    
    def __init__(self, max_entries: int = 100, max_bytes: int = 10_000_000):
        self.cache: OrderedDict[str, str] = OrderedDict()
        self.audio_hashes: OrderedDict[str, int] = OrderedDict()  # key -> audio length
        self.cache_bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.stats = CacheStats()
    
    def _make_key(self, pcm: bytes) -> str:
        """Generate cache key from audio data."""
        # Use first and last 4KB + length for faster hashing
        if len(pcm) > 8192:
            sample = pcm[:4096] + pcm[-4096:] + len(pcm).to_bytes(4, 'big')
        else:
            sample = pcm
        return hashlib.sha256(sample).hexdigest()
    
    def get(self, pcm: bytes) -> str | None:
        """Retrieve cached transcription if available."""
        key = self._make_key(pcm)
        text = self.cache.get(key)
        if text is not None:
            self.cache.move_to_end(key)
            self.audio_hashes.move_to_end(key)
            self.stats.hits += 1
            return text
        self.stats.misses += 1
        return None
    
    def put(self, pcm: bytes, text: str):
        """Store transcription in cache with LRU eviction."""
        key = self._make_key(pcm)
        previous_text = self.cache.pop(key, "")
        previous_audio_len = self.audio_hashes.pop(key, 0)
        
        self.cache_bytes += (len(pcm) - previous_audio_len) + \
                           (len(text.encode()) - len(previous_text.encode()))
        self.cache[key] = text
        self.audio_hashes[key] = len(pcm)
        
        # Evict old entries
        while len(self.cache) > self.max_entries or self.cache_bytes > self.max_bytes:
            old_key, _ = self.cache.popitem(last=False)
            old_audio_len = self.audio_hashes.pop(old_key)
            self.cache_bytes -= old_audio_len + len(self.cache.get(old_key, "").encode())
            self.stats.evictions += 1


class CacheManager:
    """Unified cache manager for all caching layers."""
    
    def __init__(self):
        self.llm = LLMCache(max_entries=200, max_bytes=5_000_000)
        self.asr = ASRCache(max_entries=100, max_bytes=10_000_000)
    
    def report(self) -> dict[str, Any]:
        """Generate cache performance report."""
        return {
            "llm": {
                "hits": self.llm.stats.hits,
                "misses": self.llm.stats.misses,
                "hit_rate": f"{self.llm.stats.hit_rate * 100:.1f}%",
                "entries": len(self.llm.cache),
                "size_mb": self.llm.cache_bytes / 1_000_000,
            },
            "asr": {
                "hits": self.asr.stats.hits,
                "misses": self.asr.stats.misses,
                "hit_rate": f"{self.asr.stats.hit_rate * 100:.1f}%",
                "entries": len(self.asr.cache),
                "size_mb": self.asr.cache_bytes / 1_000_000,
            },
        }
