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
