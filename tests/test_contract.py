from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import jsonschema
import pytest
import httpx
from fastapi.testclient import TestClient

from app.agent.fake import FakeAgent
from app.agent.llm_agent import LLMAgent
from app.compliance import BLOCK, REWRITE, ComplianceGuard
from app.events import EventBus
from app.eval.runner import EvalCase, EvalRunner
from app.flow.pack import FlowEngine, FlowPack
from app.llm.router import LLMRouter, ProviderConfig, parse_json_object
from app.main import app
from app.media.livekit import create_livekit_token, decode_payload
from app.rag.knowledge import KnowledgeBase
from app.orchestrator import Orchestrator
from app.registry import Registry, RegistryError
from app.reporting import load_session_events, render_markdown, summarize_events
from app.rsi.gate import GateConfig, compare as rsi_compare
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


def test_registry_candidate_promote_rollback():
    registry = Registry(temp_dir())
    production = registry.production()
    candidate = registry.create_candidate(
        {
            "flow": "service_outbound@1.0.1",
            "prompt": "agent_system@2",
            "knowledge": "faq@dev",
            "model": "fake@dev",
            "config": "runtime@1",
        },
        metrics={"task_success": 0.9},
        notes="test candidate",
    )
    assert candidate["status"] == "candidate"
    promoted = registry.promote(candidate["release_id"], {"ok": True})
    assert promoted["status"] == "production"
    assert registry.production()["release_id"] == candidate["release_id"]
    rolled = registry.rollback()
    assert rolled["release_id"] == production["release_id"]
    assert registry.production()["release_id"] == production["release_id"]
    try:
        registry.create_candidate({"flow": "only"})
        raise AssertionError("expected RegistryError")
    except RegistryError:
        pass


@pytest.mark.asyncio
async def test_llm_failover_and_events():
    def handler(request: httpx.Request) -> httpx.Response:
        if "primary.test" in str(request.url):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{\"reply\":\"好的\"}"}}],
            "usage": {"total_tokens": 12},
        })

    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    router = LLMRouter(
        ProviderConfig("primary", "https://primary.test/v1", "k1", "m1"),
        ProviderConfig("secondary", "https://secondary.test/v1", "k2", "m2"),
        bus=bus, transport=httpx.MockTransport(handler),
    )
    result = await router.complete([{"role": "user", "content": "hi"}], session_id="s_llm")
    assert result.ok is True
    assert result.provider == "secondary"
    events = bus.read_events("s_llm")
    validate_events(events)
    types = [e["type"] for e in events]
    assert "ProviderFailed" in types
    assert "ProviderFailover" in types
    assert "ProviderReturned" in types
    assert parse_json_object(result.text) == {"reply": "好的"}


@pytest.mark.asyncio
async def test_llm_budget_and_unconfigured():
    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    router = LLMRouter(daily_budget_cny=0.0, bus=bus,
                       primary=ProviderConfig("p", "https://p.test/v1", "k", "m"))
    result = await router.complete([{"role": "user", "content": "hi"}], session_id="s_budget")
    assert result.ok is False
    assert result.error and result.error["code"] == "BUDGET_EXCEEDED"
    events = bus.read_events("s_budget")
    validate_events(events)
    assert "BudgetExceeded" in [e["type"] for e in events]

    unconfigured = LLMRouter()
    result = await unconfigured.complete([{"role": "user", "content": "hi"}])
    assert result.ok is False
    assert result.error and result.error["code"] == "LLM_NOT_CONFIGURED"


def test_knowledge_search():
    kb = KnowledgeBase(ROOT / "knowledge" / "faq.json")
    chunks = kb.search("退款多久到账", top_k=2)
    assert chunks
    assert chunks[0].doc_id == "faq-refund"
    assert chunks[0].citation()["title"]


@pytest.mark.asyncio
async def test_agent_uses_knowledge_and_cites():
    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(data, mock, bus)
    agent = FakeAgent(ROOT / "knowledge" / "faq.json")
    orc = Orchestrator(bus, agent, tools, ComplianceGuard(), "rel_test_001", "service_outbound")
    session = await orc.create_session("text")
    result = await orc.handle_text(session.session_id, "退款多久能到账", None)
    assert result["result"]["outcome"] == "answered"
    assert "citation" in result["result"]["result"]
    events = bus.read_events(session.session_id)
    validate_events(events)


def test_flowpack_load_and_transition():
    pack = FlowPack.load(ROOT / "flowpacks" / "service_outbound.json")
    engine = FlowEngine(pack)
    start = engine.start()
    assert start["state"] == "OPEN"
    step = engine.apply_intent("confirm")
    assert step["state"] == "IDENTITY_CONFIRM"
    step = engine.apply_intent("confirm")
    assert step["state"] == "NEED_CONFIRM"
    engine.set_slot("order_id", "A12345678")
    step = engine.apply_intent("confirm")
    assert step["state"] == "OFFER"
    assert engine.snapshot()["slots"]["order_id"] == "A12345678"


@pytest.mark.asyncio
async def test_eval_runner_metrics():
    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(data, mock, bus)
    orc = Orchestrator(bus, FakeAgent(ROOT / "knowledge" / "faq.json"), tools,
                       ComplianceGuard(), "rel_test_001", "service_outbound")
    runner = EvalRunner(orc, bus)
    cases = [
        EvalCase("faq", ["退款多久能到账"], "answered"),
        EvalCase("transfer", ["我要转人工"], "transferred"),
    ]
    report = await runner.run(cases)
    assert report["cases"] == 2
    assert report["task_success_rate"] == 1.0
    assert report["p50_ms"] is not None
    assert report["failure_counts"] == {}


