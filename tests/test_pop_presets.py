"""Tests for deterministic POP stage presets."""
import asyncio
from pathlib import Path

from app.agent.llm_agent import LLMAgent
from app.agent.pop_presets import classify, preset_for
from app.cancel import CancelToken
from app.flow.pack import FlowPack

CTX = {"reject_l3": "主体资质到期", "remediation": "更新有效资质后重新提交"}


def test_classify_common_merchant_replies():
    assert classify("好") == "affirmative"
    assert classify("好的，谢谢") == "closing"
    assert classify("不用了") == "negative"
    assert classify("为什么被驳回") == "confused"
    assert classify("我要投诉") == "transfer"
    assert classify("你们是不是骗子") == "abuse"
    assert classify("我现在忙") == "busy"
    assert classify("已经提交了") == "already"


def test_stage_sequence_does_not_repeat_identity():
    reason = preset_for("REASON", "好", CTX, verified=True)
    assert reason is not None
    assert "负责人" not in reason.reply
    assert "主体资质到期" in reason.reply
    assert reason.intent == "confirm"

    remediation = preset_for("REMEDIATION", "好", CTX, verified=True)
    assert remediation is not None
    assert "负责人" not in remediation.reply
    assert "资质" in remediation.reply

    confirm = preset_for("CONFIRM_ACTION", "好", CTX, verified=True)
    assert confirm is not None
    assert "负责人" not in confirm.reply
    assert "登记" in confirm.reply

    close = preset_for("CLOSE", "好", CTX, verified=True)
    assert close is not None
    assert close.close is True
    assert "再见" in close.reply


def test_unverified_stage_never_discloses_case():
    for stage in ("REASON", "REMEDIATION"):
        p = preset_for(stage, "为什么", CTX, verified=False)
        assert p is not None
        assert "主体资质到期" not in p.reply
        assert "负责人" in p.reply


def test_safety_presets_work_in_any_stage():
    assert preset_for("REASON", "我要投诉", CTX, verified=True).outcome == "transferred"
    assert preset_for("REASON", "你们是不是骗子", CTX, verified=True).close is True
    assert preset_for("REASON", "不用了", CTX, verified=True).close is True
    assert preset_for("REASON", "我现在忙", CTX, verified=True).close is True


def test_agent_advances_short_affirmatives_without_reasking_identity():
    pack = FlowPack.load(Path(r"H:\aicall\flowpacks\pop_qualification_audit.json"))
    agent = LLMAgent(None, None, flow_pack=pack, case_context=CTX)
    replies = []

    async def scenario():
        await agent.start_session("s_seq", {}, {"id": pack.id})

        async def turn(text, state, n):
            req = {"session_id": "s_seq", "turn_id": f"t_{n}", "trace_id": "tr_seq",
                   "reply_id": f"r_{n}", "user_text": text, "flow_state": state,
                   "slots": {}}
            chunks = [c async for c in agent.handle(req, CancelToken(), None)]
            reply = "".join(str(c.get("text", "")) for c in chunks)
            snapshot = (chunks[-1].get("result") or {}).get("flow_snapshot") or {}
            replies.append((state, reply, chunks[-1].get("outcome"), snapshot.get("state")))
            return snapshot.get("state", state)

        state = await turn("是的", "IDENTITY_CONFIRM", 1)
        assert state == "REASON"
        for n in range(2, 7):
            state = await turn("好", state, n)

    asyncio.run(scenario())

    # Identity must not be asked again after the first confirmation.
    assert all("负责人吗" not in reply for _, reply, _, _ in replies[1:])
    assert any("主体资质到期" in reply for _, reply, _, _ in replies)
    assert replies[-1][2] == "closing"
