"""Small real-service acceptance run; writes inspectable results, never keys."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import Brain
from app.models import DEFAULT_CARD, TaskCard, Checkpoint
from app.providers import Cloud, Settings, wav_bytes
from app.session import Session


async def run_case(cloud, name, inputs, card=DEFAULT_CARD):
    events = []
    async def send(event):
        if isinstance(event, dict): events.append(event)
    session = Session(card, cloud, Brain(cloud), send, voice=False, predictions=False)
    output = {"name": name, "turns": []}
    try:
        await session.start()
        await asyncio.wait_for(session.response, 30)
        for text in inputs:
            offset = len(events)
            await session.user_text(text)
            await asyncio.wait_for(session.response, 40)
            result = {"user": text,
                      "assistant": [e["text"] for e in events[offset:] if e["type"] == "utterance"],
                      "state": session.memory.view(),
                      "first_text_ms": session.turn_metrics.get("first_text_ms"),
                      "errors": [e for e in events[offset:] if e["type"] == "error"]}
            output["turns"].append(result)
            print(json.dumps({"case": name, **result}, ensure_ascii=False), flush=True)
    finally:
        await session.close()
    return output


async def main():
    directory = ROOT / "runtime" / "validation"
    directory.mkdir(parents=True, exist_ok=True)
    cloud = Cloud(Settings.load())
    try:
        results = []
        results.append(await run_case(cloud, "normal_and_question", [
            "方便，请直接告诉我要补什么。",
            "我还没明白，截图具体去哪里找？",
            "明白了，我去补商标状态截图，再从原来的入口重新提交。",
        ]))
        results.append(await run_case(cloud, "compound_and_refusal", [
            "我负责这件事，但是现在在开会，不方便说话。",
            "我不想继续这个话题，请结束。",
        ]))
        results.append(await run_case(cloud, "correction_and_unknown", [
            "我只有半分钟，直接说缺什么和怎么处理。",
            "不对，我刚才说错了，我还不清楚需要什么材料。",
            "你帮我在系统里把材料提交一下。",
        ]))
        book_card = TaskCard(title="下一本读什么", goal="根据对方的阅读偏好，在两本候选中推荐一本并说明理由，确认选择。",
            facts="只有两个候选：《海底两万里》，科幻冒险；《福尔摩斯探案》，推理故事。没有购买或借阅功能。",
            checkpoints=[Checkpoint(id="recommendation", description="根据对方偏好推荐一本候选书，并说明理由")],
            opening="这次想读推理故事，还是科幻冒险？")
        results.append(await run_case(cloud, "custom_task", [
            "我想读推理，不要科幻。",
            "明白了，就选福尔摩斯探案。",
        ], card=book_card))
        (directory / "live-text.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        assert all(not turn["errors"] and turn["assistant"] for case in results for turn in case["turns"])
        assert results[0]["turns"][1]["state"]["status"] == "active"
        assert results[0]["turns"][2]["state"]["status"] == "complete"
        assert results[1]["turns"][0]["state"]["status"] == "paused"
        assert results[1]["turns"][1]["state"]["status"] == "declined"
        assert results[2]["turns"][1]["state"]["status"] == "active"
        assert "福尔摩斯" in "".join(results[3]["turns"][0]["assistant"])
        assert results[3]["turns"][1]["state"]["status"] == "complete"
        # Fresh synthetic fixtures exercise browser capture, real ASR and barge-in.
        for name, text in [("user-question", "方便，请直接告诉我要补什么。"),
                           ("user-interrupt", "等等，我还没听清，请再说一次。")]:
            pcm = b"".join([chunk async for chunk in cloud.speech(text)])
            (directory / f"{name}.pcm").write_bytes(pcm)
            (directory / f"{name}.wav").write_bytes(wav_bytes(pcm, 24000))
        print("Live text checks passed; fresh synthetic voice fixtures are ready.", flush=True)
    finally:
        await cloud.close()


if __name__ == "__main__":
    asyncio.run(main())
