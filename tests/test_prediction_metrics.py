"""Prediction reuse, hit-rate and latency metrics.

The goal of these tests is that the live counters agree with the session JSONL
log, that a turn contributes at most one decision, and that an unmeasured
percentile is ``None`` instead of a fake 0.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.brain import Brain
from app.decision_tracker import DecisionTracker
from app.models import DEFAULT_CARD, Candidate, Memory, Say, Update
from app.monitoring import PerformanceMonitor, percentile_report
from app.prediction_stats import build_prediction_stats
from app.providers import Cloud, Settings
from app.session import Session


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def candidate(candidate_id: str, when: str, text: str = "请重新截图。") -> Candidate:
    return Candidate(id=candidate_id, when=when, reply=Say(text=text))


def percents(samples: list[float]) -> dict:
    return percentile_report(samples)


def test_empty_percentiles_are_null_not_zero():
    monitor = PerformanceMonitor()
    assert percentile_report([]) == {"p50": None, "p95": None, "p99": None, "count": 0}
    stats = monitor.get_latency_percentiles("first_text_ms")
    assert stats["count"] == 0 and stats["p50"] is None and stats["p95"] is None

    tracker = DecisionTracker()
    snapshot = build_prediction_stats(tracker, monitor)
    assert snapshot["data_available"] is False
    assert snapshot["reuse"]["rate"] is None
    assert snapshot["prediction"]["hit_rate"] is None
    assert snapshot["latency_ms"]["first_text"]["all"]["p50"] is None
    assert snapshot["cache"]["llm"]["hits"] == 0
    assert snapshot["cache"]["llm"]["hit_rate"] is None
    # Required read-only endpoint fields.
    assert {"rate", "accuracy", "paths"} <= set(snapshot["reuse"])
    assert {"hits", "misses", "hit_rate", "prefetched_audio_bytes",
            "used_audio_bytes"} <= set(snapshot["prediction"])
    for metric in ("first_text", "first_packet", "voice_to_playback"):
        assert {"all", "reused", "not_reused"} <= set(snapshot["latency_ms"][metric])
    for component in ("llm", "tts", "asr"):
        assert {"hits", "misses"} <= set(snapshot["cache"][component])


def test_asr_latency_can_be_recorded_without_a_reply_turn():
    monitor = PerformanceMonitor()
    started = monitor.mark_asr_start()
    monitor.mark_asr_end(True, started_at=started)
    standalone = monitor.report()["asr_standalone"]
    assert standalone["count"] == 1
    assert standalone["p50"] is not None


def test_percentiles_are_correct_and_grouped_by_reuse():
    monitor = PerformanceMonitor()
    for index in range(10):
        turn = monitor.start_turn(f"t{index}")
        turn.first_text_ms = (index + 1) * 10
        turn.first_packet_ms = (index + 1) * 100
        turn.voice_to_playback_ms = (index + 1) * 5
        turn.reused = index % 2 == 0
        turn.path = "rule" if turn.reused else "regenerated"
        turn.completed = True
        monitor.end_turn(turn)

    everything = monitor.get_latency_percentiles("first_text_ms")
    assert everything == {"p50": 55.0, "p95": 95.5, "p99": 99.1, "count": 10}

    reused = monitor.get_latency_percentiles("first_text_ms", reused=True)
    assert reused["count"] == 5 and reused["p50"] == 50.0 and reused["p95"] == 86.0
    not_reused = monitor.get_latency_percentiles("first_text_ms", reused=False)
    assert not_reused["count"] == 5 and not_reused["p50"] == 60.0 and not_reused["p95"] == 96.0

    stats = build_prediction_stats(DecisionTracker(), monitor)
    assert stats["turns"]["completed"] == 10
    assert stats["turns"]["reused"] == 5 and stats["turns"]["not_reused"] == 5
    assert stats["latency_ms"]["first_text"]["reused"]["p50"] == 50.0
    assert stats["latency_ms"]["first_text"]["not_reused"]["p95"] == 96.0
    assert stats["latency_ms"]["first_packet"]["all"]["p50"] == 550.0
    assert stats["latency_ms"]["voice_to_playback"]["all"]["count"] == 10

    # The existing /api/metrics payload exposes the same grouped percentiles.
    report = monitor.report()
    assert report["summary"]["completed_turns"] == 10
    assert report["latency"]["by_reuse"]["reused"]["first_text_ms"]["p50"] == 50.0
    assert report["latency"]["by_reuse"]["not_reused"]["first_text_ms"]["p95"] == 96.0


class _ChatCloud:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        for line in self.lines:
            yield line


def _memory(text: str, previous: str = "您清楚吗？") -> Memory:
    memory = Memory(DEFAULT_CARD)
    memory.history.append({"role": "assistant", "text": previous, "delivery": "complete"})
    memory.history.append({"role": "user", "text": text})
    return memory


UPDATE_LINE = json.dumps({
    "type": "update", "facts": [], "questions": [], "confirmation": "",
    "disposition": "active", "disposition_quote": "",
}, ensure_ascii=False) + "\n"


async def test_brain_reports_rule_model_and_model_text_paths():
    # Rule path: the lexical engine selects a forecast without an LLM call.
    rule_candidates = [candidate(
        "c1", "用户表示知道了或会去重新截图，但没有说明是否已经补交或重新提交，也没有提出新的问题",
        "请重新截图。")]
    brain = Brain(_ChatCloud([]))
    events = [event async for event in brain.respond(_memory("好的。"), rule_candidates)]
    assert events[0][0] == "reuse"
    assert brain.last_decision == {"kind": "reuse", "path": "rule", "candidate_id": "c1"}

    # Model path: the LLM explicitly chooses a candidate id.
    model = Brain(_ChatCloud([
        '{"type":"reuse","id":"c1"}\n', UPDATE_LINE,
    ]))
    events = [event async for event in model.respond(
        _memory("为什么？"), [candidate("c1", "用户会去重新截图")])]
    assert events[0][0] == "reuse"
    assert model.last_decision["path"] == "model"
    assert model.last_decision["candidate_id"] == "c1"

    # Model-text path: the LLM returns text identical to a prepared candidate,
    # so prepared audio can still be reused.
    text_cloud = _ChatCloud([
        json.dumps({"type": "say", "text": "请重新截图。", "covers": [],
                    "question": ""}, ensure_ascii=False) + "\n",
        UPDATE_LINE,
    ])
    text_brain = Brain(text_cloud)
    events = [event async for event in text_brain.respond(
        _memory("为什么？"), [candidate("c1", "用户会去重新截图")])]
    assert events[0][0] == "reuse"
    assert text_brain.last_decision["path"] == "model_text"

    # No predictions at all: a direct reply, not a prediction miss.
    direct = Brain(_ChatCloud([
        json.dumps({"type": "say", "text": "您好。", "covers": [],
                    "question": ""}, ensure_ascii=False) + "\n",
        UPDATE_LINE,
    ]))
    events = [event async for event in direct.respond(_memory("为什么？"), [])]
    assert events[0][0] == "say"
    assert direct.last_decision["path"] == "direct"

    # A user who says "还有别的问题" invalidates the forecast before the
    # decision point; that must also be direct, not a prediction miss.
    unasked = Brain(_ChatCloud([
        json.dumps({"type": "say", "text": "您请讲。", "covers": [],
                    "question": ""}, ensure_ascii=False) + "\n",
        UPDATE_LINE,
    ]))
    events = [event async for event in unasked.respond(
        _memory("还有别的问题"), [candidate("c1", "用户会去重新截图")])]
    assert events[0][0] == "say"
    assert unasked.last_decision["path"] == "direct"

    # Candidates available but the slow path generated new text.
    regenerated = Brain(_ChatCloud([
        json.dumps({"type": "say", "text": "因为材料不全。", "covers": [],
                    "question": ""}, ensure_ascii=False) + "\n",
        UPDATE_LINE,
    ]))
    events = [event async for event in regenerated.respond(
        _memory("为什么？"), [candidate("c1", "用户会去重新截图")])]
    assert events[0][0] == "say"
    assert regenerated.last_decision["path"] == "regenerated"


class FakeCloud:
    """Voice/TTS fake: predictions retain their PCM, new text synthesizes."""

    def __init__(self):
        self.pcm = b"\x01\x00" * 960
        self.last_llm_cache_hit = False
        self.last_asr_cache_hit = False

    async def prefetch(self, text):
        return self.pcm

    def cached(self, text):
        return None

    async def speech(self, text):
        yield self.pcm

    def emit_cache_event(self, *args):
        pass


class ScriptedBrain:
    snapshot = staticmethod(Brain.snapshot)

    def __init__(self):
        self.last_decision = None

    async def predict(self, snapshot):
        return [Candidate(id="c1", when="用户表示明白",
                          reply=Say(text="好的，已经记录。", covers=["reason"]))]

    async def respond(self, memory, candidates):
        text = memory.history[-1]["text"]
        if candidates and text == "好的":
            self.last_decision = {"kind": "reuse", "path": "rule",
                                  "candidate_id": candidates[0].id}
            yield "reuse", candidates[0].reply
            yield "update_async", candidates[0]
        elif candidates and text == "为什么？":
            self.last_decision = {"kind": "say", "path": "regenerated",
                                  "candidate_id": None}
            yield "say", Say(text="因为材料不全。", covers=[])
            yield "say", Say(text="需要补充。", covers=[])
            yield "update", Update()
        else:
            self.last_decision = {"kind": "say", "path": "direct",
                                  "candidate_id": None}
            yield "say", Say(text="您好。", covers=[])
            yield "update", Update()

    async def update_for_reuse(self, memory, selected):
        return Update()


async def _acknowledge(session: Session, events: list, client_ms: float):
    """Acknowledge every playback unit until the turn finishes."""
    acked: set[int] = set()
    async with asyncio.timeout(3):
        while not session.response.done():
            for event in list(events):
                if (not isinstance(event, dict)
                        or event.get("type") != "utterance_end"
                        or event.get("epoch") != session.epoch
                        or event["unit"] in acked):
                    continue
                acked.add(event["unit"])
                await session.playback_started(event["epoch"], client_ms,
                                               unit_id=event["unit"])
                await session.playback_progress(event["epoch"], event["unit"],
                                                event["samples"], done=True)
            await asyncio.sleep(.005)
    await session.response


async def test_session_metrics_match_session_log(tmp_path):
    tracker = DecisionTracker()
    monitor = PerformanceMonitor()
    brain = ScriptedBrain()
    events: list = []

    async def send(payload):
        events.append(payload)

    session = Session(DEFAULT_CARD, FakeCloud(), brain, send, voice=True,
                      predictions=False, log_dir=tmp_path,
                      decision_tracker=tracker, performance_monitor=monitor)
    try:
        # Opening turn: no user input, no candidates.
        await session.start()
        await _acknowledge(session, events, 1000)
        assert session.reply_completed

        # Turn 1: rule-reuse a forecast with prepared audio.
        await session._predict(session.epoch, {})
        await session.user_text("好的", source="voice", end_ms=1000)
        await _acknowledge(session, events, 1300)
        assert session.reply_completed

        # Turn 2: candidates exist, but the reply is regenerated.  Two say
        # events must still count as one decision (the first one).
        await session._predict(session.epoch, {})
        await session.user_text("为什么？", source="voice", end_ms=2000)
        await _acknowledge(session, events, 2300)
        assert session.reply_completed
    finally:
        log_path = session.log_path
        await session.close()

    decisions = tracker.report()["decisions"]
    assert decisions["reuse_attempts"] == 1
    assert decisions["reuse_paths"] == {"rule": 1}
    assert decisions["reuse_correct"] == 1 and decisions["reuse_incorrect"] == 0
    assert decisions["prediction_hits"] == 1
    assert decisions["prediction_misses"] == 1
    assert decisions["regenerations"] == 1
    # Two say events in turn 2 must not inflate say_count/direct decisions.
    assert decisions["say_count"] == 1
    # /api/metrics now returns measured rates instead of the previous fake 0.
    assert decisions["reuse_accuracy"] == 1.0
    assert decisions["prediction_hit_rate"] == 0.5
    assert decisions["reuse_rate"] == 0.5

    records = [json.loads(line) for line in
               log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    finished = [event for event in records if event["event"] == "reply_finished"]
    assert len(finished) == 3

    stats = build_prediction_stats(tracker, monitor)
    assert stats["turns"]["completed"] == len(finished)
    assert stats["turns"]["with_candidates"] == 2
    reuse_logs = [event for event in finished if event.get("reused") is True]
    miss_logs = [event for event in finished
                 if event.get("candidates_available") and not event.get("reused")]
    assert stats["turns"]["reused"] == len(reuse_logs) == 1
    assert stats["reuse"]["attempts"] == len(reuse_logs)
    assert stats["prediction"]["hits"] == len(reuse_logs)
    assert stats["prediction"]["misses"] == len(miss_logs) == 1
    assert stats["turns"]["by_path"]["rule"] == 1
    assert stats["turns"]["by_path"]["regenerated"] == 1

    # Percentiles must be computed from exactly the logged turn samples.
    for log_key, stat_key in (("first_text_ms", "first_text"),
                              ("first_packet_ms", "first_packet"),
                              ("voice_to_playback_ms", "voice_to_playback")):
        assert stats["latency_ms"][stat_key]["all"] == percents(
            [event[log_key] for event in finished if isinstance(event.get(log_key), (int, float))])
        assert stats["latency_ms"][stat_key]["reused"] == percents(
            [event[log_key] for event in reuse_logs if isinstance(event.get(log_key), (int, float))])
        assert stats["latency_ms"][stat_key]["not_reused"] == percents(
            [event[log_key] for event in miss_logs if isinstance(event.get(log_key), (int, float))])

    # Forecast PCM must equal the logged prediction audio for the reused turn.
    assert stats["prediction"]["used_audio_bytes"] == sum(
        event.get("prediction_audio_bytes", 0) for event in reuse_logs)
    assert stats["prediction"]["used_audio_bytes"] > 0
    assert stats["prediction"]["prefetched_audio_bytes"] >= stats["prediction"]["used_audio_bytes"]

    # Rule reuse must not claim an LLM span; generated turns and all
    # synthesized turns must have their marks populated.
    reuse_turn = next(turn for turn in monitor.turns if turn.path == "rule")
    assert reuse_turn.llm_start is None and reuse_turn.llm_first_token is None
    assert reuse_turn.tts_start is not None
    assert reuse_turn.tts_first_chunk is not None and reuse_turn.tts_end is not None
    generated_turn = next(turn for turn in monitor.turns if turn.path == "regenerated")
    assert generated_turn.llm_start is not None
    assert generated_turn.llm_first_token is not None
    assert generated_turn.llm_end is not None


async def test_provider_cache_events_are_counted_once_per_request():
    tracker = DecisionTracker()
    requests = []

    def respond(request):
        requests.append(request)
        body = (b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b'data: [DONE]\n\n')
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = Cloud(Settings(api_key="test"), client,
                      on_cache_event=tracker.record_cache_event)
        messages = [{"role": "user", "content": "hello"}]
        assert "".join([chunk async for chunk in cloud.chat(messages)]) == "ok"
        assert "".join([chunk async for chunk in cloud.chat(messages)]) == "ok"
        await cloud.close()

    report = tracker.report()
    assert len(requests) == 1
    assert report["llm_cache_impact"]["hits"] == 1
    assert report["llm_cache_impact"]["misses"] == 1
    assert report["llm_cache_impact"]["hit_rate"] == 0.5


def test_prediction_stats_route_is_registered():
    import app.main as main

    paths = {getattr(route, "path", None) for route in main.app.routes}
    assert "/api/prediction/stats" in paths
