import httpx
import pytest

from app.agent.pop_competition import OPENING, RECORDING, STAGES, PopCompetitionAgent, identity_answer
from app.agent.pop_repair import SemanticTopics
from app.cancel import CancelToken
from app.compliance import ComplianceGuard
from app.eval.pop_runner import KNOWLEDGE
from app.events import EventBus
from app.orchestrator import Orchestrator
from app.tools.pop_writeback import PopContactStore

CONTEXT = {"request_id": "Q983101", "brand": "演示百禾",
           "reject_l1": "入驻", "reject_l2": "公司主体", "reject_l3": "主体资质到期"}


def runtime(tmp_path, fault=False):
    bus = EventBus(tmp_path, "pop-contest-test", "local-test")
    store = PopContactStore(tmp_path / "contacts.db", bus, fault=fault)
    return bus, store, PopCompetitionAgent(KNOWLEDGE, CONTEXT, store)


async def reply(agent, number, text):
    request = {"session_id": "s_contest", "turn_id": f"t_{number}",
               "trace_id": f"trace_{number}", "reply_id": f"reply_{number}", "user_text": text}
    return [chunk async for chunk in agent.handle(request, CancelToken(), None)]


@pytest.mark.asyncio
async def test_confirm_then_write_only_once(tmp_path):
    bus, store, agent = runtime(tmp_path)
    try:
        chunks = []
        for number, text in enumerate(("你好", "嗯，对对", "行，您讲", "我还没明白了，先解释"), 1):
            chunks.extend(await reply(agent, number, text))
        assert not bus.read_events("s_contest")
        chunks.extend(await reply(agent, 5, "好，清楚了"))
        assert [item["stage"] for item in chunks if item["kind"] == "stage"] == list(STAGES)
        result = chunks[-1]["result"]["writeback"]
        assert result["confirmed_turn_id"] == "t_5"
        assert result["request_id"] == CONTEXT["request_id"]
        again = await reply(agent, 6, "我听明白了")
        assert again[-1]["result"]["writeback"] == result
        assert [row["type"] for row in bus.read_events("s_contest")] == ["ToolCalled", "ToolReturned"]
    finally:
        store.close()
        bus.close()


@pytest.mark.parametrize("utterance, verdict", [
    ("是的", "confirmed"), ("对的", "confirmed"), ("是", "confirmed"), ("是的呀，您说", "confirmed"),
    ("没错", "confirmed"), ("是的我是店铺负责人", "confirmed"), ("不是", "denied"),
    ("是的，不过我不是店铺负责人", "denied"), ("我以前负责这家店，已经离职了", "denied"),
    ("应该是吧", "unclear"), ("嗯", "unclear"), ("我是点釜人", "unclear"), ("是吗", "unclear"),
    ("对", "confirmed"), ("对对", "confirmed"), ("嗯，对，对，您说吧", "confirmed"),
    ("对，这家店归我管", "confirmed"), ("这个店是我负责的", "confirmed"),
    ("您好，我是这家店的老板", "confirmed"), ("我就是这里的负责人吧", "unclear"),
    ("对不起，您找谁", "unclear"), ("这店是我负责的吗？", "unclear"),
    ("对，不过我替他接的电话", "denied"), ("我只是帮忙代接", "denied"),
])
def test_identity_answer(utterance, verdict):
    assert identity_answer(utterance) == verdict


