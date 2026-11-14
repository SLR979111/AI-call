import json

import httpx
import pytest
from pydantic import ValidationError

from app.brain import Brain
from app.models import DEFAULT_CARD, Memory, Say
from app.providers import Cloud, Settings
from app.session import Session


@pytest.mark.parametrize("text", [
    "<think>先判断用户意图</think>您还缺一份授权。",
    "分析：用户想补交。您还缺一份授权。",
    "【内部分析】先判断是不是负责人。",
    "用户已经表示方便，我需要追问材料。",
    "内部判断不方便念，我直接说结论。",
    "您还缺一份授权。接下来我需要询问授权关系。",
])
async def test_internal_narration_never_reaches_ui_history_or_tts(text):
    class LeakyCloud:
        async def chat(self, messages, **kwargs):
            yield json.dumps({"type": "say", "text": text}, ensure_ascii=False) + "\n"
            yield '{"type":"update"}\n'

        async def speech(self, text):
            pytest.fail("Rejected speech reached TTS")
            yield b""

    events = []
    async def send(event): events.append(event)
    cloud = LeakyCloud()
    session = Session(DEFAULT_CARD, cloud, Brain(cloud), send, voice=True, predictions=False)
    try:
        await session.user_text("缺什么？")
        await session.response
        assert not any(e.get("type") == "utterance" for e in events)
        assert not any(e["role"] == "assistant" for e in session.memory.history)
        assert any(e.get("type") == "error" for e in events)
    finally:
        await session.close()


async def test_prediction_cannot_prepare_internal_narration_as_audio():
    class LeakyForecast:
        async def chat(self, messages, **kwargs):
            yield json.dumps({"candidates": [{"id": "c1", "when": "用户问缺什么",
                "reply": {"text": "分析：先判断授权关系。"}}]}, ensure_ascii=False)
    with pytest.raises(ValidationError):
        await Brain(LeakyForecast()).predict(Brain.snapshot(Memory(DEFAULT_CARD)))


def test_normal_customer_facing_explanations_are_allowed():
    for text in ["不是说您的授权是假的，是还缺品牌方给经销商的那一份。",
                 "您需要先补清楚的截图，再重新提交。", "这个我查不到，没法保证今天通过。"]:
        assert Say(text=text).text == text


async def test_reasoning_stream_field_is_never_returned_as_response_text():
    events = [
        {"choices": [{"delta": {"reasoning_content": "不应播报的内部推理"}}]},
        {"choices": [{"delta": {"reasoning_content": "另一段内部推理", "content": "您还缺一份授权。"}}]},
    ]
    stream = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)
    stream += "data: [DONE]\n\n"
    async def respond(request):
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = Cloud(Settings(api_key="test-only"), client)
        assert "".join([chunk async for chunk in cloud.chat([])]) == "您还缺一份授权。"
