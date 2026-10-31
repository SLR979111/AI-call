from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from app.agent.fake import FakeAgent
from app.compliance import BLOCK, REWRITE, ComplianceGuard
from app.events import EventBus
from app.main import app
from app.orchestrator import Orchestrator
from app.tools.gateway import MockBusiness, ToolGateway

ROOT = Path(__file__).resolve().parent.parent
EVENT_SCHEMA = json.loads((ROOT / "contracts" / "schemas" / "event.v1.json").read_text(encoding="utf-8"))


def temp_dir() -> Path:
    path = ROOT / "data" / "test-runs" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def build() -> tuple[Orchestrator, EventBus, ToolGateway, MockBusiness]:
    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(data, mock, bus)
    orc = Orchestrator(bus, FakeAgent(), tools, ComplianceGuard(), "rel_test_001", "service_outbound")
    return orc, bus, tools, mock


def validate_events(events: list[dict]) -> None:
    validator = jsonschema.Draft7Validator(EVENT_SCHEMA)
    for event in events:
        validator.validate(event)


@pytest.mark.asyncio
async def test_three_turns_and_events():
    orc, bus, _, _ = build()
    session = await orc.create_session("text")
    turns = [
        "你好，我想问一下订单什么时候到？",
        "我想改到明天下午三点。",
        "可以，帮我改吧。",
    ]
    results = [await orc.handle_text(session.session_id, text, None) for text in turns]
    assert len(results) == 3
    assert all("turn_id" in r for r in results)

    events = bus.read_events(session.session_id)
    validate_events(events)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    types = [e["type"] for e in events]
    for required in ("TurnStarted", "TurnCommitted", "AgentStarted", "AgentSentence",
                     "ComplianceChecked", "TTSFirstChunk", "TurnLatency"):
        assert required in types, required
    assert types.count("TurnLatency") == 3
    latency = [e for e in events if e["type"] == "TurnLatency"][0]["payload"]
    assert latency["e2e_ms"] > 0
    assert latency["t0_last_voiced_ms"] <= latency["t9_first_playback_ms"]


@pytest.mark.asyncio
async def test_tool_idempotency():
    _, _, tools, mock = build()
    args = {"order_id": "A12345678", "new_time": "2026-11-01T15:00"}
    first = await tools.invoke("order.reschedule", args, idempotency_key="k-reschedule-1",
                               session_id="s_test", turn_id="t_0001")
    second = await tools.invoke("order.reschedule", args, idempotency_key="k-reschedule-1",
                                session_id="s_test", turn_id="t_0001")
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["data"]["ticket_id"] == second["data"]["ticket_id"]
    assert second["meta"]["cached"] is True
    assert len(mock.tickets) == 1


@pytest.mark.asyncio
async def test_tool_failure_retry_and_error():
    _, _, tools, mock = build()
    mock.fault = "timeout"
    result = await tools.invoke("order.query", {"order_id": "A12345678"},
                                session_id="s_test", turn_id="t_0001")
    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_TIMEOUT"
    assert result["meta"]["attempt"] == 2
    mock.fault = None


@pytest.mark.asyncio
async def test_invalid_tool_args():
    _, _, tools, _ = build()
    result = await tools.invoke("order.query", {"order_id": "bad id"},
                                session_id="s_test", turn_id="t_0001")
    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_INVALID_ARGS"


@pytest.mark.asyncio
async def test_compliance_rewrite_and_block():
    guard = ComplianceGuard()
    rewritten = await guard.check_sentence("我保证明天一定送到")
    assert rewritten.decision == REWRITE
    assert "保证" not in rewritten.text
    blocked = await guard.check_sentence("请提供身份证号")
    assert blocked.decision == BLOCK


class _BadAgent(FakeAgent):
    def __init__(self, text: str) -> None:
        self.text = text

    async def handle(self, req, cancel, tools):
        yield {"kind": "sentence", "reply_id": req["reply_id"], "sentence_id": 1,
               "text": self.text, "final": True}
        yield {"kind": "end", "reply_id": req["reply_id"], "outcome": "completed",
               "slots": {}, "result": {}}


@pytest.mark.asyncio
async def test_compliance_events_are_valid():
    for text, expected in (
        ("我保证明天一定送到", "ComplianceRewritten"),
        ("请提供身份证号", "ComplianceBlocked"),
    ):
        data = temp_dir()
        bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
        mock = MockBusiness()
        tools = ToolGateway(data, mock, bus)
        orc = Orchestrator(bus, _BadAgent(text), tools, ComplianceGuard(), "rel_test_001", "service_outbound")
        session = await orc.create_session("text")
        await orc.handle_text(session.session_id, "测试合规", None)
        events = bus.read_events(session.session_id)
        validate_events(events)
        assert expected in [e["type"] for e in events], (text, expected)


@pytest.mark.asyncio
async def test_barge_in_cancels_turn():
    orc, bus, _, _ = build()
    session = await orc.create_session("voice")
    task = asyncio.create_task(orc.handle_text(session.session_id, "帮我改一下订单", None))
    await asyncio.sleep(0.01)
    await orc.cancel_current(session.session_id, "BARGE_IN")
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.get("aborted") is True
    types = [e["type"] for e in bus.read_events(session.session_id)]
    assert "BargeInDetected" in types
    assert "CancelRequested" in types
    assert "TurnAborted" in types


def test_http_api_smoke():
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        created = client.post("/sessions", json={"mode": "text"}).json()
        sid = created["session_id"]
        turn = client.post(f"/sessions/{sid}/text", json={"text": "我想问一下订单"}).json()
        assert turn["result"]["outcome"] in {"rescheduled", "completed", "tool_failed"}
        events = client.get(f"/sessions/{sid}/events")
        assert events.status_code == 200
        lines = [line for line in events.text.splitlines() if line.strip()]
        assert len(lines) >= 5
        assert json.loads(lines[0])["type"] == "SessionStarted"