@pytest.mark.asyncio
async def test_call_ready_opens_once_without_a_customer_transcript(tmp_path):
    bus, store, agent = runtime(tmp_path)
    orc = Orchestrator(bus, agent, None, ComplianceGuard(), tts_provider="fake")
    session = await orc.create_session("voice")

    class Connection:
        async def send_json(self, message):
            if message["type"] == "tts_started":
                await orc.on_client_event(session.session_id, {
                    "type": "playback_first_frame", "turn_id": message["turn_id"],
                    "reply_id": message["reply_id"],
                })

    conn = Connection()
    try:
        await orc.attach(session.session_id, conn)
        await orc.open_outbound(session.session_id, conn)
        await orc.open_outbound(session.session_id, conn)
        events = bus.read_events(session.session_id)
        assert [row["payload"]["trigger"] for row in events if row["type"] == "TurnStarted"] == [
            "outbound_opening",
        ]
        assert [row["payload"]["text"] for row in events if row["type"] == "AgentSentence"] == [
            OPENING, RECORDING,
        ]
        assert not any(row["type"] in {"Transcript", "TurnLatency", "ToolCalled"} for row in events)
        assert any(row["type"] == "OutboundOpeningLatency" for row in events)
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
async def test_short_yes_only_advances_after_question(tmp_path):
    bus, store, agent = runtime(tmp_path)
    try:
        opening = await reply(agent, 1, "是的")
        assert opening[-1]["outcome"] == "pending_identity"
        uncertain = await reply(agent, 2, "嗯")
        assert uncertain[-1]["outcome"] == "pending_identity"
        assert any("直接回答" in chunk.get("text", "") for chunk in uncertain)
        confirmation = await reply(agent, 3, "对的")
        assert confirmation[-1]["outcome"] == "pending_need"
        assert [chunk["stage"] for chunk in confirmation if chunk["kind"] == "stage"] == ["IDENTITY_CONFIRM"]
        assert not bus.read_events("s_contest")
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
async def test_wrong_person_exits_without_disclosure(tmp_path):
    bus, store, agent = runtime(tmp_path)
    try:
        await reply(agent, 1, "你好")
        reply_chunks = await reply(agent, 2, "我不是店铺负责人，告诉我是哪一单")
        assert reply_chunks[-1]["outcome"] == "safe_exit"
        assert CONTEXT["brand"] not in str(reply_chunks)
        assert not bus.read_events("s_contest")
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
async def test_negative_ack_and_reason_followups_answer_without_writing(tmp_path):
    bus, store, agent = runtime(tmp_path)
    try:
        for number, text in enumerate(("", "对", "哪一项没达标？"), 1):
            await reply(agent, number, text)
        recap = await reply(agent, 4, "没有")
        recap_text = "".join(row.get("text", "") for row in recap)
        assert "主体资质到期" in recap_text and "上传有效材料" in recap_text
        reason = await reply(agent, 5, "没有听明白，我想要确认驳回原因啊")
        assert reason[0]["text"] == "这次退回是因为主体资质到期。"
        assert reason[-1]["result"]["followup"] == {"topics": ["reason"], "source": "local"}
        assert not bus.read_events("s_contest")
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
async def test_modification_instructions_are_grounded_and_do_not_confirm(tmp_path):
    bus, store, agent = runtime(tmp_path)
    try:
        await reply(agent, 1, "")
        before_identity = await reply(agent, 2, "如何修改资质？")
        assert before_identity[-1]["outcome"] == "pending_identity"
        assert "已失效" not in str(before_identity)
        await reply(agent, 3, "对")
        await reply(agent, 4, "方便")
        how = await reply(agent, 5, "如何修改资质？")
        speech = "".join(row.get("text", "") for row in how)
        assert how[-1]["result"]["followup"]["topics"] == ["process"]
        assert all(term in speech for term in ("京麦", "原申请", "上传有效材料", "书面驳回意见"))
        blocked = await reply(agent, 6, "页面没有修改按钮")
        assert "联系审核支持" in str(blocked)
        progress = await reply(agent, 7, "多久能看到结果？")
        assert "无法核实复审时间" in str(progress)
        assert not bus.read_events("s_contest")
        confirmed = await reply(agent, 8, "清楚了")
        assert confirmed[-1]["outcome"] == "pop_contact_recorded"
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_text,topics,source", [
    ('{"topics":["channel"]}', ["channel"], "classified"),
    ('{"topics":["writeback"]}', ["reason", "action"], "invalid_recap"),
])
async def test_semantic_fallback_cannot_confirm_or_write(tmp_path, model_text, topics, source):
    bus, store, agent = runtime(tmp_path)
    calls = []

    async def model(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": model_text}}]})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            agent.semantic_topics = SemanticTopics(client, "https://simulated.invalid", "stub", "unused")
            for number, text in enumerate(("", "要给我看一下吗", "对", "方便"), 1):
                await reply(agent, number, text)
            assert not calls
            result = await reply(agent, 5, "后台我翻了好一阵，还是摸不着门道")
            assert len(calls) == 1
            assert result[-1]["result"]["followup"] == {"topics": topics, "source": source}
            assert result[-1]["outcome"] == "pending_result"
            assert not bus.read_events("s_contest")
    finally:
        store.close()
        bus.close()


@pytest.mark.asyncio
async def test_write_failure_is_not_success(tmp_path):
    bus, store, agent = runtime(tmp_path, fault=True)
    try:
        for number, text in enumerate(("你好", "我是店铺负责人", "方便请说"), 1):
            await reply(agent, number, text)
        failed = await reply(agent, 4, "我听明白了")
        assert failed[-1]["outcome"] == "writeback_failed"
        assert all(row.get("stage") != "WRITEBACK" for row in failed)
        assert [row["type"] for row in bus.read_events("s_contest")] == ["ToolCalled", "ToolFailed"]
        assert store.conn.execute("SELECT count(*) FROM contacts").fetchone()[0] == 0
    finally:
        store.close()
        bus.close()
