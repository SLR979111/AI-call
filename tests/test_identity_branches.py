"""Identity/privacy branch regression tests. No LLM/network is required."""
import asyncio

from app.agent.llm_agent import LLMAgent
from app.cancel import CancelToken


def _run(text: str, *, flow_state: str = "IDENTITY_CONFIRM"):
    agent = LLMAgent(None, None, case_context={
        "request_id": "Q_SECRET", "brand": "SECRET_BRAND",
        "reject_l1": "入驻", "reject_l2": "公司主体", "reject_l3": "SECRET_REASON",
    })
    req = {"session_id": "s_test", "turn_id": "t_1", "trace_id": "tr_1",
           "reply_id": "r_1", "user_text": text, "flow_state": flow_state,
           "slots": {}}
    async def collect():
        return [chunk async for chunk in agent.handle(req, CancelToken(), None)]
    return asyncio.run(collect())


def _joined(chunks):
    return "".join(str(chunk.get("text", "")) for chunk in chunks)


def test_identity_denial_exits_without_case_details():
    for text in ("不是", "不是本人", "我不是店铺负责人", "你打错了"):
        chunks = _run(text)
        joined = _joined(chunks)
        assert chunks[-1]["kind"] == "end", text
        assert chunks[-1]["outcome"] == "wrong_contact", text
        assert "SECRET_BRAND" not in joined
        assert "SECRET_REASON" not in joined
        assert "Q_SECRET" not in joined
        assert "打扰" in joined


def test_unclear_or_question_before_identity_only_reasks():
    for text in ("嗯", "应该是吧", "为什么没通过", "你先告诉我"):
        chunks = _run(text)
        joined = _joined(chunks)
        assert chunks[-1]["kind"] == "end", text
        assert chunks[-1]["outcome"] == "pending_identity", text
        assert "SECRET_BRAND" not in joined
        assert "SECRET_REASON" not in joined
        assert "Q_SECRET" not in joined
        assert "店铺负责人" in joined


def test_identity_confirmation_still_uses_case_script():
    chunks = _run("是的")
    joined = _joined(chunks)
    assert chunks[-1]["kind"] == "end"
    assert "SECRET_REASON" in joined
    assert "SECRET_BRAND" not in joined


def test_closing_does_not_continue_case():
    chunks = _run("好的，谢谢")
    joined = _joined(chunks)
    assert chunks[-1]["kind"] == "end"
    assert chunks[-1]["outcome"] == "closing"
    assert "SECRET_REASON" not in joined
    assert "再见" in joined
