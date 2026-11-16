"""Regression cases from recorded sessions and ambiguous forecast branches."""

import asyncio
import json

import pytest

from app.context_aware_decision import ContextAwareDecisionEngine
from app.models import Candidate, Say


def candidate(id: str, when: str) -> Candidate:
    return Candidate(id=id, when=when, reply=Say(text="请按页面提示操作。"))


@pytest.mark.parametrize("user,previous,conditions,expected", [
    ("是的。", "您是负责这块的吗？", [
        "用户自报是负责人，并直接问资料为什么被退回，且没有补交",
        "用户表示自己不是负责人、只是代接"], "c1"),
    ("是的，是的。", "您是负责这块的吗？", [
        "用户确认自己是负责人，但没有提出具体问题", "用户表示自己不是负责人"], "c1"),
    ("好的。", "要能看清商标名称、注册号和当前状态。", [
        "用户表示知道了或会去重新截图，但没有说明是否已经补交或重新提交，也没有提出新的问题",
        "用户说已经补交或已经重新提交了截图，但没有说明是只保存了还是点了提交"], "c1"),
    ("明白了。", "这两处先说明。", [
        "用户表示明白授权问题，但没有说明已提交", "用户说截图已重新提交"], "c1"),
    ("保存了。", "您是点提交了，还是只保存了？", [
        "用户回答已经点了提交", "用户回答只是保存了、还没点提交"], "c2"),
    ("已经重新提交了。", "您是只保存了，还是已经点提交了？", [
        "用户回答只是保存了、还没点提交", "用户回答已经重新提交"], "c2"),
    ("直接给我的。", "是经销商转授，还是品牌方直接给的？", [
        "用户回答授权是经销商转授", "用户回答授权是品牌方直接给的"], "c2"),
    ("都对得上。", "名称和有效期都对得上吧？", [
        "用户回答名称和有效期都对得上", "用户说名称对不上"], "c1"),
    ("开好了。", "请把直接授权文件准备好。您这份文件已经开好了吗？", [
        "用户表示直接授权文件已开好", "用户说已经重新提交了"], "c1"),
    ("是的。", "您是负责人吗？", [
        "用户说自己不是负责人", "用户确认自己是负责人"], "c2"),
    ("不是。", "您是负责人吗？", [
        "用户确认自己是负责人", "用户说自己不是负责人"], None),
    ("是的，但是截图还没准备。", "您是负责人吗？", [
        "用户确认自己是负责人", "用户不是负责人"], None),
    ("好的，谢谢，多久能出结果？", "您现在方便吗？", [
        "用户表示现在方便", "用户表示现在不方便"], None),
    ("青禾品牌不是我的。", "那份材料还有吗？", [
        "用户提到青禾品牌且没有其他问题"], None),
    ("都行。", "您要第一项还是第二项？", [
        "用户选择第一项", "用户选择第二项"], None),
    ("第一项。", "您要第一项还是第二项？", [
        "用户选择第一项", "用户选择第二项"], None),
    ("准备了。", "您手上有截图吗？", [
        "用户说授权文件已经准备好", "用户说截图已经准备好"], None),
])
def test_forecast_match(user, previous, conditions, expected):
    result, selected = ContextAwareDecisionEngine().should_reuse(
        user, [candidate(f"c{i}", when) for i, when in enumerate(conditions, 1)], previous
    )
    assert result == ("reuse" if expected else "llm")
    assert (selected.id if selected else None) == expected


async def test_forecast_branches_run_concurrently():
    from app.brain import Brain
    from app.models import DEFAULT_CARD, Memory

    class Cloud:
        def __init__(self):
            self.calls = 0
            self.all_started = asyncio.Event()

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 3:
                self.all_started.set()
            await asyncio.wait_for(self.all_started.wait(), 1)
            yield json.dumps({'candidates': [candidate('c1', '用户确认').model_dump()]})

    cloud = Cloud()
    result = await Brain(cloud).predict(Brain.snapshot(Memory(DEFAULT_CARD)))
    assert cloud.calls == 3
    assert len(result) == 3
    assert len({c.id for c in result}) == 3


async def test_fast_forecast_is_available_before_slow_branches():
    from app.brain import Brain
    from app.models import DEFAULT_CARD, Memory

    class Cloud:
        def __init__(self):
            self.calls = 0
            self.cancelled = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls > 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled += 1
            yield json.dumps({'candidates': [candidate('c1', '用户确认').model_dump()]})

    cloud = Cloud()
    batches = Brain(cloud).predict_batches(Brain.snapshot(Memory(DEFAULT_CARD)))
    first = await asyncio.wait_for(anext(batches), 1)
    assert len(first) == 1
    await batches.aclose()
    assert cloud.cancelled == 2


def test_replay_never_uses_future_predictions(tmp_path):
    from replay_cache import replay

    events = [
        dict(event='user', epoch=2, text='是的'),
        dict(event='prediction_ready', epoch=2, candidates=[candidate('c1', '用户确认自己是负责人').model_dump()]),
        dict(event='delivered', epoch=2, text='您是负责人吗？'),
        dict(event='reply_finished', epoch=2, reused=False),
        dict(event='user', epoch=3, text='是的'),
        dict(event='interrupted', epoch=4),
        dict(event='user', epoch=5, text='是的'),
    ]
    (tmp_path / 'session.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    rows = replay(tmp_path)['rows']
    assert [r['available'] for r in rows] == [0, 1, 0]
    assert [r['hit'] for r in rows] == [False, True, False]
