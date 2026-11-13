"""Tests for deterministic POP stage presets."""
from app.agent.pop_presets import classify, preset_for

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
