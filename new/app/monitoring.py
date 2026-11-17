"""Complete monitoring system for latency, cache, and decision tracking."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class LatencyBreakdown:
    """Detailed latency breakdown for a single turn."""
    
    turn_id: str
    started: float
    
    asr_start: float | None = None
    asr_end: float | None = None
    
    llm_start: float | None = None
    llm_first_token: float | None = None
    llm_end: float | None = None
    
    tts_start: float | None = None
    tts_first_chunk: float | None = None
    tts_end: float | None = None
    
    first_audio_sent: float | None = None
    interrupted: bool = False
    
    cache_hits: dict[str, bool] = field(default_factory=dict)  # "llm", "tts", "asr"
    
    @property
    def asr_latency_ms(self) -> float | None:
        if self.asr_start and self.asr_end:
            return (self.asr_end - self.asr_start) * 1000
        return None
    
    @property
    def llm_ttft_ms(self) -> float | None:
        """Time to first token."""
        if self.llm_start and self.llm_first_token:
            return (self.llm_first_token - self.llm_start) * 1000
        return None
    
    @property
    def llm_total_ms(self) -> float | None:
        if self.llm_start and self.llm_end:
            return (self.llm_end - self.llm_start) * 1000
        return None
    
    @property
    def tts_first_chunk_ms(self) -> float | None:
        if self.tts_start and self.tts_first_chunk:
            return (self.tts_first_chunk - self.tts_start) * 1000
        return None
    
    @property
    def tts_total_ms(self) -> float | None:
        if self.tts_start and self.tts_end:
            return (self.tts_end - self.tts_start) * 1000
        return None
    
    @property
    def end_to_end_ms(self) -> float | None:
        """From user input to first audio sent."""
        if self.first_audio_sent:
            return (self.first_audio_sent - self.started) * 1000
        return None


class PerformanceMonitor:
    """Comprehensive monitoring for all performance metrics."""
    
    def __init__(self):
        self.turns: list[LatencyBreakdown] = []
        self.current_turn: LatencyBreakdown | None = None
        
        # Aggregated metrics
        self.task_completions: int = 0
        self.task_failures: int = 0
        self.total_interrupts: int = 0
        self.total_errors: int = 0
        
        # Error tracking
        self.error_counts: dict[str, int] = defaultdict(int)
    
    def start_turn(self, turn_id: str):
        """Start tracking a new turn."""
        self.current_turn = LatencyBreakdown(turn_id=turn_id, started=time.perf_counter())
    
    def mark_asr_start(self):
        if self.current_turn:
            self.current_turn.asr_start = time.perf_counter()
    
    def mark_asr_end(self, cache_hit: bool):
        if self.current_turn:
            self.current_turn.asr_end = time.perf_counter()
            self.current_turn.cache_hits["asr"] = cache_hit
    
    def mark_llm_start(self):
        if self.current_turn:
            self.current_turn.llm_start = time.perf_counter()
    
    def mark_llm_first_token(self, cache_hit: bool):
        if self.current_turn:
            self.current_turn.llm_first_token = time.perf_counter()
            self.current_turn.cache_hits["llm"] = cache_hit
    
    def mark_llm_end(self):
        if self.current_turn:
            self.current_turn.llm_end = time.perf_counter()
    
    def mark_tts_start(self):
        if self.current_turn:
            self.current_turn.tts_start = time.perf_counter()
    
    def mark_tts_first_chunk(self, cache_hit: bool):
        if self.current_turn:
            self.current_turn.tts_first_chunk = time.perf_counter()
            self.current_turn.cache_hits["tts"] = cache_hit
    
    def mark_tts_end(self):
        if self.current_turn:
            self.current_turn.tts_end = time.perf_counter()
    
    def mark_first_audio_sent(self):
        if self.current_turn:
            self.current_turn.first_audio_sent = time.perf_counter()
    
    def mark_interrupt(self):
        if self.current_turn:
            self.current_turn.interrupted = True
            self.total_interrupts += 1
    
    def end_turn(self):
        """Finalize the current turn."""
        if self.current_turn:
            self.turns.append(self.current_turn)
            self.current_turn = None
    
    def record_task_completion(self, success: bool):
        if success:
            self.task_completions += 1
        else:
            self.task_failures += 1
    
    def record_error(self, error_type: str):
        self.error_counts[error_type] += 1
        self.total_errors += 1
    
    def get_latency_percentiles(self, metric: str) -> dict[str, float]:
        """Calculate P50/P95/P99 for a specific metric."""
        samples = []
        for turn in self.turns:
            value = getattr(turn, metric, None)
            if value is not None:
                samples.append(value)
        
        if not samples:
            return {"p50": 0, "p95": 0, "p99": 0, "count": 0}
        
        samples.sort()
        count = len(samples)
        return {
            "p50": samples[int(count * 0.50)],
            "p95": samples[int(count * 0.95)] if count > 1 else samples[0],
            "p99": samples[int(count * 0.99)] if count > 2 else samples[0],
            "count": count,
        }
    
    def report(self) -> dict:
        """Generate comprehensive performance report."""
        return {
            "summary": {
                "total_turns": len(self.turns),
                "task_completions": self.task_completions,
                "task_failures": self.task_failures,
                "total_interrupts": self.total_interrupts,
                "total_errors": self.total_errors,
                "interrupt_rate": f"{self.total_interrupts / len(self.turns) * 100:.1f}%" if self.turns else "0%",
            },
            "latency": {
                "end_to_end": self.get_latency_percentiles("end_to_end_ms"),
                "llm_ttft": self.get_latency_percentiles("llm_ttft_ms"),
                "llm_total": self.get_latency_percentiles("llm_total_ms"),
                "asr": self.get_latency_percentiles("asr_latency_ms"),
                "tts_first_chunk": self.get_latency_percentiles("tts_first_chunk_ms"),
            },
            "cache_impact": {
                "llm_hits": sum(1 for t in self.turns if t.cache_hits.get("llm")),
                "llm_misses": sum(1 for t in self.turns if t.cache_hits.get("llm") is False),
                "tts_hits": sum(1 for t in self.turns if t.cache_hits.get("tts")),
                "tts_misses": sum(1 for t in self.turns if t.cache_hits.get("tts") is False),
            },
            "errors": dict(self.error_counts),
        }
