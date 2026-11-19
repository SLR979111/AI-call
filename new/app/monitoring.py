"""Complete monitoring system for latency, cache, and decision tracking."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field


def percentile_report(samples: list[float]) -> dict:
    """Return P50/P95/P99 with linear interpolation.

    An empty sample set yields ``None`` percentiles and ``count == 0`` so a
    caller can distinguish "no measurement" from a real 0 ms latency.
    """
    values = sorted(float(value) for value in samples
                    if isinstance(value, (int, float)) and math.isfinite(value))
    count = len(values)
    if count == 0:
        return {"p50": None, "p95": None, "p99": None, "count": 0}

    def at(percent: float) -> float:
        if count == 1:
            return values[0]
        position = (count - 1) * percent / 100
        lower = int(math.floor(position))
        fraction = position - lower
        if lower + 1 < count:
            value = values[lower] + (values[lower + 1] - values[lower]) * fraction
        else:
            value = values[lower]
        return round(value, 1)

    return {"p50": at(50), "p95": at(95), "p99": at(99), "count": count}


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

    # Session turn metrics mirrored from Session.turn_metrics.  Keeping them on
    # the turn object lets P50/P95 be grouped by reuse without re-reading logs.
    first_text_ms: float | None = None
    first_packet_ms: float | None = None
    answer_first_packet_ms: float | None = None
    voice_to_playback_ms: float | None = None
    voice_to_answer_ms: float | None = None
    reused: bool | None = None
    source: str | None = None
    path: str | None = None
    candidates_available: int = 0
    prediction_audio_bytes: int = 0
    prediction_audio_hit: bool | None = None
    completed: bool = False

    @property
    def asr_latency_ms(self) -> float | None:
        if self.asr_start is not None and self.asr_end is not None:
            return (self.asr_end - self.asr_start) * 1000
        return None

    @property
    def llm_ttft_ms(self) -> float | None:
        """Time to first token."""
        if self.llm_start is not None and self.llm_first_token is not None:
            return (self.llm_first_token - self.llm_start) * 1000
        return None

    @property
    def llm_total_ms(self) -> float | None:
        if self.llm_start is not None and self.llm_end is not None:
            return (self.llm_end - self.llm_start) * 1000
        return None

    @property
    def tts_first_chunk_ms(self) -> float | None:
        if self.tts_start is not None and self.tts_first_chunk is not None:
            return (self.tts_first_chunk - self.tts_start) * 1000
        return None

    @property
    def tts_total_ms(self) -> float | None:
        if self.tts_start is not None and self.tts_end is not None:
            return (self.tts_end - self.tts_start) * 1000
        return None

    @property
    def end_to_end_ms(self) -> float | None:
        """From user input to first audio sent."""
        if self.first_audio_sent is not None:
            return (self.first_audio_sent - self.started) * 1000
        return None


class PerformanceMonitor:
    """Comprehensive monitoring for all performance metrics."""

    # Metrics mirrored from the session log/reply_finished event.
    SESSION_METRICS = (
        "first_text_ms",
        "first_packet_ms",
        "answer_first_packet_ms",
        "voice_to_playback_ms",
        "voice_to_answer_ms",
        "asr_latency_ms",
        "llm_ttft_ms",
        "llm_total_ms",
        "tts_first_chunk_ms",
        "tts_total_ms",
        "end_to_end_ms",
    )

    def __init__(self):
        self.turns: list[LatencyBreakdown] = []
        self.current_turn: LatencyBreakdown | None = None
        self._closed_turns: set[int] = set()
        # ASR runs before a response turn exists; keep those samples separately.
        self.asr_latency_samples: list[float] = []

        # Aggregated metrics
        self.task_completions: int = 0
        self.task_failures: int = 0
        self.total_interrupts: int = 0
        self.total_errors: int = 0

        # Error tracking
        self.error_counts: dict[str, int] = defaultdict(int)

    def start_turn(self, turn_id: str) -> LatencyBreakdown:
        """Start tracking a new turn and return its per-turn object."""
        turn = LatencyBreakdown(turn_id=turn_id, started=time.perf_counter())
        self.current_turn = turn
        return turn

    def end_turn(self, turn: LatencyBreakdown | None = None):
        """Finalize a turn object (never the same object twice)."""
        target = turn if turn is not None else self.current_turn
        if target is None:
            return
        key = id(target)
        if key not in self._closed_turns:
            self._closed_turns.add(key)
            self.turns.append(target)
        if self.current_turn is target:
            self.current_turn = None
        # Bound memory for long-running servers; aggregate stats are unaffected.
        if len(self.turns) > 2000:
            removed = self.turns[:-2000]
            self.turns = self.turns[-2000:]
            for item in removed:
                self._closed_turns.discard(id(item))

    def _active(self, turn: LatencyBreakdown | None) -> LatencyBreakdown | None:
        return turn if turn is not None else self.current_turn

    def mark_asr_start(self, turn: LatencyBreakdown | None = None) -> float:
        now = time.perf_counter()
        target = self._active(turn)
        if target is not None:
            target.asr_start = now
        return now

    def mark_asr_end(self, cache_hit: bool, turn: LatencyBreakdown | None = None,
                     started_at: float | None = None):
        now = time.perf_counter()
        target = self._active(turn)
        if target is not None:
            target.asr_end = now
            target.cache_hits["asr"] = cache_hit
        elif started_at is not None:
            self.asr_latency_samples.append((now - started_at) * 1000)

    def mark_llm_start(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.llm_start = time.perf_counter()

    def mark_llm_first_token(self, cache_hit: bool, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.llm_first_token = time.perf_counter()
            target.cache_hits["llm"] = cache_hit

    def mark_llm_end(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.llm_end = time.perf_counter()

    def mark_tts_start(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.tts_start = time.perf_counter()

    def mark_tts_first_chunk(self, cache_hit: bool, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.tts_first_chunk = time.perf_counter()
            target.cache_hits["tts"] = cache_hit

    def mark_tts_end(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.tts_end = time.perf_counter()

    def mark_first_audio_sent(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.first_audio_sent = time.perf_counter()

    def mark_interrupt(self, turn: LatencyBreakdown | None = None):
        target = self._active(turn)
        if target is not None:
            target.interrupted = True
        self.total_interrupts += 1

    def record_task_completion(self, success: bool):
        if success:
            self.task_completions += 1
        else:
            self.task_failures += 1

    def record_error(self, error_type: str):
        self.error_counts[error_type] += 1
        self.total_errors += 1

    def get_latency_percentiles(self, metric: str, *, reused: bool | None = None,
                                completed_only: bool = True) -> dict:
        """Calculate P50/P95/P99 for one metric, optionally by reuse group."""
        samples: list[float] = []
        for turn in self.turns:
            if completed_only and not turn.completed:
                continue
            if reused is not None and turn.reused is not reused:
                continue
            value = getattr(turn, metric, None)
            if isinstance(value, (int, float)) and math.isfinite(value):
                samples.append(float(value))
        return percentile_report(samples)

    def _by_reuse(self) -> dict:
        return {
            "reused": {metric: self.get_latency_percentiles(metric, reused=True)
                       for metric in self.SESSION_METRICS},
            "not_reused": {metric: self.get_latency_percentiles(metric, reused=False)
                           for metric in self.SESSION_METRICS},
        }

    def report(self) -> dict:
        """Generate comprehensive performance report."""
        turns = self.turns
        completed = sum(1 for turn in turns if turn.completed)
        predicted = [turn for turn in turns
                     if turn.completed and turn.candidates_available > 0]
        interp = {
            "total_turns": len(turns),
            "completed_turns": completed,
            "incomplete_turns": len(turns) - completed,
            "turns_with_candidates": len(predicted),
            "task_completions": self.task_completions,
            "task_failures": self.task_failures,
            "total_interrupts": self.total_interrupts,
            "total_errors": self.total_errors,
            "interrupt_rate": (round(self.total_interrupts / len(turns), 4)
                               if turns else None),
        }
        return {
            "summary": interp,
            "latency": {
                "end_to_end": self.get_latency_percentiles("end_to_end_ms"),
                "llm_ttft": self.get_latency_percentiles("llm_ttft_ms"),
                "llm_total": self.get_latency_percentiles("llm_total_ms"),
                "asr": self.get_latency_percentiles("asr_latency_ms"),
                "tts_first_chunk": self.get_latency_percentiles("tts_first_chunk_ms"),
                "first_text": self.get_latency_percentiles("first_text_ms"),
                "first_packet": self.get_latency_percentiles("first_packet_ms"),
                "answer_first_packet": self.get_latency_percentiles("answer_first_packet_ms"),
                "voice_to_playback": self.get_latency_percentiles("voice_to_playback_ms"),
                "voice_to_answer": self.get_latency_percentiles("voice_to_answer_ms"),
                "by_reuse": self._by_reuse(),
            },
            "asr_standalone": percentile_report(self.asr_latency_samples),
            "cache_impact": {
                "llm_hits": sum(1 for t in turns if t.cache_hits.get("llm") is True),
                "llm_misses": sum(1 for t in turns if t.cache_hits.get("llm") is False),
                "tts_hits": sum(1 for t in turns if t.cache_hits.get("tts") is True),
                "tts_misses": sum(1 for t in turns if t.cache_hits.get("tts") is False),
            },
            "errors": dict(self.error_counts),
        }
