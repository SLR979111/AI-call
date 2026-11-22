from __future__ import annotations

import re

from .models import Candidate


_GENERIC = {"用户", "商家", "对方", "这个", "那个", "什么", "怎么", "可以",
            "需要", "已经", "没有", "不是", "我们", "你们"}


def _normalized(text: str) -> str:
    return re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", text).lower()


def worth_sending(user_input: str, candidates: list[Candidate]) -> bool:
    """Cheap gate: spend the 0.7-0.9s decision request only on plausible matches.

    The rule engine has already failed; this is not trying to be semantic. It
    only prevents an unrelated full sentence from paying for a decision call.
    """
    if not candidates:
        return False
    user = _normalized(user_input)
    if len(user) <= 2:
        return True
    if len(user) <= 8:
        return True
    grams = {
        user[i:i + n]
        for n in (2, 3, 4)
        for i in range(max(0, len(user) - n + 1))
    }
    grams = {gram for gram in grams if gram not in _GENERIC}
    for candidate in candidates:
        source = _normalized(
            candidate.when + " " + candidate.reply.text + " " + " ".join(candidate.examples)
        )
        if any(gram in source for gram in grams):
            return True
    return False
