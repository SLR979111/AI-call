"""Semantic tie-breaker: it may rescue a forecast, never fail a turn."""

import asyncio

import numpy as np
import pytest

from app.brain import Brain
from app.models import DEFAULT_CARD, Candidate, Memory, Say
from app.semantic_decision import SemanticMatcher


def _vector(*values: float) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


class FakeEmbedder:
    """Deterministic vectors so the thresholds, not the model, decide."""

    def __init__(self, vectors: dict[str, np.ndarray], delay: float = 0.0):
        self.vectors = vectors
        self.delay = delay
        self.calls: list[list[str]] = []

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True,
               batch_size=None):
        self.calls.append(list(texts))
        if self.delay:
            import time

            time.sleep(self.delay)
        rows = []
        for text in texts:
            vector = self.vectors.get(text)
            if vector is None:
                vector = _vector(1.0, 0.0, 0.0)
            norm = np.linalg.norm(vector)
            rows.append(vector / norm if norm else vector)
        return np.asarray(rows, dtype=np.float32)


def _candidate(cid: str, when: str, text: str = "好的。") -> Candidate:
    return Candidate(id=cid, when=when, reply=Say(text=text))


QUESTION = "是品牌方直接授权，还是走经销商？"


def test_short_answer_is_rescued_when_the_score_clears_threshold():
    engine = SemanticMatcher(embedder=FakeEmbedder({
        QUESTION: _vector(0.0, 1.0, 0.0),
        "走经销商这条线": _vector(1.0, 1.0, 0.0),
        "品牌方直接授权": _vector(0.0, 0.2, 1.0),
    }), threshold=0.7, margin=0.05)
    candidates = [
        _candidate("direct", "品牌方直接授权"),
        _candidate("reseller", "走经销商这条线"),
    ]
    chosen = engine.best("走经销商的", candidates, QUESTION)
    assert chosen is not None and chosen.id == "reseller"


def test_low_score_falls_back_to_the_model():
    engine = SemanticMatcher(embedder=FakeEmbedder({
        QUESTION: _vector(0.0, 1.0, 0.0),
        "用户确认提交了": _vector(0.0, 0.0, 1.0),
        "品牌方直接授权": _vector(0.0, 1.0, 0.0),
        "嗯，这样吧": _vector(1.0, 0.0, 0.0),
    }), threshold=0.7, margin=0.05)
    candidates = [_candidate("submitted", "用户确认提交了"),
                  _candidate("direct", "品牌方直接授权")]
    assert engine.best("嗯，这样吧", candidates, QUESTION) is None


def test_clear_winner_needs_a_margin():
    engine = SemanticMatcher(embedder=FakeEmbedder({
        "远杉那边": _vector(1.0, 0.0, 0.0),
        "远杉转授权": _vector(1.0, 0.02, 0.0),
        "远杉直接给": _vector(1.0, 0.01, 0.0),
    }), threshold=0.7, margin=0.05)
    candidates = [_candidate("a", "远杉转授权"), _candidate("b", "远杉直接给")]
    # Both branches score ~1.0: the answer cannot tell them apart, so ask the model.
    assert engine.best("远杉那边", candidates, QUESTION) is None


def test_a_new_question_or_a_farewell_is_never_rescued():
    engine = SemanticMatcher(embedder=FakeEmbedder({}), threshold=0.7)
    candidates = [_candidate("reseller", "走经销商这条线")]
    assert engine.best("那我要补什么材料？", candidates, QUESTION) is None
    assert engine.best("算了不用了", candidates, QUESTION) is None
    # Without a question from the assistant there is nothing to answer.
    assert engine.best("走经销商的", candidates, "材料已经收到了。") is None


def test_negation_is_never_turned_into_agreement():
    # "不是经销商" has the same topic as "经销商转授权"; embeddings ignore polarity.
    engine = SemanticMatcher(embedder=FakeEmbedder({
        "不是经销商": _vector(1.0, 0.0, 0.0),
        "经销商转授权": _vector(1.0, 0.0, 0.0),
    }), threshold=0.6, margin=0.0)
    assert engine.best("不是经销商", [_candidate("reseller", "经销商转授权")], QUESTION) is None


