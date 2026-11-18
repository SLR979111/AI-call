"""Read-only aggregation for ``GET /api/prediction/stats``.

The counters come from the same turn and decision records that write the
session JSONL log.  Nothing here invents samples: when a metric was not
measured the percentile is ``None`` and its ``count`` is 0.
"""

from __future__ import annotations

from .monitoring import PerformanceMonitor, percentile_report


LATENCY_METRICS = (
    ("first_text", "first_text_ms"),
    ("first_packet", "first_packet_ms"),
    ("voice_to_playback", "voice_to_playback_ms"),
    ("answer_first_packet", "answer_first_packet_ms"),
    ("voice_to_answer", "voice_to_answer_ms"),
)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _samples(turns, metric: str) -> list[float]:
    values = []
    for turn in turns:
        value = getattr(turn, metric, None)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _latency_group(all_turns, reused_turns, not_reused_turns) -> dict:
    return {
        name: {
            "all": percentile_report(_samples(all_turns, metric)),
            "reused": percentile_report(_samples(reused_turns, metric)),
            "not_reused": percentile_report(_samples(not_reused_turns, metric)),
        }
        for name, metric in LATENCY_METRICS
    }


def _cache_bucket(report: dict) -> dict:
    return {
        "hits": report.get("hits", 0),
        "misses": report.get("misses", 0),
        "hit_rate": report.get("hit_rate"),
        "avg_hit_latency_ms": report.get("avg_hit_latency_ms"),
        "avg_miss_latency_ms": report.get("avg_miss_latency_ms"),
        "miss_interrupts": report.get("miss_interrupts", 0),
        "hit_interrupts": report.get("hit_interrupts", 0),
    }


def build_prediction_stats(decision_tracker,
                           performance_monitor: PerformanceMonitor | None) -> dict:
    """Build a JSON-safe snapshot for the read-only stats endpoint."""
    decisions = decision_tracker.report() if decision_tracker is not None else {}
    decision_stats = decisions.get("decisions", {})
    turns = list(performance_monitor.turns) if performance_monitor is not None else []
    completed = [turn for turn in turns if turn.completed]
    reuse_turns = [turn for turn in completed if turn.reused is True]
    regenerated_turns = [turn for turn in completed if turn.reused is False]
    predicted_turns = [turn for turn in completed if turn.candidates_available > 0]
    audio_turns = [turn for turn in predicted_turns
                   if turn.prediction_audio_hit is not None]

    by_path: dict[str, int] = {}
    for turn in completed:
        if turn.path:
            by_path[turn.path] = by_path.get(turn.path, 0) + 1

    hits = int(decision_stats.get("prediction_hits", 0))
    misses = int(decision_stats.get("prediction_misses", 0))
    prefetched = int(decision_stats.get("prediction_audio_prefetched_bytes", 0))
    used = int(decision_stats.get("prediction_audio_used_bytes", 0))
    reuse_attempts = int(decision_stats.get("reuse_attempts", 0))
    regenerations = int(decision_stats.get("regenerations", 0))

    cache = {
        "llm": _cache_bucket(decisions.get("llm_cache_impact", {})),
        "tts": _cache_bucket(decisions.get("tts_cache_impact", {})),
        "asr": _cache_bucket(decisions.get("asr_cache_impact", {})),
    }

    measured = bool(completed or hits or misses or prefetched or used
                    or any(bucket["hits"] or bucket["misses"]
                           for bucket in cache.values()))
    return {
        "data_available": measured,
        "turns": {
            "completed": len(completed),
            "incomplete": len(turns) - len(completed),
            "with_candidates": len(predicted_turns),
            "reused": len(reuse_turns),
            "not_reused": len(regenerated_turns),
            "by_path": by_path,
        },
        "reuse": {
            "attempts": reuse_attempts,
            "rate": _rate(reuse_attempts, reuse_attempts + regenerations),
            "accuracy": decision_stats.get("reuse_accuracy"),
            "correct": int(decision_stats.get("reuse_correct", 0)),
            "incorrect": int(decision_stats.get("reuse_incorrect", 0)),
            "paths": dict(decision_stats.get("reuse_paths", {})),
            "regenerations": regenerations,
        },
        "prediction": {
            "hits": hits,
            "misses": misses,
            "hit_rate": _rate(hits, hits + misses),
            "prefetched_audio_bytes": prefetched,
            "used_audio_bytes": used,
            "audio_hit_turns": sum(1 for turn in audio_turns
                                   if turn.prediction_audio_hit),
            "audio_miss_turns": sum(1 for turn in audio_turns
                                    if not turn.prediction_audio_hit),
            "measured_audio_turns": len(audio_turns),
        },
        "latency_ms": _latency_group(completed, reuse_turns, regenerated_turns),
        "cache": cache,
    }
