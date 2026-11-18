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
        "用户表示自己不是负责人、只是代接"], None),
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


async def test_prediction_owns_audio_and_reuses_without_tts():
    from app.models import DEFAULT_CARD
    from app.session import Session, Unit

    pcm = b'\x01\x00' * 100

    class Cloud:
        async def prefetch(self, text):
            return pcm

        async def speech(self, text):
            raise AssertionError('prepared audio must not call TTS')
            yield b''

    class Forecast:
        async def predict(self, snapshot):
            return [candidate('c1', '用户确认')]

    async def send(payload):
        pass

    session = Session(DEFAULT_CARD, Cloud(), Forecast(), send, voice=True)
    await session._predict(0, {})
    reply = session.candidates[0].reply
    retained = session.prediction_audio[reply.text]
    unit = Unit(1, reply, {})
    await session._synthesize(unit, retained)
    assert await unit.queue.get() == pcm
    assert await unit.queue.get() is None
    assert unit.total == 100
    assert unit.source_done
    await session.close()


async def test_duplicate_forecasts_share_audio_preparation():
    from app.models import DEFAULT_CARD
    from app.session import Session

    calls = []
    class Cloud:
        async def prefetch(self, text):
            calls.append(text)
            await asyncio.sleep(0)
            return b'\x01\x00' * 960

    class Forecast:
        async def predict(self, snapshot):
            return [candidate('a', '用户确认'), candidate('b', '用户表示明白')]

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, Cloud(), Forecast(), send, voice=True)
    try:
        await session._predict(0, {})
        assert len(calls) == 1
        assert len(session.candidates) == 2
        assert len(session.prediction_audio) == 1
    finally:
        await session.close()


async def test_cancel_forecast_cancels_shared_audio_preparation():
    from app.models import DEFAULT_CARD
    from app.session import Session

    started, cancelled = asyncio.Event(), asyncio.Event()

    class Cloud:
        async def prefetch(self, text):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Forecast:
        async def predict(self, snapshot):
            return [candidate('a', '用户确认'), candidate('b', '用户表示明白')]

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, Cloud(), Forecast(), send, voice=True)
    task = session.spawn(session._predict(0, {}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert cancelled.is_set()
        assert not session.candidates
        assert not session.prediction_audio
    finally:
        await session.close()


async def test_provider_prefetch_survives_shared_cache_eviction():
    import httpx
    from app.providers import Cloud, Settings
    from app.models import DEFAULT_CARD
    from app.session import Session, Unit

    pcm = b'\x01\x00' * 960
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=pcm,
                              headers={'content-type': 'audio/pcm'})

    class Forecast:
        async def predict(self, snapshot):
            return [candidate('c1', '用户确认')]

    async def send(event):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = Cloud(Settings(api_key='test'), client)
        session = Session(DEFAULT_CARD, cloud, Forecast(), send, voice=True)
        try:
            await session._predict(0, {})
            reply = session.candidates[0].reply
            assert cloud.cached(reply.text) == pcm
            for index in range(49):
                cloud.remember(f'other-{index}', pcm)
            assert cloud.cached(reply.text) is None
            unit = Unit(1, reply, {})
            await session._synthesize(unit, session.prediction_audio[reply.text])
            assert await unit.queue.get() == pcm
            assert await unit.queue.get() is None
            assert len(requests) == 1
        finally:
            await session.close()


async def test_reuse_sends_prepared_audio_before_model_update():
    from app.models import DEFAULT_CARD, Update
    from app.session import Session
    from app.brain import Brain

    pcm = b'\x01\x00' * 960
    update_release = asyncio.Event()
    packet_sent = asyncio.Event()
    events = []

    class Cloud:
        async def prefetch(self, text):
            return pcm

        async def speech(self, text):
            raise AssertionError('reuse must bypass provider TTS')
            yield b''

    class Forecast:
        snapshot = staticmethod(Brain.snapshot)

        async def predict(self, snapshot):
            return [candidate('c1', '用户确认')]

        async def respond(self, memory, candidates):
            assert candidates
            yield 'reuse', candidates[0].reply
            await update_release.wait()
            yield 'update', Update()

    async def send(event):
        events.append(event)
        if isinstance(event, bytes):
            packet_sent.set()

    session = Session(DEFAULT_CARD, Cloud(), Forecast(), send, voice=True)
    await session._predict(0, {})
    session.reply_completed = True
    await session.user_text('是的')
    try:
        await asyncio.wait_for(packet_sent.wait(), 1)
        assert not update_release.is_set()
        assert session.turn_metrics['reused'] is True
        assert session.turn_metrics['prediction_audio_hit'] is True
        assert next(e for e in events if isinstance(e, bytes))[12:] == pcm
    finally:
        await session.close()
    assert not session.jobs


async def test_slow_answer_uses_bridge_without_claiming_cache_hit():
    from app.models import DEFAULT_CARD, Update
    from app.session import Session

    release = asyncio.Event()
    sent = asyncio.Event()

    class Cloud:
        async def speech(self, text):
            raise AssertionError('bridge must use prepared PCM')
            yield b''

    class SlowBrain:
        async def respond(self, memory, candidates):
            await release.wait()
            yield 'say', Say(text='正式回答。')
            yield 'update', Update()

    async def send(event):
        if isinstance(event, bytes):
            sent.set()

    session = Session(DEFAULT_CARD, Cloud(), SlowBrain(), send, voice=True)
    session.bridge_audio = b'\x01\x00' * 960
    session.bridge_delay = .01
    await session.user_text('怎么处理？')
    try:
        await asyncio.wait_for(sent.wait(), 1)
        assert session.turn_metrics['bridge_used']
        assert not session.turn_metrics['reused']
        assert 'first_text_ms' not in session.turn_metrics
        assert 'prediction_audio_hit' not in session.turn_metrics
        assert not release.is_set()
    finally:
        await session.close()
    assert not session.jobs


@pytest.mark.parametrize('user,condition', [
    ('能看清。', '用户表示截图能看清或已重新截好，且没有说明已重新提交'),
    ('已经提交了。', '用户明确说已经点了提交，且没有提出新的材料问题'),
    ('清楚。', '用户说清楚或已知道授权问题，但没有提到其他问题'),
    ('好的。', '用户表示明白或同意继续听，且没有提出新的问题'),
    ('好的。', '用户说“好的”，只是承接上一句，且没有提出新的问题'),
])
def test_parallel_equivalent_branches_do_not_require_model(user, condition):
    engine = ContextAwareDecisionEngine()
    branches = [candidate('a', condition), candidate('b', condition)]
    for ordered in (branches, list(reversed(branches))):
        decision, selected = engine.should_reuse(user, ordered, '您清楚吗？')
        assert decision == 'reuse'
        assert selected.id == 'a'
    assert engine.should_reuse(user + '但是还有问题', branches)[0] == 'llm'


async def test_long_bridge_is_not_used_to_delay_answer():
    from app.models import DEFAULT_CARD
    from app.session import Session

    class Cloud:
        async def prefetch(self, text):
            return b'\x00\x00' * 53760  # 2.24 seconds from the live trace

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, Cloud(), None, send, voice=True)
    await session._warm_bridge()
    assert session.bridge_audio is None
    await session.close()


