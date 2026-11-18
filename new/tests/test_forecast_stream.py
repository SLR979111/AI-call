import asyncio
import json

from app.brain import Brain
from app.forecast import OpeningForecastCache, likely_short_answers, match_example, stream_candidates
from app.models import DEFAULT_CARD, Candidate, Memory, Say
from app.session import Session
from test_core import until


def branch(text="请上传商标状态截图。", **kwargs):
    return Candidate(id="p1", when="用户表示愿意继续听", reply=Say(text=text), **kwargs)


async def test_first_candidate_can_be_prepared_while_second_is_still_generating():
    release = asyncio.Event()
    closed = asyncio.Event()
    item = branch('请看文件中的“]”和“}”。')

    async def chunks():
        try:
            yield '```json\n{"candi'
            yield 'dates":[' + item.model_dump_json() + ','
            await release.wait()
            yield branch().model_dump_json() + ']}\n```'
        finally:
            closed.set()

    stream = stream_candidates(chunks(), 2)
    assert await asyncio.wait_for(anext(stream), 1) == item
    assert not release.is_set()
    await stream.aclose()
    assert closed.is_set()


async def test_first_prediction_audio_is_ready_before_the_forecast_finishes():
    release = asyncio.Event()
    async def send(event):
        pass

    class Cloud:
        async def chat(self, messages, **kwargs):
            yield '{"candidates":[' + branch().model_dump_json()
            await release.wait()
            yield ']}'

        async def prefetch(self, text):
            return bytes(1920)

    cloud = Cloud()
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=True)
    try:
        task = session.spawn(session._predict(0, Brain.snapshot(session.memory)))
        await until(lambda: bool(session.candidates))
        assert session.candidates[0].reply.text == branch().reply.text
        assert not task.done()
        assert not session.reply_completed
        release.set()
        await task
    finally:
        await session.close()


def test_exact_spoken_examples_preserve_negation_questions_and_ambiguity():
    yes = branch(examples=["是的。", "好的。"])
    previous = "您是负责人吗？"
    assert match_example("呃，是的呢。", [yes], previous) is yes
    assert match_example("哦，好的呀。", [yes], previous) is yes
    for user in ("不是。", "是的？", "好的，但是缺的是哪一份？", "嗯，好的，请别挂。"):
        assert match_example(user, [yes], previous) is None
    assert match_example("是的", [yes], "您是只保存了，还是重新提交了？") is None
    other = branch("那请您点提交。", examples=["好的"])
    assert match_example("好呀。", [yes, other], previous) is None
    bad = yes.model_copy(update={"when": "用户确认已经提交"})
    assert match_example("是的。", [bad], previous) is None


def test_forecast_choices_follow_actual_question():
    snapshot = {"pending_reply": [{"text": "您是只保存了，还是已经提交了？"}]}
    assert likely_short_answers(snapshot) == ["只保存了。", "已经提交了。"]


async def test_model_spelling_out_a_cached_line_keeps_current_turn_metadata():
    class Cloud:
        async def chat(self, messages, **kwargs):
            yield '{"type":"say","text":"请上传商标状态截图。","covers":["reason"]}\n'
            yield '{"type":"update"}\n'

    memory = Memory(DEFAULT_CARD, history=[{"role": "user", "text": "具体缺哪份材料？"}])
    events = [event async for event in Brain(Cloud()).respond(memory, [branch()])]
    assert events[0][0] == "reuse"
    assert events[0][1].covers == ["reason"]


async def test_only_identical_openings_share_forecasts_across_sessions():
    class Cloud:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            yield json.dumps({"candidates": [branch().model_dump()]}, ensure_ascii=False)

    cloud, cache = Cloud(), OpeningForecastCache()
    snapshot = {**Brain.snapshot(Memory(DEFAULT_CARD)), "opening": True}
    baseline = await Brain(cloud, cache).predict(snapshot)
    first_calls = cloud.calls
    assert baseline and first_calls
    second = await Brain(cloud, cache).predict(snapshot)
    assert second == baseline and cloud.calls == first_calls
    second[0].reply.text = "变更"
    assert (await Brain(cloud, cache).predict(snapshot)) == baseline
    await Brain(cloud, cache).predict({**snapshot, "opening": False})
    assert cloud.calls == first_calls * 2
    await Brain(cloud, cache).predict({**snapshot, "task": {**snapshot["task"], "facts": "改用新的提交入口"}})
    assert cloud.calls == first_calls * 3


def test_interrupted_warmup_keeps_validated_items_and_can_be_completed():
    cache = OpeningForecastCache()
    first = branch()
    second = branch("点击提交。").model_copy(update={"id": "p2"})
    cache.store("task", [first], complete=False)
    assert cache.get("task") == ([first], False)
    cache.store("task", [first, second], complete=True)
    cache.store("task", [first], complete=False)
    assert cache.get("task") == ([first, second], True)
