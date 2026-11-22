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
        assert "task" not in body
        assert {"basic_response","background","graph"} <= body.keys()
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
            opening = turn()
            assert [e["text"] for e in opening if e["type"] == "utterance"] == [doc["basic_response"]["opening"]]
            states = [e["flow"] for e in opening if e.get("flow")]
            assert states[0]["current"] == "greeting" and states[0]["completed"] == ["call_start"]
            delivered = ws.receive_json()
            assert delivered["type"] == "state" and "greeting" in delivered["flow"]["completed"]
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
                yield json.dumps({"guide":"直接回答","background":"已有记录","graph":compile_phone(DEFAULT_CARD).graph.model_dump()})

    file = await Planner().plan_phone(DEFAULT_CARD)
    goal = next(n for n in file.graph.nodes if n.id == "goal_reason")
    assert goal.requires == expected


def test_confirmed_file_is_self_contained_and_source_never_enters_model_context():
    from app.phone_file import model_context
    source = DEFAULT_CARD.model_copy(update={"facts":"AUTHORING_SOURCE_ONLY", "goal":"ORIGINAL_MANUAL"})
    file = compile_phone(source).model_copy(update={"background":"经确认的背景"})
    memory = Memory(source, flow=Flow(file))
    data = model_context(Brain.snapshot(memory))
    for original in ("AUTHORING_SOURCE_ONLY", "ORIGINAL_MANUAL"):
        assert original not in json.dumps(data,ensure_ascii=False)
    assert data["basic_response"] == file.basic_response.model_dump()
    assert data["graph"] == file.graph.model_dump()
    assert data["state"]["flow"]["goal"]
    assert "task" not in data and "task" not in file.model_dump()
    assert PhoneFile.model_validate_json(file.model_dump_json()).task.facts == "经确认的背景"
    legacy = {"format":"ai-call.phone.dag.v1","task":source.model_dump(),"graph":file.graph.model_dump()}
    assert PhoneFile.model_validate(legacy).graph == file.graph


def test_completed_instructions_do_not_loop_after_acknowledgment():
    memory = Memory(DEFAULT_CARD, flow=Flow(compile_phone(DEFAULT_CARD)))
    memory.flow.judged = True
    memory.delivered = {p.id for p in DEFAULT_CARD.checkpoints}
    memory.history = [{"role":"assistant", "delivery":"complete", "text":"提交后留意申请页的新提示就行。"}]
    for ack in ("嗯，明白了。","好的。"):
        memory.hear(ack)
        assert memory.closing_reason(ack) == "instructions_acknowledged"
        memory.history.pop()
    for followup in ("好的，但是截图怎么弄？","我明白了，然后呢？"):
        memory.hear(followup)
        assert not memory.closing_reason(followup)
        memory.history.pop()
    memory.delivered.clear()
    memory.hear("好的。")
    assert not memory.closing_reason("好的。")
    repeated = Candidate(id="old",when="同意",reply=Say(text="提交后留意申请页的新提示就行。"))
    assert memory.fresh_candidates([repeated]) == []


async def test_candidate_pie_settles_after_requested_audio_only(monkeypatch):
    release = asyncio.Event()
    class Voice:
        async def prefetch(self,text):
            await release.wait()
            return b"spoken"
    class Forecast:
        async def predict(self,snapshot):
            return [Candidate(id="x",when="问时间",reply=Say(text="九点开门。"))]
    sent = []
    async def send(e):
        sent.append(e)
    session = Session(DEFAULT_CARD,Voice(),Forecast(),send,voice=True)
    monkeypatch.setenv("PREDICTION_AUDIO_LIMIT","1")
    try:
        await session._predict(session.epoch,{})
        assert sent[-1]["progress"] == {"ready":0,"target":1,"settled":False,"complete":False}
        release.set()
        await session.settle_audio()
        assert sent[-1]["type"] == "candidate_progress"
        assert sent[-1]["progress"] == {"ready":1,"target":1,"settled":True,"complete":True}
    finally:
        release.set()
        await session.close()

def test_old_files_gain_one_entry_on_all_paths_without_overwriting_business_ids():
    doc = compile_phone(DEFAULT_CARD).model_dump()
    doc["graph"]["nodes"] = [
        {"id":"call_start","goal":"确认现状","done_when":"用户确认","checkpoints":[p.id for p in DEFAULT_CARD.checkpoints]},
        {"id":"greeting","goal":"已提交","done_when":"确认已提交"},
        {"id":"pending","goal":"未提交","done_when":"知道如何办理","requires":["call_start"]},
    ]
    doc["graph"]["default_leaf"] = "pending"
    doc["basic_response"]["opening"] = "您好，我是工单客服。现在方便吗？"
    file = PhoneFile.model_validate(doc)
    root, opening, *business = file.graph.nodes
    assert root.id != "call_start" and opening.id != "greeting"
    assert [n.id for n in file.graph.nodes if not n.requires] == [root.id]
    assert opening.requires == [root.id] and opening.action == file.basic_response.opening
    assert [n.requires for n in business] == [[opening.id], [opening.id], ["call_start"]]
    flow = Flow(file)
    for leaf in file.graph.leaves():
        assert flow.path(leaf)[:2] == [root.id, opening.id]
    assert flow.context()["current"] == root.id
    assert PhoneFile.model_validate_json(file.model_dump_json()) == file
    doc = file.model_dump()
    doc["basic_response"]["opening"] = "您好，我是新的联系人。"
    assert PhoneFile.model_validate(doc).graph.nodes[1].action == doc["basic_response"]["opening"]


def test_interrupted_greeting_releases_business_path_without_fabricated_delivery():
    flow = Flow(compile_phone(DEFAULT_CARD))
    flow.opening_status = "playing"
    user = "我已经提交了。"
    flow.accept(user)
    flow.apply(Route(leaf="goal_all", completed=[Credit(id="greeting",quote=user)],
                     reopen=[Credit(id="call_start",quote=user)]), user)
    state = flow.context()
    assert state["completed"] == ["call_start"] and state["skipped"] == ["greeting"]
    assert state["current"] == "purpose"
