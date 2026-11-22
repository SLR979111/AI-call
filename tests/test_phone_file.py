import asyncio
import json

import pytest
from pydantic import ValidationError

from app.brain import Brain
from app.models import Candidate, DEFAULT_CARD, Memory, Say
from app.phone_file import Credit, Flow, Graph, PhoneFile, Route, compile_phone
from app.providers import Cloud, ProviderError
from app.session import Session


def test_model_selects_leaf_with_dependency_order_and_grounded_reopening():
    file = compile_phone(DEFAULT_CARD)
    flow = Flow(file)
    flow.accept("嗯，是。", "您是负责人吗？")
    flow.apply(Route(leaf="goal_reason", completed=[
        Credit(id="purpose", quote="嗯，是。"), Credit(id="step_reason", quote="嗯，是。")
    ], next_node="goal_reason"), "嗯，是。")
    assert flow.context()["current"] == "step_reason"
    assert set(flow.done) == {"purpose"}
    flow.accept("我知道缺的是商标截图。")
    flow.apply(Route(leaf="goal_reason", completed=[Credit(id="step_reason", quote="商标截图")]), flow.need)
    assert flow.context()["current"] == "goal_reason"
    flow.accept("其实我没听懂那张截图是什么。")
    flow.apply(Route(leaf="goal_reason", reopen=[Credit(id="step_reason", quote="没听懂")]), flow.need)
    assert flow.context()["current"] == "step_reason"
    assert "step_next_step" not in flow.path()
    flow.apply(Route(leaf="goal_all", completed=[Credit(id="step_next_step", quote="改好了")]), flow.need)
    assert "step_next_step" not in flow.done


def test_import_rejects_cycle_unknown_edges_and_nonleaf_target():
    file = compile_phone(DEFAULT_CARD)
    for parent in ("goal_all", "missing"):
        invalid = file.model_dump()
        invalid["graph"]["nodes"][0]["requires"] = [parent]
        with pytest.raises(ValidationError):
            PhoneFile.model_validate(invalid)
    with pytest.raises(ValidationError):
        Graph(nodes=file.graph.nodes, default_leaf="purpose")


async def test_rejected_model_tail_keeps_only_validated_speech():
    class Text:
        def __init__(self, lines):
            self.lines = lines

        async def chat(self, messages):
            yield self.lines

    candidate = Candidate(id="c", when="确认负责", reply=Say(text="授权书还少一份。"))
    mem = Memory(DEFAULT_CARD)
    mem.hear("我们继续聊授权的事。")
    brain = Brain(Text('{"type":"reuse","id":"c"}\n{"type":"say","text":"过时的话。"}'))
    brain.decision_engine.should_reuse = lambda *args: ("llm", None)
    brain.semantic_engine = brain.decision_model = None
    assert [e async for e in brain.respond(mem, [candidate])] == [("reuse", candidate.reply)]
    brain.cloud = Text('{"type":"nonsense"}')
    with pytest.raises(ProviderError):
        _ = [e async for e in brain.respond(mem, [])]
    brain.cloud = Text('{"type":"say","text":"先确认一下。"}\n{"type":"update","disposition":"complete"}\nBAD')
    assert [e[0] async for e in brain.respond(mem, [])] == ["say"]


async def test_prepared_audio_clause_is_followed_only_by_the_new_suffix():
    class Voice:
        forecast_clips = Cloud.forecast_clips

        def __init__(self):
            self.spoken = []

        async def warm(self, text):
            return text.encode("utf-16-le")

        def cached(self, text):
            return None

        async def speech(self, text):
            self.spoken.append(text)
            yield text.encode("utf-16-le")

    voice = Voice()
    prefix = "您现在要补的是授权书，"
    old = prefix + "然后等原公司开具。"
    clips = dict([pair async for pair in voice.forecast_clips(old)])
    assert clips[prefix] == prefix.encode("utf-16-le")
    new = prefix + "您已更名，可以先核对新名称。"
    class Response:
        async def respond(self, *_):
            yield "say", Say(text=new)

    sent = []
    async def send(event):
        sent.append(event)
    s = Session(DEFAULT_CARD, voice, Response(), send, predictions=False, voice=True)
    try:
        s.epoch = 1
        s._begin_turn("voice")
        s.memory.hear("我改过公司名。")
        job = asyncio.create_task(s._run(1, user="我改过公司名。", candidates=[], audio=clips))
        for _ in range(100):
            if any(isinstance(e,dict) and e.get("type") == "utterance_end" for e in sent):
                break
            await asyncio.sleep(.01)
        await s.playback_progress(1, 1, s.units[1].sent, done=True)
        await job
        packets = b"".join(b[12:] for b in sent if isinstance(b, bytes))
        assert packets.decode("utf-16-le") == new
        assert voice.spoken == [new[len(prefix):]]
        assert s.turn_metrics["prefix_audio_hit"] and s.units[1].delivered
    finally:
        await s.close()


def test_portable_graph_drives_websocket_reply(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)

    async def chat(self, messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        if "leaves" in body:
            yield json.dumps({"leaf": "goal_reason", "need": "询问截图", "completed": [{"id":"purpose","quote":"哪张截图"}]})
        else:
            assert body["state"]["flow"]["goal"] == "解释导入文件指定的截图小目标"
            yield '{"type":"say","text":"需要商标状态截图。"}\n{"type":"update"}'
    monkeypatch.setattr(Cloud, "chat", chat)
    with TestClient(main.app) as client:
        doc = client.post("/api/phone-file", json=DEFAULT_CARD.model_dump()).json()
        next(n for n in doc["graph"]["nodes"] if n["id"] == "step_reason")["goal"] = "解释导入文件指定的截图小目标"
        assert client.post("/api/phone-file/validate", json=doc).json() == doc
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type":"start","phone_file": doc,"voice": False,"predictions": False})
            def turn():
                events = []
                while True:
                    event = ws.receive_json()
                    assert event["type"] != "error", event
                    events.append(event)
                    if event["type"] == "reply_done":
                        return events
            turn()
            ws.send_json({"type":"text","text":"哪张截图？"})
            events = turn()
            assert any(e.get("flow",{}).get("current") == "step_reason" for e in events)
            assert any(e.get("text") == "需要商标状态截图。" for e in events)
            ws.send_json({"type":"stop"})


@pytest.mark.parametrize("parent,expected", [("purpose", ["purpose"]), ("goal_reason", ["step_reason"])])
async def test_dependency_review_can_shorten_a_path_and_cannot_introduce_cycles(parent, expected):
    class Planner:
        plan_phone = Cloud.plan_phone

        async def chat(self, messages, **kwargs):
            if "changes" in messages[0]["content"]:
                yield json.dumps({"changes": [{"id": "goal_reason", "requires": [parent]}]})
            else:
                yield compile_phone(DEFAULT_CARD).graph.model_dump_json()

    file = await Planner().plan_phone(DEFAULT_CARD)
    goal = next(n for n in file.graph.nodes if n.id == "goal_reason")
    assert goal.requires == expected
