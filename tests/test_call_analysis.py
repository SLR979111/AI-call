import asyncio
import json
from contextlib import closing, suppress

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.brain import Brain
from app.call_analysis import ApplyReport, CallAnalysis, ExperienceScope, Scope, SetTrigger, Trigger, router
from app.models import DEFAULT_CARD, CallPlaybook
from app.session import Session


PLAYBOOK = {"opening": "您好，您方便聊资料补正吗？",
            "flow": ["先回答来电者提出的具体问题，再补充材料要求。"],
            "replies": [{"when": "对方问先处理哪项", "text": "您先看申请页面的补正提示。"}]}
OTHER_CARD = DEFAULT_CARD.model_copy(update={"title": "核对开放时间"})
RULES = [
    {"kind": "flow", "when": "来电者已说明处理进度", "guidance": "从目前进度对应的节点开始。"},
    {"kind": "reply", "when": "对方追问先处理哪项", "guidance": "先说第一步，再解释原因。"},
]


def write_call(runtime, name, order, *, mode="cloud", voice=True, outcome="active",
               closed=True, text="我该先处理哪项？", card=DEFAULT_CARD):
    logs = runtime / "sessions"
    logs.mkdir(exist_ok=True)
    events = [{"event": "started", "mode": mode, "voice": voice, "task": card.model_dump()},
              {"event": "prediction_ready", "text": "预测没有播放"}]
    if text:
        events.append({"event": "user", "text": text})
    events.append({"event": "delivered", "text": "请看本次申请的补正提示。"})
    if closed:
        events.append({"event": "closed", "at": order, "status": outcome,
                       "cutoff": [{"text": "您先看", "remaining_text": "没有播放的续句"}]})
    (logs / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events), encoding="utf-8")


class ReviewProvider:
    def __init__(self):
        self.contexts = []
        self.bad_report = False
        self.checked_local = False

    def result(self, context):
        self.contexts.append(context)
        if "transcript" in context:
            return {"findings": [{"turn": 1, "observation": "对方追问先处理哪项"}]}
        return {"summary": "不同工单都有先后顺序的问题",
                "experiences": [{**rule, "calls": [1, 9 if self.bad_report else 2]} for rule in RULES]}

    async def chat(self, messages, *, max_tokens, use_cache):
        assert not use_cache
        yield json.dumps(self.result(json.loads(messages[-1]["content"])), ensure_ascii=False)

    async def ready(self, *, voice):
        assert not voice
        self.checked_local = True

    async def structured(self, schema, instruction, context, tokens, *, context_size):
        assert self.checked_local
        return schema.model_validate(self.result(context))


def test_analysis_reports_retry_evidence_and_version_rollback(tmp_path):
    cloud = ReviewProvider()
    app = FastAPI()
    app.include_router(router)
    app.state.call_analysis = CallAnalysis(tmp_path, {"cloud": cloud})
    payload = {"mode": "cloud"}
    for i in range(3):
        write_call(tmp_path, f"cloud-{i}", i, voice=i < 2, card=OTHER_CARD if i == 1 else DEFAULT_CARD)
    write_call(tmp_path, "local", 10, mode="local")
    write_call(tmp_path, "old", 11, mode=None, outcome=None)
    write_call(tmp_path, "open", 12, closed=False)
    write_call(tmp_path, "empty", 13, text="")
    with TestClient(app) as client:
        def post(path, **fields):
            return client.post("/api/call-analysis/" + path, json={**payload, **fields})

        assert post("run").status_code == 409
        assert post("trigger", trigger={"batch_size": 2, "min_incomplete": 3}).status_code == 422
        status = post("trigger", trigger={"batch_size": 2, "min_incomplete": 1}).json()
        assert (status["pending_voice"], status["pending_text"], status["reviewed"]) == (2, 1, 0)
        cloud.bad_report = True
        assert post("run").status_code == 409
        assert post("status").json()["pending_voice"] == 2
        cloud.bad_report = False
        status = post("run").json()
        report, = status["reports"]
        assert len(report["experiences"]) == 2 and status["active_version"] is None
        assert {sample["id"] for sample in report["samples"]} == {"cloud-0", "cloud-1"}
        raw = json.dumps(cloud.contexts[0], ensure_ascii=False)
        assert "您先看" in raw
        assert "预测没有播放" not in raw and "没有播放的续句" not in raw

        edited = [RULES[0], {**RULES[1], "guidance": "先直接回答对方问的第一步。"}]
        assert post("apply", report_id=report["id"], rules=edited).json()["active_version"] == report["id"]
        assert post("apply", report_id=report["id"], rules=edited).status_code == 409
        assert post("activate", version=None, expected_version=None).status_code == 409
        restored = CallAnalysis(tmp_path, {})
        assert [rule.model_dump() for rule in restored.library("cloud")[1]] == edited
        assert post("activate", version=None, expected_version=report["id"]).json()["active_version"] is None
        assert restored.library("cloud") == (None, [])
        assert post("activate", version=report["id"], expected_version=None).status_code == 200
        assert restored.library("local") == (None, [])
        assert post("run").status_code == 409
        write_call(tmp_path, "new-1", 20)
        write_call(tmp_path, "new-2", 21, card=OTHER_CARD)
        next_report = post("run").json()["reports"][0]
        assert next_report["base_version"] == report["id"]
        write_call(tmp_path, "new-3", 22)
        write_call(tmp_path, "new-4", 23, card=OTHER_CARD)
        last_report = post("run").json()["reports"][0]
        assert last_report["base_version"] == report["id"]
        added = {"kind": "reply", "when": "要核对现状时", "guidance": "先问一个最能区分当前状态的问题。"}
        assert post("apply", report_id=last_report["id"], rules=[added]).json()["library"] == [*edited, added]
        additional = {"kind": "flow", "when": "已办事项被重复解释", "guidance": "按新的诉求进入后续节点。"}
        result = post("apply", report_id=next_report["id"], rules=[additional])
        assert result.json()["library"] == [*edited, added, additional]


