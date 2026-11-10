from __future__ import annotations

from app.agent.reply_stream import ReplyExtractor


def test_reply_extractor_emits_before_rest_of_json():
    extractor = ReplyExtractor()
    assert extractor.feed('{"intent": "faq", "reply": "你好') is None
    assert extractor.feed('，我是客服。", "outcome": "answered"}') == "你好，我是客服。"
    assert extractor.done is True
