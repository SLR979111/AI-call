from __future__ import annotations

from app.agent.reply_stream import ReplyExtractor


def test_reply_extractor_emits_before_rest_of_json():
    extractor = ReplyExtractor()
    assert extractor.feed('{"intent": "faq", "reply": "你好') is None
    assert extractor.feed('，我是客服。", "outcome": "answered"}') == "你好，我是客服。"
    assert extractor.done is True


def test_first_sentence_cuts_at_first_clause_for_early_tts():
    """TTS starts on this prefix; waiting for 。 costs ~0.3-1 s of first audio."""
    extractor = ReplyExtractor()
    assert extractor.feed('{"reply": "您好') is None
    prefix = extractor.first_sentence()
    assert prefix is None  # shorter than the minimum clause length
    assert extractor.feed("，我这边是京东") is None
    assert extractor.first_sentence() == "您好，"


def test_default_thinking_disables_hidden_reasoning_for_flash_models():
    from app.llm.router import default_thinking

    assert default_thinking("deepseek-flash") == "disabled"
    assert default_thinking("deepseek-chat") is None
    assert default_thinking("") is None