async def test_auto_requires_ready_batch_idle_service_and_selected_provider(tmp_path):
    cloud, local = ReviewProvider(), ReviewProvider()
    analysis = CallAnalysis(tmp_path, {"cloud": cloud, "local": local})
    scope = ExperienceScope(mode="local")
    trigger = Trigger(automatic=True, batch_size=2, min_incomplete=1, include_text=True)
    analysis.set_trigger(SetTrigger(**scope.model_dump(), trigger=trigger))
    write_call(tmp_path, "legacy", 1, mode=None, outcome=None)
    write_call(tmp_path, "cloud", 2, mode="cloud")
    write_call(tmp_path, "complete", 3, mode="local", outcome="complete")
    active = True
    worker = asyncio.create_task(analysis.worker(lambda: active))
    try:
        active = False
        analysis.changed.set()
        assert not (await analysis.status(scope))["ready"]
        active = True
        write_call(tmp_path, "text", 4, mode="local", voice=False, outcome="paused", card=OTHER_CARD)
        analysis.changed.set()
        assert (await analysis.status(scope))["ready"]
        assert not local.contexts
        active = False
        analysis.changed.set()
        async with asyncio.timeout(4):
            while not (status := await analysis.status(scope))["reports"]:
                await asyncio.sleep(0.02)
        report, = status["reports"]
        assert report["automatic"]
        assert {sample["id"] for sample in report["samples"]} == {"legacy", "text"}
        assert status["pending_voice"] == 1 and not status["ready"]
        assert not cloud.contexts
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def test_approved_opening_and_live_prompt_keep_original_task(tmp_path):
    class DialogueProvider:
        async def chat(self, messages):
            context = json.loads(messages[-1]["content"])
            assert context["state"]["flow"]["playbook"] == {k: PLAYBOOK[k] for k in ("flow", "replies")}
            assert context["basic_response"]["opening"] == PLAYBOOK["opening"]
            assert "replies" in messages[0]["content"]
            yield '{"type":"say","text":"您先看申请页面的补正提示。"}\n{"type":"update"}\n'

    class Events:
        def __init__(self):
            self.items = []

        async def __call__(self, event):
            self.items.append(event)

    events = Events()
    cloud = DialogueProvider()
    playbook = CallPlaybook.model_validate(PLAYBOOK)
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), events, voice=False, predictions=False,
                      playbook=playbook, playbook_version=1, mode="cloud", log_dir=tmp_path / "sessions")
    await session.start()
    await session.response
    await session.user_text("先处理哪项？")
    await session.response
    await session.close()
    assert [item["text"] for item in events.items if item["type"] == "utterance"] == [
        PLAYBOOK["opening"], PLAYBOOK["replies"][0]["text"],
    ]
    assert events.items[0]["phone_file"]["basic_response"]["opening"] == DEFAULT_CARD.opening
    assert session.memory.card.opening == PLAYBOOK["opening"]