def test_disabled_or_unavailable_engine_is_silent(monkeypatch):
    monkeypatch.setenv("SEMANTIC_DECISION", "0")
    assert SemanticMatcher.from_env() is None
    monkeypatch.setenv("SEMANTIC_DECISION", "1")
    monkeypatch.setenv("SEMANTIC_MODEL_DIR", "/nonexistent/bge")
    assert SemanticMatcher.from_env() is None


@pytest.mark.asyncio
async def test_confirm_returns_none_instead_of_blocking_the_turn():
    slow = FakeEmbedder({"走经销商的": _vector(1.0, 0.0, 0.0),
                         "走经销商这条线": _vector(1.0, 0.0, 0.0)}, delay=0.2)
    engine = SemanticMatcher(embedder=slow, threshold=0.7, margin=0.05, timeout_ms=10)
    candidates = [_candidate("reseller", "走经销商这条线")]
    assert await engine.confirm("走经销商的", candidates, QUESTION) is None


@pytest.mark.asyncio
async def test_confirm_is_async_and_returns_the_candidate():
    engine = SemanticMatcher(embedder=FakeEmbedder({
        "走经销商的": _vector(1.0, 0.0, 0.0),
        "走经销商这条线": _vector(1.0, 0.0, 0.0),
    }), threshold=0.7, margin=0.05)
    candidates = [_candidate("reseller", "走经销商这条线")]
    chosen = await engine.confirm("走经销商的", candidates, QUESTION)
    assert chosen is not None and chosen.id == "reseller"


class StubCloud:
    """Minimal model stream: one line and one state update."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        yield '{"type":"say","text":"那我们再看下一步。","covers":[],"question":""}\n'
        yield '{"type":"update","disposition":"active"}\n'


def _memory_with_answer(answer: str) -> Memory:
    memory = Memory(DEFAULT_CARD)
    memory.history = [
        {"role": "user", "text": "材料我收到了", "delivery": "complete"},
        {"role": "assistant", "text": QUESTION, "delivery": "complete"},
        {"role": "user", "text": answer, "delivery": "complete"},
    ]
    return memory


@pytest.mark.asyncio
async def test_brain_reuses_the_semantic_choice_and_records_its_path():
    brain = Brain(StubCloud())
    brain.decision_engine = type("Lexical", (), {
        "should_reuse": staticmethod(lambda *args, **kwargs: ("say", None))
    })()
    brain.semantic_engine = SemanticMatcher(embedder=FakeEmbedder({
        "走经销商的": _vector(1.0, 0.0, 0.0),
        "走经销商这条线": _vector(1.0, 0.0, 0.0),
    }), threshold=0.7, margin=0.05)
    candidates = [_candidate("reseller", "走经销商这条线", "那您让经销商补一份授权。")]

    events = [event async for event in brain.respond(_memory_with_answer("走经销商的"), candidates)]

    assert [kind for kind, _ in events] == ["reuse", "update_async"]
    assert brain.last_decision == {"kind": "reuse", "path": "semantic",
                                  "candidate_id": "reseller"}


@pytest.mark.asyncio
async def test_brain_still_regenerates_when_no_semantic_engine_is_configured():
    cloud = StubCloud()
    brain = Brain(cloud)
    brain.decision_engine = type("Lexical", (), {
        "should_reuse": staticmethod(lambda *args, **kwargs: ("say", None))
    })()
    brain.semantic_engine = None

    events = [event async for event in brain.respond(
        _memory_with_answer("走经销商的"), [_candidate("reseller", "走经销商这条线")])]

    assert cloud.calls == 1
    assert [kind for kind, _ in events] == ["say", "update"]
    assert brain.last_decision["path"] == "regenerated"
