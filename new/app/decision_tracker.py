"""Decision tracking and analysis for reuse vs say choices.

One user turn contributes at most one reuse/say decision and at most one
reuse outcome, so the counters below can be summed without double counting.
Latency percentiles live in :mod:`app.monitoring`; this module only keeps the
decision and cache counters.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

CacheType = Literal["llm", "tts", "asr"]
ReusePath = Literal["rule", "model", "model_text", "semantic", "decision_model", "unknown"]


def _rate(numerator: int, denominator: int) -> float | None:
    """Return a rate, or ``None`` when there is no measured denominator."""
    return round(numerator / denominator, 4) if denominator > 0 else None


def _mean(samples: list[float]) -> float | None:
    return round(sum(samples) / len(samples), 1) if samples else None


@dataclass
class DecisionStats:
    """Track reuse vs say decision outcomes."""

    reuse_attempts: int = 0
    reuse_correct: int = 0  # Reply finished without an interruption
    reuse_incorrect: int = 0  # Reply was interrupted or failed
    say_count: int = 0

    prediction_hits: int = 0  # Forecast candidates reused by the turn
    prediction_misses: int = 0  # Candidates were available but regenerated

    # How a reuse was selected.  "rule" is the lexical decision engine, "model"
    # is the LLM choosing a candidate id and "model_text" is the LLM returning
    # text identical to a prepared candidate.
    reuse_paths: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    regenerations: int = 0  # Turns with candidates that produced a new reply

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
    def reuse_rate(self) -> float:
        total = self.reuse_attempts + self.regenerations
        return self.reuse_attempts / total if total > 0 else 0.0

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
    """Track cache hit/miss counts and the latency of each operation."""

    miss_latency_samples: list[float] = field(default_factory=list)
    hit_latency_samples: list[float] = field(default_factory=list)

    miss_interrupts: int = 0  # Request was cancelled/failed before completion
    hit_interrupts: int = 0

    @property
    def hits(self) -> int:
        return len(self.hit_latency_samples)

    @property
    def misses(self) -> int:
        return len(self.miss_latency_samples)

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total > 0 else None

    @property
    def miss_penalty_ms(self) -> float | None:
        """Average latency increase from a cache miss."""
        if not self.miss_latency_samples or not self.hit_latency_samples:
            return None
        return (_mean(self.miss_latency_samples) -
                _mean(self.hit_latency_samples))

    @property
    def miss_interrupt_rate(self) -> float | None:
        return _rate(self.miss_interrupts, self.misses)

    @property
    def hit_interrupt_rate(self) -> float | None:
        return _rate(self.hit_interrupts, self.hits)


class DecisionTracker:
    """Centralized tracking for all decision points."""

    def __init__(self):
        self.decisions = DecisionStats()
        self.llm_cache_impact = CacheMissImpact()
        self.tts_cache_impact = CacheMissImpact()
        self.asr_cache_impact = CacheMissImpact()

        # Per-candidate tracking
        self.candidate_usage: dict[str, int] = defaultdict(int)
        self.candidate_success: dict[str, int] = defaultdict(int)

        # Forecast audio prepared for later re-use vs actually played.
        self.prediction_audio_prefetched_bytes = 0
        self.prediction_audio_used_bytes = 0

    def record_reuse(self, candidate_id: str, latency_ms: float, *,
                     path: ReusePath = "unknown"):
        """Record one reuse decision.

        Call this once per user turn: a reply may be emitted as several
        utterance units, but the decision itself is singular.
        """
        self.decisions.reuse_attempts += 1
        self.decisions.reuse_latency_samples.append(latency_ms)
        self.decisions.reuse_paths[path] += 1
        self.candidate_usage[candidate_id] += 1

    def record_reuse_outcome(self, candidate_id: str, correct: bool):
        """Record whether a reused reply finished smoothly.

        ``correct`` means the reply reached ``reply_finished`` without being
        interrupted or failing.  Interrupted reuse is counted as incorrect.
        """
        if correct:
            self.decisions.reuse_correct += 1
            self.candidate_success[candidate_id] += 1
        else:
            self.decisions.reuse_incorrect += 1

    def record_say(self, latency_ms: float, *, path: str = "direct"):
        """Record one generated (non-reused) reply decision."""
        self.decisions.say_count += 1
        self.decisions.say_latency_samples.append(latency_ms)
        if path == "regenerated":
            self.decisions.regenerations += 1

    def record_prediction_hit(self, candidate_id: str):
        """Record a forecast that was actually reused by this turn."""
        self.decisions.prediction_hits += 1

    def record_prediction_miss(self):
        """Record a turn where forecasts were available but not reused."""
        self.decisions.prediction_misses += 1

    def record_prediction_audio(self, *, prefetched_bytes: int = 0,
                                used_bytes: int = 0):
        """Record forecast PCM bytes prepared and/or actually played."""
        if prefetched_bytes > 0:
            self.prediction_audio_prefetched_bytes += prefetched_bytes
        if used_bytes > 0:
            self.prediction_audio_used_bytes += used_bytes

    def record_cache_event(self, cache_type: CacheType, hit: bool,
                           latency_ms: float, interrupted: bool = False):
        """Record exactly one cache/network operation per provider request."""
        impact = {
            "llm": self.llm_cache_impact,
            "tts": self.tts_cache_impact,
            "asr": self.asr_cache_impact,
        }[cache_type]

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

    @staticmethod
    def _cache_report(impact: CacheMissImpact) -> dict:
        return {
            "hits": impact.hits,
            "misses": impact.misses,
            "hit_rate": impact.hit_rate,
            "avg_hit_latency_ms": _mean(impact.hit_latency_samples),
            "avg_miss_latency_ms": _mean(impact.miss_latency_samples),
            "miss_penalty_ms": (round(impact.miss_penalty_ms, 1)
                                if impact.miss_penalty_ms is not None else None),
            "miss_interrupts": impact.miss_interrupts,
            "hit_interrupts": impact.hit_interrupts,
            "miss_interrupt_rate": impact.miss_interrupt_rate,
            "hit_interrupt_rate": impact.hit_interrupt_rate,
        }

    def report(self) -> dict:
        """Generate comprehensive decision report.

        Rates are ``None`` when their denominator is empty, not a fake 0.
        """
        stats = self.decisions
        produced = stats.reuse_attempts + stats.regenerations
        return {
            "decisions": {
                "reuse_attempts": stats.reuse_attempts,
                "reuse_correct": stats.reuse_correct,
                "reuse_incorrect": stats.reuse_incorrect,
                "reuse_accuracy": (_rate(stats.reuse_correct,
                                         stats.reuse_correct + stats.reuse_incorrect)),
                "say_count": stats.say_count,
                "regenerations": stats.regenerations,
                "reuse_rate": _rate(stats.reuse_attempts, produced),
                "reuse_paths": dict(stats.reuse_paths),
                "prediction_hits": stats.prediction_hits,
                "prediction_misses": stats.prediction_misses,
                "prediction_hit_rate": (_rate(stats.prediction_hits,
                                              stats.prediction_hits + stats.prediction_misses)),
                "avg_reuse_latency_ms": _mean(stats.reuse_latency_samples),
                "avg_say_latency_ms": _mean(stats.say_latency_samples),
                "prediction_audio_prefetched_bytes": self.prediction_audio_prefetched_bytes,
                "prediction_audio_used_bytes": self.prediction_audio_used_bytes,
            },
            "llm_cache_impact": self._cache_report(self.llm_cache_impact),
            "tts_cache_impact": self._cache_report(self.tts_cache_impact),
            "asr_cache_impact": self._cache_report(self.asr_cache_impact),
            "top_candidates": [
                {"id": cid, **self.get_candidate_stats(cid)}
                for cid in sorted(self.candidate_usage, key=self.candidate_usage.get,
                                  reverse=True)[:5]
            ],
        }
