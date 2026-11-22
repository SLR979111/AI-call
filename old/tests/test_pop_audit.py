from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.pop_audit import PopAuditAgent
from app.compliance import ComplianceGuard
from app.eval.runner import EvalRunner
from app.events import EventBus
from app.flow.pack import FlowEngine, FlowPack
from app.orchestrator import Orchestrator
from app.rag.knowledge import KnowledgeBase
from app.tools.gateway import MockBusiness, ToolGateway
from app.voice.fake import FakeTts

ROOT = Path(__file__).resolve().parents[1]


def temp_dir() -> Path:
    path = ROOT / "data" / f"test-pop-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build(tmp_path: Path):
    bus = EventBus(tmp_path, "rel_pop_test", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(tmp_path, mock, bus)
    agent = PopAuditAgent(ROOT / "knowledge" / "pop_audit.json", pause_ms=0)
    orc = Orchestrator(bus, agent, tools, ComplianceGuard(), "rel_pop_test",
                       "pop_qualification_audit", tts=FakeTts())
    return orc, bus, tools, mock


class RecordingAgent:
    """Test double for the Orchestrator <-> Agent session contract."""

    def __init__(self) -> None:
        self.started: tuple | None = None
        self.requests: list[dict] = []

    async def start_session(self, session_id, version_set, flow_pack) -> None:
        self.started = (session_id, version_set, flow_pack)

    async def handle(self, req, cancel, tools):
        self.requests.append(req)
        yield {"kind": "sentence", "reply_id": req["reply_id"], "sentence_id": 1,
               "text": "收到", "final": True}
        yield {"kind": "end", "reply_id": req["reply_id"], "outcome": "completed",
               "slots": {"probe": "ok"}, "result": {}}



def test_pop_flowpack_valid():
    pack = FlowPack.load(ROOT / "flowpacks" / "pop_qualification_audit.json")
    engine = FlowEngine(pack)
    assert engine.start()["state"] == "OPEN"
    assert engine.apply_intent("confirm")["state"] == "IDENTITY_CONFIRM"
    assert engine.apply_intent("confirm")["state"] == "REASON"
    assert engine.apply_intent("confirm")["state"] == "REMEDIATION"
    assert engine.apply_intent("confirm")["state"] == "CONFIRM_ACTION"
    engine.set_slot("request_id", "Q000001")
    assert engine.apply_intent("confirm")["state"] == "WRITEBACK"


def test_pop_knowledge_has_scene_and_policy():
    kb = KnowledgeBase(ROOT / "knowledge" / "pop_audit.json")
    hits = kb.search("授权链路不完整需要补什么", top_k=3)
    assert hits
    assert any("授权链路" in h.title for h in hits)
    assert any(d.get("id") == "pop-policy-phone-vs-jd" for d in kb.docs)


@pytest.mark.asyncio
async def test_pop_agent_explains_reason_and_action():
    orc, _, _, _ = build(temp_dir())
    session = await orc.create_session("text")
    result = await orc.handle_text(session.session_id, "授权链路不完整，需要补什么材料？", None)
    assert result["result"]["outcome"] == "rejected"
    slots = result["result"]["slots"]
    assert slots["handling_mode"] == "电话"
    assert "授权链" in slots["remediation"]
    assert any("授权" in s for s in result["sentences"])


@pytest.mark.asyncio
async def test_pop_agent_jd_reject_and_callback():
    orc, _, _, mock = build(temp_dir())
    session = await orc.create_session("text")
    jd = await orc.handle_text(session.session_id, "图形商标，京麦驳回就行吧？", None)
    assert jd["result"]["outcome"] == "jd_rejected"

    callback = await orc.handle_text(session.session_id, "我没听清，能安排回电吗？", None)
    assert callback["result"]["outcome"] == "callback_scheduled"
    assert any(t.get("type") == "callback" for t in mock.tickets.values())


@pytest.mark.asyncio
async def test_pop_eval_suite():
    orc, bus, _, _ = build(temp_dir())
    runner = EvalRunner(orc, bus)
    cases = runner.load_cases(ROOT / "eval" / "cases" / "pop_audit.json")
    report = await runner.run(cases)
    assert report["cases"] == 6
    assert report["task_success_rate"] == 1.0, report["failure_counts"]
    assert report["p50_ms"] is not None


@pytest.mark.asyncio
async def test_orchestrator_wires_agent_session_and_flow_context():
    data = temp_dir()
    bus = EventBus(data, "rel_wire_test", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(data, mock, bus)
    agent = RecordingAgent()
    orc = Orchestrator(bus, agent, tools, ComplianceGuard(), "rel_wire_test",
                       "pop_qualification_audit", tts=FakeTts())
    session = await orc.create_session("text")
    assert agent.started is not None
    assert agent.started[0] == session.session_id
    assert agent.started[2]["id"] == "pop_qualification_audit"

    await orc.handle_text(session.session_id, "你好", None)
    assert agent.requests
    assert agent.requests[0]["flow_state"] == "OPEN"
    assert "slots" in agent.requests[0]
    assert session.slots.get("probe") == "ok"