def test_livekit_token_and_voice_status():
    token = create_livekit_token("key", "secret", "s_test", "aicall-s_test", ttl_seconds=60)
    payload = decode_payload(token)
    assert payload["iss"] == "key"
    assert payload["sub"] == "s_test"
    assert payload["video"]["room"] == "aicall-s_test"
    assert len(token.split(".")) == 3

    from app.voice.factory import voice_status
    status = voice_status()
    assert status.provider
    assert isinstance(status.to_dict(), dict)


@pytest.mark.asyncio
async def test_llm_agent_tool_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "intent": "reschedule",
                "reply": "可以帮您改约。",
                "slots": {"order_id": "A12345678"},
                "tool": "order.query",
                "tool_args": {"order_id": "A12345678"},
                "outcome": "rescheduled",
            }, ensure_ascii=False)}}],
            "usage": {"total_tokens": 20},
        })

    data = temp_dir()
    bus = EventBus(data, "rel_test_001", "clock-test", mode="text")
    mock = MockBusiness()
    tools = ToolGateway(data, mock, bus)
    router = LLMRouter(
        ProviderConfig("mock", "https://mock.test/v1", "k", "m"),
        bus=bus, transport=httpx.MockTransport(handler),
    )
    agent = LLMAgent(router, ROOT / "knowledge" / "faq.json")
    orc = Orchestrator(bus, agent, tools, ComplianceGuard(), "rel_test_001", "service_outbound")
    session = await orc.create_session("text")
    result = await orc.handle_text(session.session_id, "帮我改一下订单", None)
    assert result["result"]["outcome"] == "rescheduled"
    events = bus.read_events(session.session_id)
    validate_events(events)
    types = [e["type"] for e in events]
    assert "ProviderCalled" in types
    assert "ToolCalled" in types
    assert "ToolReturned" in types


def test_ws_tts_metadata_turn_result_and_hangup():
    with TestClient(app) as client:
        created = client.post("/sessions", json={"mode": "text"}).json()
        sid = created["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "session_ready"
            assert ready["protocol_version"] == "0.2"
            assert ready["tts_alignment"] is False
            assert ws.receive_json()["type"] == "state"

            ws.send_json({"type": "transport_degraded", "from": "webrtc",
                          "to": "ws_pcm", "reason": "ice_failed"})
            assert ws.receive_json()["type"] == "transport_switched"
            ws.send_json({"type": "media_request"})
            media = ws.receive_json()
            assert media["type"] == "error"
            assert media["error"]["code"] == "MEDIA_NOT_CONFIGURED"

            ws.send_json({"type": "text", "text": "退款多久能到账"})
            seen: list[str] = []
            sent_first = False
            for _ in range(40):
                msg = ws.receive_json()
                seen.append(msg["type"])
                if msg["type"] == "agent_text" and not sent_first:
                    ws.send_json({
                        "type": "playback_first_frame", "turn_id": msg["turn_id"],
                        "reply_id": msg["reply_id"], "client_mono_ms": 1.0,
                        "audio_ctx_time": 0.1,
                    })
                    sent_first = True
                if msg["type"] == "turn_result":
                    break
            for expected in ("tts_started", "tts_sentence", "tts_completed",
                             "compliance", "turn_result"):
                assert expected in seen, (expected, seen)

            ws.send_json({"type": "hangup", "reason": "user_click"})
            ended = None
            for _ in range(10):
                msg = ws.receive_json()
                if msg["type"] == "session_ended":
                    ended = msg
                    break
            assert ended is not None
            assert ended["reason"] == "user_click"


def test_rsi_gate_pass_and_fail():
    baseline = {"results": [{"task_success": i < 10} for i in range(20)],
                "p50_ms": 1000, "p95_ms": 1800, "compliance_pass_rate": 1.0}
    candidate = {"results": [{"task_success": i < 16} for i in range(20)],
                 "p50_ms": 1050, "p95_ms": 1850, "compliance_pass_rate": 1.0}
    result = rsi_compare(baseline, candidate, GateConfig(min_delta=0.03))
    assert result["decision"] == "pass"
    assert result["delta"] > 0
    assert result["ci95"][0] > 0

    worse = {"results": [{"task_success": i < 8} for i in range(20)],
             "p50_ms": 1400, "p95_ms": 2400, "compliance_pass_rate": 0.9}
    failed = rsi_compare(baseline, worse, GateConfig(min_delta=0.03))
    assert failed["decision"] == "fail"
    assert failed["reasons"]


def test_report_from_fixture():
    fixture = ROOT / "contracts" / "fixtures" / "session_normal.jsonl"
    events = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = summarize_events(events)
    assert summary["turns"] == 1
    assert summary["p50_ms"] == 1135
    assert summary["compliance_checked"] >= 1
    markdown = render_markdown(ROOT / "data", [("s_normal", events)])
    assert "# 自测报告" in markdown
    assert "P50 首字时延" in markdown
    assert "s_normal" in markdown

    loaded = load_session_events(ROOT / "data", "s_normal")
    assert loaded == []  # fixture is not a runtime session; helper must be tolerant
