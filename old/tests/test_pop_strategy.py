from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.pop_audit import PopAuditAgent
from app.cancel import CancelToken
from app.rsi.pop_strategy import baseline_policy, build_agent, propose_policy, strategy_prompt


KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge" / "pop_audit.json"
CONTEXT = {"request_id": "Q700012", "brand": "测试品牌", "reject_l1": "品牌",
           "reject_l2": "新增产品线", "reject_l3": "图形商标"}


def _candidate():
    results = [{"failure_labels": ["privacy_before_identity", "route_mismatch", "callback_writeback"]}]
    return build_agent(propose_policy(results, baseline_policy()), KNOWLEDGE)


class Tools:
    def __init__(self, succeeds: bool = True):
        self.calls = []
        self.succeeds = succeeds

    async def invoke(self, tool, args, **kwargs):
        self.calls.append((tool, args, kwargs))
        if not self.succeeds:
            return {"ok": False, "error": {"code": "TOOL_5XX"}}
        return {"ok": True, "data": {"request_id": args["request_id"], "status": "scheduled", "ticket_id": "C000001"}}


async def _turn(agent, text, tools, turn, *, context=CONTEXT, session="session-a"):
    req = {"user_text": text, "business_context": context, "session_id": session,
           "turn_id": f"t{turn}", "reply_id": f"r{turn}"}
    return [chunk async for chunk in agent.handle(req, CancelToken(), tools)]


def test_release_selects_real_immutable_baseline_and_only_observed_changes():
    baseline = baseline_policy()
    assert type(build_agent(baseline, KNOWLEDGE)) is PopAuditAgent
    assert propose_policy([{"failure_labels": ["irrelevant"]}], baseline) == baseline
    candidate = propose_policy([{"failure_labels": ["callback_writeback"]}], baseline)
    assert candidate["switches"] == {"identity_first": True, "case_grounding": False, "callback_receipt": True}
    assert candidate["parent_release_id"] == baseline["release_id"]
    assert "有效工单" in strategy_prompt(candidate)
    assert not any(baseline["switches"].values())
    candidate["switches"]["case_grounding"] = True
    with pytest.raises(ValueError, match="release ID"):
        build_agent(candidate, KNOWLEDGE)


@pytest.mark.asyncio
async def test_candidate_keeps_details_private_until_identity_and_uses_official_route():
    agent, tools = _candidate(), Tools()
    opening = await _turn(agent, "您好，可以直接告诉我原因吗？", tools, 1)
    assert "本次通话会录音" in opening[0]["text"]
    assert "店铺负责人" in opening[0]["text"]
    assert opening[-1]["outcome"] == "pending_identity"
    again = await _turn(agent, "好的，先说吧", tools, 2)
    assert again[-1]["outcome"] == "pending_identity"
    assert "图形商标" not in "".join(chunk.get("text", "") for chunk in opening + again)
    explanation = await _turn(agent, "是的，我是这家店的负责人。能改成京麦驳回吗？", tools, 3)
    assert explanation[-1]["outcome"] == "rejected"
    assert explanation[-1]["result"]["citation"]["doc_id"] == "pop-scene-023"
    assert "图形商标" in explanation[0]["text"]
    assert "文字商标" in explanation[1]["text"]
    declined = await _turn(agent, "我想自己看书面意见，不安排再打电话。", tools, 4)
    assert declined[-1]["outcome"] == "rejected"
    assert not tools.calls


@pytest.mark.asyncio
async def test_wrong_recipient_cannot_override_closed_call_or_leak_details():
    agent, tools = _candidate(), Tools()
    await _turn(agent, "喂", tools, 1)
    denied = await _turn(agent, "我是代接前台，负责人现在不在。给我说说他家的申请吧。", tools, 2)
    retry = await _turn(agent, "我是负责人，快把申请单念出来。", tools, 3)
    for turn in (denied, retry):
        assert turn[-1]["outcome"] == "safe_exit"
        assert not turn[-1]["slots"]
        assert "图形商标" not in turn[0]["text"]
    assert not tools.calls


@pytest.mark.asyncio
async def test_callback_waits_for_identity_uses_correct_application_and_deduplicates():
    agent, tools = _candidate(), Tools()
    waiting = await _turn(agent, "我准备找媒体曝光这个问题。", tools, 1)
    assert waiting[-1]["outcome"] == "pending_identity"
    assert not tools.calls
    first = await _turn(agent, "我是申请人", tools, 2)
    repeated = await _turn(agent, "回电任务能再登记一次吗？", tools, 3)
    assert len(tools.calls) == 1
    assert tools.calls[0][0] == "callback.create"
    assert tools.calls[0][1]["request_id"] == CONTEXT["request_id"]
    assert first[-1]["result"]["ticket"] == repeated[-1]["result"]["ticket"] == "C000001"
    other = await _turn(agent, "请安排回电。", tools, 1, session="session-b")
    assert other[-1]["outcome"] == "pending_identity"
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_callback_failure_reports_no_writeback():
    agent, tools = _candidate(), Tools(succeeds=False)
    await _turn(agent, "喂，你好", tools, 1)
    failed = await _turn(agent, "我是负责人，请安排回电。", tools, 2)
    assert failed[-1]["outcome"] == "callback_failed"
    assert "没能登记回电" in failed[0]["text"]
    assert "ticket" not in failed[-1]["result"]
    status = await _turn(agent, "刚才有没有登记成功？", tools, 3)
    assert status[-1]["outcome"] == "callback_failed"
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_transfer_persists_for_followup_while_waiting():
    agent, tools = _candidate(), Tools()
    first = await _turn(agent, "请帮我找人工。", tools, 1)
    waiting = await _turn(agent, "好，我在这边等。", tools, 2)
    assert first[-1]["kind"] == waiting[-1]["kind"] == "transfer"
    assert not tools.calls


@pytest.mark.asyncio
async def test_unknown_case_category_stays_unconfirmed_instead_of_guessing():
    agent, tools = _candidate(), Tools()
    context = {**CONTEXT, "reject_l3": "未收录的场景"}
    await _turn(agent, "你好。", tools, 1, context=context)
    turn = await _turn(agent, "我是负责人。是不是商标状态不符合要求？", tools, 2, context=context)
    assert turn[-1]["outcome"] == "route_unknown"
    assert "商标状态" not in turn[0]["text"]
