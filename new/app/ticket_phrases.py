"""Ticket-derived phrases used by the lexical candidate selector.

The phrasebook is generated from the current TaskCard, so a new work order gets
its own common short forms without adding business words to the engine.
"""

from __future__ import annotations

import re

from .models import Candidate, TaskCard

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


def _source(candidate: Candidate) -> str:
    return (candidate.when + " " + candidate.reply.text + " " +
            " ".join(candidate.examples))


# Common question forms derived from a work order. These are intentionally
# coarse; a rule fires only when exactly one candidate matches.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"^(?:嗯+|呃+|啊+|哦+)?(?:方便|有空|现在可以|可以的|方便的)$", ("方便", "有空", "可以")),
    (r"提交.{0,6}(?:审核中|审核)|审核中|已经提交", ("提交", "审核")),
    (r"(?:品牌方|厂家|品牌).{0,8}直接.{0,4}授权|直接授权|直接给我们.{0,4}授权", ("直接授权", "品牌方")),
    (r"(?:授权书|授权).{0,8}(?:公司名|名称).{0,6}(?:错|不对|不一致|写错)", ("公司名", "名称")),
    (r"(?:商标|状态).{0,8}截图.{0,10}(?:包含|什么|怎么|要什么|哪)", ("截图", "商标")),
    (r"(?:还要|需要|用不用|是否).{0,8}经销商.{0,8}授权", ("经销商", "授权")),
    (r"(?:保存|只是保存|还没提交)", ("保存", "提交")),
)


def rule_candidate(user_input: str, candidates: list[Candidate],
                   card: TaskCard | None) -> Candidate | None:
    """Return one candidate when a ticket-derived question pattern is unambiguous."""
    if card is None or not candidates:
        return None
    text = re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", user_input).lower()
    for pattern, keywords in _RULES:
        if not re.search(pattern, text):
            continue
        matched = []
        for candidate in candidates:
            source = _source(candidate).lower()
            if any(keyword in source for keyword in keywords):
                matched.append(candidate)
        if len(matched) == 1:
            return matched[0]
    return None
