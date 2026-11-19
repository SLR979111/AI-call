from __future__ import annotations

import re

from .models import Candidate, TaskCard

_TOKEN = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}")
_STOP = {
    "用户", "商家", "对方", "可以", "需要", "这个", "那个", "一个", "什么",
    "怎么", "如何", "已经", "如果", "但是", "然后", "现在", "本次", "资料",
    "信息", "说明", "不要", "不能", "没有", "不是", "进行", "情况", "问题",
    "一下", "我们", "你们", "咱们", "这次", "当前", "直接", "具体",
}
_NGRAM_STOP = {
    "用户", "商家", "对方", "什么", "怎么", "这个", "那个", "已经", "没有",
    "不是", "可以", "需要", "一个", "我们", "你们", "一下", "这边", "怎么",
}

SHORT_YES = {
    "好", "好的", "好呀", "好嘞", "好滴", "行", "行吧", "可以", "可以呀",
    "是", "是的", "是的呀", "对", "对的", "嗯", "嗯嗯", "嗯呢", "明白",
    "明白了", "懂了", "知道了", "收到", "清楚了", "了解", "了解了",
    "没问题", "当然", "确实", "中", "要得", "ok", "okay", "yes", "yeah",
    "yep", "sure", "fine", "gotit", "understood", "soundsgood", "alright",
    "cool", "great", "noproblem",
}
SHORT_NO = {
    "不", "不是", "不对", "不要", "不用", "没有", "没", "不行", "不可以",
    "算了", "拒绝", "没空", "不方便", "忙", "以后再说", "稍后", "等会儿",
    "no", "nope", "notnow", "busy", "later", "nothanks", "notreally",
    "cannot", "cant",
}
QUESTION = re.compile(r"什么|怎么|如何|哪|多少|是否|要不要|需不需要|可以吗|多久|为什么|能不能")
NEGATION = re.compile(r"(?<!是)不是|不对|没有|没空|不方便|(?<!行)不行|不可以|不用|不要|算了|拒绝|改口|换人")


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
        # Chinese task facts often arrive as long clauses without spaces.
        # Add short substrings so "路由器"/"网速" are usable ticket words.
        if len(token) > 4:
            for size in (3, 4):
                for start in range(0, len(token) - size + 1):
                    part = token[start:start + size]
                    if part not in _STOP and part not in seen:
                        seen.append(part)
    short = sorted((word for word in seen if len(word) <= 4),
                   key=lambda value: (-len(value), value))[:500]
    long = sorted((word for word in seen if len(word) > 4),
                  key=lambda value: (-len(value), value))[:60]
    return short + long


def short_form(text: str) -> str:
    text = re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", text).lower()
    # Degree words are filler for lexical matching: "网速很慢" == "网速慢".
    text = re.sub(r"(?:很|非常|特别|有点|太|比较|挺)+", "", text)
    return text.replace("连不上", "上不了").replace("联不上", "上不了")


def ticket_phrases(card: TaskCard) -> list[str]:
    """Common phrases a caller may use for this ticket."""
    words = terms(card)
    phrases: list[str] = []
    for word in words:
        phrases.extend([word, f"{word}怎么", f"{word}要什么", f"{word}是什么"])
    phrases.extend(sorted(SHORT_YES | SHORT_NO))
    return list(dict.fromkeys(phrases))


def _unique(items: list[Candidate]) -> Candidate | None:
    return items[0] if len(items) == 1 else None


def _source(candidate: Candidate) -> str:
    return (candidate.when + " " + candidate.reply.text + " " +
            " ".join(candidate.examples))


def _ngrams(text: str) -> set[str]:
    grams = {
        text[i:i + n]
        for n in (2, 3, 4, 5)
        for i in range(max(0, len(text) - n + 1))
    }
    return {gram for gram in grams if gram not in _NGRAM_STOP}


def rule_candidate(user_input: str, candidates: list[Candidate],
                   card: TaskCard | None) -> Candidate | None:
    """Return one candidate when ticket words and caller fragments agree."""
    if card is None or not candidates:
        return None
    text = short_form(user_input)
    if not text:
        return None
    if NEGATION.search(text) or text in SHORT_NO:
        return None
    english_yes = {"ok", "okay", "yes", "yeah", "yep", "sure", "fine", "gotit",
                   "understood", "soundsgood", "alright", "cool", "great", "noproblem"}
    if text in english_yes:
        matches = [
            candidate for candidate in candidates
            if re.search(r"确认|继续|愿意|方便|负责|可以|同意|明白", _source(candidate))
        ]
        return _unique(matches)
    # A bare "好的/是的" is too weak for candidate selection. The precise
    # short-answer rules in ContextAwareDecisionEngine handle it.
    if text in SHORT_YES:
        return None

    if re.search(r"缺什么|缺哪些|要补什么|补什么|补哪些", text):
        matches = [
            candidate for candidate in candidates
            if re.search(r"缺|材料|补", short_form(_source(candidate)))
        ]
        if len(matches) == 1:
            return matches[0]
    words = terms(card)
    # If a ticket-specific word names exactly one candidate, that is stronger
    # evidence than a generic overlap score.
    for word in words:
        if len(word) < 2 or word not in text:
            continue
        matches = [
            candidate for candidate in candidates
            if word in short_form(_source(candidate))
        ]
        if len(matches) == 1:
            return matches[0]
    grams = _ngrams(text)
    scored: list[tuple[int, Candidate]] = []
    for candidate in candidates:
        source = short_form(_source(candidate))
        when_source = short_form(candidate.when + " " + " ".join(candidate.examples))
        word_score = sum(len(word) for word in words if word in text and word in source)
        # The condition/examples are stronger evidence than prose in the reply.
        gram_score = sum(len(gram) for gram in grams if gram in when_source) * 2
        gram_score += sum(len(gram) for gram in grams if gram in source)
        score = word_score + gram_score
        if score:
            scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    if len(scored) == 1:
        return scored[0][1]
    if scored[0][0] >= 2 and scored[0][0] > scored[1][0]:
        return scored[0][1]
    return None
