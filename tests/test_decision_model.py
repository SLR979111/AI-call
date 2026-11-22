from __future__ import annotations

import asyncio

from app.brain import Brain
from app.models import Candidate, DEFAULT_CARD, Memory, Say


class FakeDecider:
    def __init__(self, candidate):
        self.candidate = candidate
        self.calls = 0

    async def decide(self, user_input, assistant_last, candidates):
        self.calls += 1
        return self.candidate


class FakeCloud:
    async def chat(self, messages, **kwargs):
        raise AssertionError("decision model reuse must not call full generation")
        yield ""


def build_memory(user: str):
    memory = Memory(DEFAULT_CARD)
    memory.history = [
        {"role": "assistant", "text": "这次资料被退回了，您想先了解哪一部分？",
         "delivery": "complete"},
        {"role": "user", "text": user},
    ]
    return memory


def test_decision_model_selects_candidate_without_generation():
    candidate = Candidate.model_validate({
        "id": "p0_0",
        "when": "用户表示已了解",
        "examples": ["我明白了"],
        "reply": {"text": "这次缺的是商标状态截图。", "covers": [], "question": ""},
    })
    decider = FakeDecider(candidate)
    brain = Brain(FakeCloud(), decision_model=decider)
    class UnsureEngine:
        def should_reuse(self, *args, **kwargs):
            return "llm", None
    brain.decision_engine = UnsureEngine()
    memory = build_memory("品牌方直接给我们授权了。")

    async def run():
        return [event async for event in brain.respond(memory, [candidate])]

    events = asyncio.run(run())
    assert decider.calls == 1
    assert [kind for kind, _ in events] == ["reuse", "update_async"]
    assert brain.last_decision["path"] == "decision_model"
    assert brain.last_decision["candidate_id"] == "p0_0"


def test_default_configuration_and_local_calls_do_not_create_a_cloud_selector(monkeypatch):
    from app.decision_model import DecisionModel
    from app.semantic_decision import SemanticMatcher

    monkeypatch.delenv("DECISION_ENABLED", raising=False)
    monkeypatch.delenv("SEMANTIC_DECISION", raising=False)
    assert DecisionModel.from_env(FakeCloud()) is None
    assert SemanticMatcher.from_env() is None

    monkeypatch.setenv("DECISION_ENABLED", "1")
    cloud = FakeCloud()
    assert DecisionModel.from_env(cloud) is not None
    cloud.is_local = True
    assert DecisionModel.from_env(cloud) is None


async def test_cloud_selector_recovers_from_non_object_model_output():
    from app.decision_model import DecisionModel

    class MalformedCloud:
        async def chat(self, messages, **kwargs):
            yield "[]"

    candidate = Candidate(id="p1", when="愿意继续", reply=Say(text="好的。"))
    assert await DecisionModel(MalformedCloud()).decide("好", "", [candidate]) is None


async def test_semantic_then_cloud_on_an_actual_uncertain_rule():
    from unittest.mock import AsyncMock

    candidate = Candidate(id="p1", when="经销商给的授权", reply=Say(text="需要上游授权。"))
    cloud = FakeDecider(candidate)
    brain = Brain(FakeCloud(), decision_model=cloud)
    memory = build_memory("供货那边出具的")
    assert brain.decision_engine.should_reuse(
        memory.history[-1]["text"], [candidate], memory.history[0]["text"], memory.card)[0] == "llm"
    semantic = AsyncMock()
    semantic.confirm.return_value = candidate
    brain.semantic_engine = semantic
    events = [e async for e in brain.respond(memory, [candidate])]
    assert events[0] == ("reuse", candidate.reply)
    assert brain.last_decision["path"] == "semantic" and cloud.calls == 0

    semantic.confirm.return_value = None
    events = [e async for e in brain.respond(memory, [candidate])]
    assert events[0] == ("reuse", candidate.reply)
    assert brain.last_decision["path"] == "decision_model" and cloud.calls == 1