async def test_playback_metrics_separate_bridge_from_answer():
    from app.models import DEFAULT_CARD
    from app.session import Session, Unit

    async def send(event):
        pass

    session = Session(DEFAULT_CARD, None, None, send, voice=True)
    session.turn_metrics = {'last_voice_client_ms': 1000}
    session.units[1] = Unit(1, Say(text='嗯。'), {'bridge': True})
    session.units[2] = Unit(2, Say(text='正式回答。'), {})
    await session.playback_started(0, 1400, unit_id=1)
    assert session.turn_metrics['voice_to_playback_ms'] == 400
    assert 'voice_to_answer_ms' not in session.turn_metrics
    await session.playback_started(0, 1900, unit_id=2)
    assert session.turn_metrics['voice_to_answer_ms'] == 900
    assert session.turn_metrics['voice_to_playback_ms'] == 400
    await session.playback_started(0, 2000, unit_id=2)
    assert session.turn_metrics['voice_to_answer_ms'] == 900
    await session.close()


async def test_forecast_branches_run_concurrently():
    from app.brain import Brain
    from app.models import DEFAULT_CARD, Memory

    class Cloud:
        def __init__(self):
            self.calls = 0
            self.all_started = asyncio.Event()

        async def chat(self, messages, **kwargs):
            self.calls += 1
            payload = json.loads(messages[-1]['content'])
            if self.calls == 1:
                assert payload['forecast_user_inputs'] == ['好的。', '是的。']
                assert '不能推断填错字' in messages[0]['content']
            else:
                assert 'forecast_user_inputs' not in payload
            if self.calls == 3:
                self.all_started.set()
            await asyncio.wait_for(self.all_started.wait(), 1)
            yield json.dumps({'candidates': [candidate('c1', '用户确认').model_dump()]})

    cloud = Cloud()
    result = await Brain(cloud, forecast_branches=3).predict(Brain.snapshot(Memory(DEFAULT_CARD)))
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
    batches = Brain(cloud, forecast_branches=3).predict_batches(Brain.snapshot(Memory(DEFAULT_CARD)))
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
