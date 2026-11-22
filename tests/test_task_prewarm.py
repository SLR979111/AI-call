from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import task_prewarm
from app.models import DEFAULT_CARD, TaskCard
from app.providers import Settings
from test_core import Cloud, TestBrain
from app.task_prewarm import ADVERSARIAL_TURNS, OFFICIAL_TURNS, preset_dialogues

ROOT = Path(__file__).resolve().parents[1]


def test_preset_dialogues_cover_card_official_and_adversarial():
    card = TaskCard.model_validate_json(
        (ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8")
    )
    dialogues = dict(preset_dialogues(card))
    assert {"official", "official_status", "adversarial", "adversarial_2",
            "short", "english"} <= set(dialogues)
    assert dialogues["official"] == list(OFFICIAL_TURNS)
    assert dialogues["adversarial"] == list(ADVERSARIAL_TURNS)
    assert any(name.startswith("card_") for name in dialogues)
    assert any(turns for name, turns in dialogues.items() if name.startswith("card_"))
    assert len(dialogues) >= 8

async def test_synthetic_player_finishes_long_opening_and_followup(monkeypatch, tmp_path):
    class LongVoice(Cloud):
        settings = Settings(api_key="")

        async def speech(self, text):
            for _ in range(20):
                yield bytes(1920)

    def brain(*args, forecast_branches):
        assert forecast_branches == LongVoice.settings.forecast_branches
        instance = TestBrain()
        instance.forecast_release.set()
        return instance

    monkeypatch.setattr(task_prewarm, "Brain", brain)
    log = tmp_path / "warm.jsonl"
    group = task_prewarm.PrewarmGroup(DEFAULT_CARD, log)
    await asyncio.wait_for(task_prewarm._run_one(
        group, LongVoice(), None, "case", ["具体缺什么？"]), timeout=2)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["epoch"] for e in events if e["event"] == "reply_done"] == [1, 2]
    assert events[-1]["event"] == "scenario_done"
    assert events[-1]["turns"] == 1
    assert events[-1]["audio_bytes"] >= 2 * 38400

async def test_full_round_waits_for_candidate_audio_and_stays_ready(monkeypatch, tmp_path):
    class Voice(Cloud):
        settings = Settings(api_key="")
        async def warm(self, text):
            warming.set()
            await release.wait()
            return await self.prefetch(text)

    def brain(*args, **kwargs):
        instance = TestBrain()
        instance.forecast_release.set()
        return instance

    warming, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    monkeypatch.delenv("PREDICTION_AUDIO_LIMIT", raising=False)
    monkeypatch.setattr(task_prewarm, "Brain", brain)
    card = TaskCard.model_validate_json((ROOT / "tasks/jd-qualification.json").read_text(encoding="utf-8"))
    owner = task_prewarm.TaskPrewarmer()
    args = (card, Voice(), None, tmp_path)
    request = asyncio.create_task(owner.prepare(*args, wait=True))
    await asyncio.wait_for(warming.wait(), 5)
    assert not request.done()
    release.set()
    assert (await asyncio.wait_for(request, 12))["status"] == "ready"
    assert not (await owner.prepare(*args))["started"]
    log, = (tmp_path / "task-prewarm").glob("*.jsonl")
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    finished = [event["scenario"] for event in events if event["event"] == "scenario_done"]
    assert sorted(finished) == sorted(dict(preset_dialogues(card)))
    assert sum(event["event"] == "round_start" for event in events) == 1
    await owner.close()


async def test_one_round_is_shared_while_running_and_after_completion(monkeypatch, tmp_path):
    owner = task_prewarm.TaskPrewarmer()
    entered = asyncio.Event()
    release = asyncio.Event()
    runs = []
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    monkeypatch.setenv("TASK_PREWARM_MAX_ROUNDS", "0")  # older installations
    monkeypatch.setattr(task_prewarm, "preset_dialogues", lambda card: [("a", ["1"]), ("b", ["2"])])

    async def scenario(group, cloud, forecasts, name, turns):
        runs.append(name)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(task_prewarm, "_run_one", scenario)

    def prepare(card=DEFAULT_CARD, **kwargs):
        return owner.prepare(card, None, None, tmp_path, **kwargs)

    assert (await prepare())["status"] == "preparing"
    await entered.wait()
    abandoned = asyncio.create_task(prepare(wait=True))
    await asyncio.sleep(0)
    abandoned.cancel()
    await asyncio.gather(abandoned, return_exceptions=True)
    waiting = asyncio.create_task(prepare(wait=True))
    await asyncio.sleep(0)
    assert not waiting.done()
    release.set()
    assert (await waiting)["status"] == "ready"
    assert not (await prepare(retry=True))["started"]
    assert sorted(runs) == ["a", "b"]
    changed = DEFAULT_CARD.model_copy(update={"goal": "另一个目标"})
    assert (await prepare(changed, wait=True))["status"] == "ready"
    assert sorted(runs) == ["a", "a", "b", "b"]
    await owner.close()


async def test_failed_round_only_retries_on_request(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    owner = task_prewarm.TaskPrewarmer()
    scenario = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(task_prewarm, "_run_one", scenario)
    monkeypatch.setattr(task_prewarm, "preset_dialogues", lambda card: [("one", ["1"])])
    args = (DEFAULT_CARD, None, None, tmp_path)
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "0")
    assert (await owner.prepare(*args, wait=True))["status"] == "disabled"
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    assert (await owner.prepare(*args, wait=True))["status"] == "failed"
    assert not (await owner.prepare(*args))["started"]
    assert (await owner.prepare(*args, wait=True, retry=True))["status"] == "ready"
    assert scenario.await_count == 2


async def test_shutdown_drains_inflight_scenarios(monkeypatch, tmp_path):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def scenario(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    owner = task_prewarm.TaskPrewarmer()
    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    monkeypatch.setattr(task_prewarm, "_run_one", scenario)
    await owner.prepare(DEFAULT_CARD, None, None, tmp_path)
    await entered.wait()
    await owner.close()
    assert stopped.is_set()
    assert not owner.active


def test_cloud_socket_waits_for_round_started_by_http(monkeypatch, tmp_path):
    import threading
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from app import main
    entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    async def simulate(*args):
        calls.append(1)
        entered.set()
        return await asyncio.to_thread(release.wait, 5)

    prepare = main.prepare_cloud_task
    async def observed(card, **options):
        if options["wait"]:
            waiting.set()
        return await prepare(card, **options)

    async def start(session):
        await session.send({"type": "ready"})

    monkeypatch.setenv("TASK_PREWARM_ON_LOAD", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test")
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(task_prewarm, "_run_round", simulate)
    monkeypatch.setattr(main, "prepare_cloud_task", observed)
    monkeypatch.setattr(main.Cloud, "prewarm", AsyncMock())
    monkeypatch.setattr(main.Session, "start", start)
    monkeypatch.setattr(main.FirstPassASR, "load", lambda: None)
    monkeypatch.setattr(main, "load_from_env", lambda: None)
    with TestClient(main.app) as client:
        assert client.post("/api/warm", json={}).json()["status"] == "preparing"
        assert entered.wait(2)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "mode": "cloud", "voice": False})
            try:
                assert waiting.wait(2)
                assert client.get("/healthz").json()["sessions"] == 0
            finally:
                release.set()
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "stop"})
        assert client.post("/api/warm", json={"wait": True}).json() == {"started": False, "status": "ready"}
        assert calls == [1]