def test_preexisting_per_order_version_still_applies_in_websocket(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from app import main
    from app.call_history import CallArchive
    from app.phone_file import compile_phone

    archive = CallArchive(tmp_path / "sessions")
    analysis = CallAnalysis(tmp_path, {}, archive=archive)
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)
    monkeypatch.setattr(main, "warmup_shared", AsyncMock())
    monkeypatch.setattr(main, "CallArchive", lambda _: archive)
    monkeypatch.setattr(main, "CallAnalysis", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(main, "Session", lambda *args, **kwargs: Session(
        *args, **{**kwargs, "log_dir": archive.logs}))
    prepare = AsyncMock(return_value={"status": "ready"})
    monkeypatch.setattr(main, "prepare_cloud_task", prepare)
    file = compile_phone(DEFAULT_CARD).model_copy(update={"background": "电话文件中整理后的业务资料。"})
    assert file.task != DEFAULT_CARD
    payload = {"task": DEFAULT_CARD.model_dump(), "mode": "cloud"}
    scope = Scope.model_validate(payload)
    with closing(analysis.connect()) as db, db:
        db.execute("INSERT INTO profiles(scope, task, mode, trigger, active_version) VALUES (?, ?, 'cloud', '{}', 1)",
                   (scope.key, DEFAULT_CARD.model_dump_json()))
        db.execute("INSERT INTO reports(id, scope, created_at, data, applied) VALUES (1, ?, 1, '{}', ?)",
                   (scope.key, json.dumps(PLAYBOOK, ensure_ascii=False)))

    with TestClient(main.app, base_url="http://127.0.0.1") as client:
        def dial():
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "start", "phone_file": file.model_dump(), "analysis_task": payload["task"],
                              "mode": "cloud", "voice": False, "predictions": False})
                while (event := ws.receive_json())["type"] != "utterance":
                    assert event["type"] != "error", event
                    if event["type"] == "ready":
                        assert event["phone_file"]["basic_response"]["opening"] == DEFAULT_CARD.opening
                ws.send_json({"type": "stop"})
                return event["text"]

        assert dial() == PLAYBOOK["opening"]
        assert prepare.await_args.kwargs["opening_override"] == PLAYBOOK["opening"]
        assert client.post("/api/call-analysis/status", json={"mode": "cloud"}).json()["library"] == []
    call, = archive.calls()
    assert call.version == 1 and scope.key.endswith(call.task)


async def test_new_order_planning_uses_only_approved_mode_rules_and_invalidates_cache(tmp_path, monkeypatch):
    from collections import OrderedDict
    from types import SimpleNamespace

    from app import main
    from app.phone_file import compile_phone
    from app.providers import Cloud

    scope = ExperienceScope(mode="cloud")
    review = ReviewProvider()
    analysis = CallAnalysis(tmp_path, {"cloud": review})
    analysis.set_trigger(SetTrigger(mode="cloud", trigger=Trigger(batch_size=2)))
    write_call(tmp_path, "first", 1)
    write_call(tmp_path, "still-first-order", 2)
    assert not (await analysis.status(scope))["ready"]
    write_call(tmp_path, "other", 3, card=OTHER_CARD)
    await analysis.analyze(scope)
    report, = (await analysis.status(scope))["reports"]
    assert {call["id"] for call in report["samples"]} == {"first", "other"}
    third = DEFAULT_CARD.model_copy(update={"title": "解释账单"})
    graph = compile_phone(third).graph.model_dump()

    class Planner:
        plan_phone = Cloud.plan_phone
        calls = []

        async def chat(self, messages, *, max_tokens, use_cache):
            context = json.loads(messages[-1]["content"])
            self.calls.append((messages[0]["content"], context))
            if "graph" in context:
                yield '{"changes":[],"remove_merges":[],"default_leaf":null}'
            else:
                yield json.dumps({"guide": "先解释下一步。", "background": third.facts, "graph": graph})

    planner = Planner()
    state = SimpleNamespace(settings=SimpleNamespace(api_key="configured"), cloud=planner, local=planner,
                            call_analysis=analysis, phone_files=OrderedDict(), phone_jobs={})
    monkeypatch.setattr(main, "app", SimpleNamespace(state=state))
    before = await main.generate_phone_file(third)
    assert "reviewed_experience" not in planner.calls[0][1]

    analysis.apply(ApplyReport(mode="cloud", report_id=report["id"], rules=RULES))
    after = await main.generate_phone_file(third)
    assert after is not before and await main.generate_phone_file(third) is after
    instruction, context = planner.calls[2]
    assert context["reviewed_experience"] == RULES
    assert "前置条件" in instruction and "guide" in instruction
    assert planner.calls[3][1]["reviewed_experience"] == [RULES[0]]
    await main.generate_phone_file(third, "local")
    assert "reviewed_experience" not in planner.calls[4][1]
