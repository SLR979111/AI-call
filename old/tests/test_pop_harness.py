import pytest

from app.eval.pop_judge import validate_review
from app.eval.pop_runner import CaseAgent, SimulatedMerchant


def test_merchant_verifies_only_when_the_agent_asked():
    script = [{"say": "对，我是负责人", "verifies_identity": True,
               "if_previous_matches": ["负责人"], "otherwise": "你是哪里打来的？"}]
    assert SimulatedMerchant(script).next_input(["请问您是负责人吗？"])["verified_identity"]
    fallback = SimulatedMerchant(script).next_input(["您提交的申请已驳回。"])
    assert fallback["user_text"] == "你是哪里打来的？"
    assert not fallback["verified_identity"]


def test_case_agent_does_not_leak_simulator_state():
    class Agent:
        def handle(self, request, cancel, tools):
            return request

    request = CaseAgent(Agent(), {"request_id": "Q100001"}).handle({"user_text": "喂"}, None, None)
    assert request == {"user_text": "喂", "business_context": {"request_id": "Q100001"}}


def test_judge_requires_real_agent_evidence():
    turns = [{"turn": 1, "merchant": "保我过", "agent": ["请以复审为准。"]}]
    scored = {
        dimension: {"verdict": "pass", "reason": "有证据", "evidence": [{"turn": 1, "quote": "以复审为准"}]}
        for dimension in ("clarity", "dialogue_fit", "semantic_safety")
    }
    assert validate_review(scored, turns) == scored
    scored["semantic_safety"]["evidence"][0]["quote"] = "保我过"
    with pytest.raises(ValueError, match="absent"):
        validate_review(scored, turns)
