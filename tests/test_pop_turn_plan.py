import pytest

from app.agent.pop_competition import normalize_answer
from app.agent.pop_turn_plan import RETRY, check_plan, plan_reply


def test_question_plan_preserves_coverage_and_does_not_treat_acknowledgment_as_confirmation():
    text = "听懂了。这个该怎么办，去哪里交？"
    labels, _ = check_plan({"intents": [
        {"intent": "understood", "quote": "听懂了"},
        {"intent": "action", "quote": "这个该怎么办"},
        {"intent": "channel", "quote": "去哪里交"},
    ]}, text)
    assert labels == ("action", "channel")
    assert plan_reply(labels, "缺材料", "补这笔申请要求的授权") == [
        "请补这笔申请要求的授权。",
        "您可以在京麦的这笔原申请里查看书面驳回意见。",
    ]


def test_plan_rejects_non_verbatim_evidence_and_prioritizes_explicit_stop():
    text = "知道了，先不要记录。"
    with pytest.raises(ValueError):
        check_plan({"intents": [{"intent": "understood", "quote": "我确认处理办法"}]}, text)
    assert check_plan({"intents": [
        {"intent": "understood", "quote": "知道了"},
        {"intent": "stop", "quote": "先不要记录"},
    ]}, text)[0] == ("stop",)


@pytest.mark.parametrize("user,expected", [
    ("好的，请重试一次。", True),
    ("好，您重新记录一下吧。", True),
    ("先别重试，我不想继续了。", False),
    ("你知道怎么重试吗？", False),
    ("可以重试一下，不过我还有个问题。", False),
])
def test_retry_requires_a_direct_request(user, expected):
    assert bool(RETRY.fullmatch(normalize_answer(user))) is expected
