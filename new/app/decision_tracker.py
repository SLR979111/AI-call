"""Decision tracking and analysis for reuse vs say choices."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class DecisionStats:
    """Track reuse vs say decision outcomes."""
    
    reuse_attempts: int = 0
    reuse_correct: int = 0  # User confirmed or continued smoothly
    reuse_incorrect: int = 0  # User corrected or complained
    say_count: int = 0
    
    prediction_hits: int = 0  # Predictions that were reused
    prediction_misses: int = 0  # Predictions that were ignored
    
    reuse_latency_samples: list[float] = field(default_factory=list)
    say_latency_samples: list[float] = field(default_factory=list)
    
    @property
    def reuse_accuracy(self) -> float:
        total = self.reuse_correct + self.reuse_incorrect
        return self.reuse_correct / total if total > 0 else 0.0
    
    @property
    def prediction_hit_rate(self) -> float:
        total = self.prediction_hits + self.prediction_misses
        return self.prediction_hits / total if total > 0 else 0.0
    
    @property
    def avg_reuse_latency(self) -> float:
        return (sum(self.reuse_latency_samples) / len(self.reuse_latency_samples)
                if self.reuse_latency_samples else 0)
    
    @property
    def avg_say_latency(self) -> float:
        return (sum(self.say_latency_samples) / len(self.say_latency_samples)
                if self.say_latency_samples else 0)


@dataclass
class CacheMissImpact:
    """Track cache miss punishment metrics."""
    
    miss_latency_samples: list[float] = field(default_factory=list)
    hit_latency_samples: list[float] = field(default_factory=list)
    
    miss_interrupts: int = 0  # User interrupted during slow response
    hit_interrupts: int = 0
    
    @property
    def miss_penalty_ms(self) -> float:
        """Average latency increase from cache miss."""
        if not self.miss_latency_samples or not self.hit_latency_samples:
            return 0
        return (sum(self.miss_latency_samples) / len(self.miss_latency_samples) -
                sum(self.hit_latency_samples) / len(self.hit_latency_samples))
    
    @property
    def miss_interrupt_rate(self) -> float:
        total_miss = len(self.miss_latency_samples)
        return self.miss_interrupts / total_miss if total_miss > 0 else 0.0
    
    @property
    def hit_interrupt_rate(self) -> float:
        total_hit = len(self.hit_latency_samples)
        return self.hit_interrupts / total_hit if total_hit > 0 else 0.0


class DecisionTracker:
    """Centralized tracking for all decision points."""
    
    def __init__(self):
        self.decisions = DecisionStats()
        self.llm_cache_impact = CacheMissImpact()
        self.tts_cache_impact = CacheMissImpact()
        
        # Per-candidate tracking
        self.candidate_usage: dict[str, int] = defaultdict(int)
        self.candidate_success: dict[str, int] = defaultdict(int)
    
    def record_reuse(self, candidate_id: str, latency_ms: float):
        """Record a reuse decision."""
        self.decisions.reuse_attempts += 1
        self.decisions.reuse_latency_samples.append(latency_ms)
        self.candidate_usage[candidate_id] += 1
    
    def record_reuse_outcome(self, candidate_id: str, correct: bool):
        """Record whether reuse was correct (user confirmed/continued)."""
        if correct:
            self.decisions.reuse_correct += 1
            self.candidate_success[candidate_id] += 1
        else:
            self.decisions.reuse_incorrect += 1
    
    def record_say(self, latency_ms: float):
        """Record a say decision (not reusing)."""
        self.decisions.say_count += 1
        self.decisions.say_latency_samples.append(latency_ms)
    
    def record_prediction_hit(self, candidate_id: str):
        """Record a prediction that was reused."""
        self.decisions.prediction_hits += 1
    
    def record_prediction_miss(self):
        """Record predictions that were ignored."""
        self.decisions.prediction_misses += 1
    
    def record_cache_event(self, cache_type: Literal["llm", "tts"],
                          hit: bool, latency_ms: float, interrupted: bool = False):
        """Record cache hit/miss and its impact."""
        impact = self.llm_cache_impact if cache_type == "llm" else self.tts_cache_impact
        
        if hit:
            impact.hit_latency_samples.append(latency_ms)
            if interrupted:
                impact.hit_interrupts += 1
        else:
            impact.miss_latency_samples.append(latency_ms)
            if interrupted:
                impact.miss_interrupts += 1
    
    def get_candidate_stats(self, candidate_id: str) -> dict:
        """Get stats for a specific candidate."""
        used = self.candidate_usage[candidate_id]
        success = self.candidate_success[candidate_id]
        return {
            "used": used,
            "success": success,
            "accuracy": success / used if used > 0 else 0.0,
        }
    
    def report(self) -> dict:
        """Generate comprehensive decision report."""
        return {
            "decisions": {
                "reuse_attempts": self.decisions.reuse_attempts,
                "reuse_accuracy": f"{self.decisions.reuse_accuracy * 100:.1f}%",
                "say_count": self.decisions.say_count,
                "prediction_hit_rate": f"{self.decisions.prediction_hit_rate * 100:.1f}%",
                "avg_reuse_latency_ms": round(self.decisions.avg_reuse_latency),
                "avg_say_latency_ms": round(self.decisions.avg_say_latency),
            },
            "llm_cache_impact": {
                "miss_penalty_ms": round(self.llm_cache_impact.miss_penalty_ms),
                "miss_interrupt_rate": f"{self.llm_cache_impact.miss_interrupt_rate * 100:.1f}%",
                "hit_interrupt_rate": f"{self.llm_cache_impact.hit_interrupt_rate * 100:.1f}%",
            },
            "tts_cache_impact": {
                "miss_penalty_ms": round(self.tts_cache_impact.miss_penalty_ms),
                "miss_interrupt_rate": f"{self.tts_cache_impact.miss_interrupt_rate * 100:.1f}%",
                "hit_interrupt_rate": f"{self.tts_cache_impact.hit_interrupt_rate * 100:.1f}%",
            },
            "top_candidates": [
                {"id": cid, **self.get_candidate_stats(cid)}
                for cid in sorted(self.candidate_usage, key=self.candidate_usage.get, reverse=True)[:5]
            ],
        }
