"""Inspect one grounded unknown answer and one ready-cache selection."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.brain import Brain
from app.models import DEFAULT_CARD
from app.providers import Cloud, Settings
from app.session import Session


async def main():
    cloud = Cloud(Settings.load())
    events = []
    async def send(event):
        if isinstance(event, dict): events.append(event)
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=False, predictions=True)
    try:
        await session.start()
        await session.response
        await asyncio.wait_for(session.forecast, 20)
        candidates = [item.model_dump() for item in session.candidates]
        await session.user_text("方便，请直接告诉我要补什么。")
        await session.response
        reused = session.turn_metrics["reused"]
        await session.user_text("截图具体从哪里获取？我没有找到入口。")
        await session.response
        busy_events = []
        async def busy_send(event):
            if isinstance(event, dict): busy_events.append(event)
        busy = Session(DEFAULT_CARD, cloud, Brain(cloud), busy_send, voice=False, predictions=True)
        try:
            await busy.start()
            await busy.response
            await asyncio.wait_for(busy.forecast, 20)
            offset = len(busy_events)
            await busy.user_text("现在很忙，不方便说话。")
            await busy.response
            busy_reply = "".join(e["text"] for e in busy_events[offset:] if e["type"] == "utterance")
            assert busy.memory.status == "paused"
            assert "截图" not in busy_reply and "重新提交" not in busy_reply
        finally:
            await busy.close()
        result = {"purpose":"Ready-cache path check; forecast was deliberately awaited, not a latency benchmark.",
                  "candidates":candidates,"reused":reused,"events":events,"busy_events":busy_events}
        (ROOT/"runtime"/"validation"/"targeted.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False),flush=True)
        assert not [event for event in events if event["type"]=="error"]
        assert reused, "Appropriate ready candidate was not reused; inspect the recorded candidates."
    finally:
        await session.close()
        await cloud.close()


asyncio.run(main())
