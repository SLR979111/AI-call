"""Ticket-derived phrases used by the lexical candidate selector.

The phrasebook is generated from the current TaskCard, so a new work order gets
its own common short forms without adding business words to the engine.
"""

from __future__ import annotations

import re

from .models import TaskCard

_TOKEN = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}")
_STOP = {
    "用户", "商家", "对方", "可以", "需要", "这个", "那个", "一个", "什么",
    "怎么", "如何", "已经", "如果", "但是", "然后", "现在", "本次", "资料",
    "信息", "说明", "不要", "不能", "没有", "不是", "进行", "情况", "问题",
}

# Common spoken forms that are useful across work orders. They are only used
# by the broad fallback after the precise examples/intents failed.
SHORT_YES = {
    "好", "好的", "好呀", "好嘞", "行", "可以", "是", "是的", "对", "对的",
    "嗯", "嗯嗯", "明白", "明白了", "懂了", "知道了", "收到", "没问题",
    "ok", "okay", "yes", "yep", "sure", "fine", "got it", "understood",
    "sounds good", "alright",
}
SHORT_NO = {
    "不", "不是", "不对", "不要", "不用", "没有", "没", "不行", "不可以",
    "no", "nope", "not really",
}


def terms(card: TaskCard) -> list[str]:
    """Return stable ticket-specific words, longest first."""
    text = " ".join([
        card.title, card.goal, card.facts,
        card.opening,
        " ".join(point.description for point in card.checkpoints),
    ])
    seen: list[str] = []
    for token in _TOKEN.findall(text):
        token = token.strip()
        if len(token) < 2 or token in _STOP or token in seen:
            continue
        seen.append(token)
    return sorted(seen, key=lambda value: (-len(value), value))[:40]


def short_form(text: str) -> str:
    return re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", text).lower()


def ticket_phrases(card: TaskCard) -> list[str]:
    """Common phrases a caller may use for this ticket."""
    words = terms(card)
    phrases: list[str] = []
    for word in words:
        phrases.extend([word, f"{word}怎么", f"{word}要什么", f"{word}是什么"])
    phrases.extend(sorted(SHORT_YES | SHORT_NO))
    return list(dict.fromkeys(phrases))
