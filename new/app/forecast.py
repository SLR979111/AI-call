"""Read complete candidate objects as they arrive and match whole spoken replies."""

import json
import re
from collections import OrderedDict
from contextlib import aclosing

from .models import Candidate


class OpeningForecastCache:
    """Share only the first-turn forecasts for identical task cards."""

    def __init__(self):
        self.entries = OrderedDict()

    def get(self, key):
        if key not in self.entries:
            return [], False
        items, complete = self.entries[key]
        self.entries.move_to_end(key)
        return [item.model_copy(deep=True) for item in items], complete

    def store(self, key, items, *, complete):
        if not items:
            return
        existing, finished = self.entries.get(key, ([], False))
        if finished or (not complete and len(existing) >= len(items)):
            return
        self.entries[key] = [item.model_copy(deep=True) for item in items], complete
        self.entries.move_to_end(key)
        if len(self.entries) > 16:
            self.entries.popitem(last=False)


async def stream_candidates(chunks, limit):
    """Decode the existing candidates array without waiting for its final item."""
    decoder = json.JSONDecoder()
    header = re.compile(r'^\s*(?:```(?:json)?\s*)?\{\s*"candidates"\s*:\s*\[')
    buffer = ""
    opened = False
    count = 0
    async with aclosing(chunks):
        async for chunk in chunks:
            buffer += chunk
            if not opened:
                match = header.match(buffer)
                if not match:
                    continue
                buffer = buffer[match.end():]
                opened = True
            while True:
                buffer = buffer.lstrip()
                if count:
                    if buffer.startswith("]"):
                        break
                    if not buffer.startswith(","):
                        break
                    item = buffer[1:].lstrip()
                else:
                    item = buffer
                try:
                    value, end = decoder.raw_decode(item)
                except json.JSONDecodeError:
                    break
                count += 1
                buffer = item[end:]
                if count <= limit:
                    yield Candidate.model_validate(value)
    if not opened:
        # Older providers may include metadata before the candidates array.
        data = json.loads(buffer.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
        for value in data["candidates"][:limit]:
            yield Candidate.model_validate(value)
        return
    tail = buffer.strip().removesuffix("```").rstrip()
    if not tail.startswith("]"):
        raise ValueError("Incomplete forecast array")
    json.loads('{"candidates":[' + tail)


def spoken_form(text: str) -> str:
    # Preserve questions, negation, and everything after a conjunction. Only
    # remove filler at the start and particles on a closed set of short replies.
    text = re.sub(r"[\s，,。.!！、“”‘’]", "", text).replace("?", "？")
    text = re.sub(r"^[嗯呃哦噢啊]+(?=[^嗯呃哦噢啊])", "", text)
    for pattern, form in (
        (r"(?:是的?|对的?|没错)+", "是的"),
        (r"(?:好[的啊呀]?)+", "好的"),
        (r"(?:我)?(?:知道|明白|懂|了解)(?:了)?", "明白了"),
        (r"(?:我)?(?:现在)?方便(?:的)?", "方便"),
    ):
        if re.fullmatch(pattern + r"[呢啊呀啦]*", text):
            return form
    return text


def likely_short_answers(snapshot):
    """Ground the common short-answer forecast in the current question."""
    pending = snapshot.get("pending_reply", [])
    last = pending[-1] if pending else {}
    question = (last.get("question") or last.get("text", "")).rstrip()
    if question.endswith(("？", "?")):
        question = re.split(r"[。？！!?]", question[:-1])[-1]
        if "还是" in question:
            left, right = question.split("还是", 1)
            left = re.sub(r"^(?:请问)?[您你]?(?:想|要|是)?", "", left).strip("，, ")
            right = right.strip("，, ")
            if 1 < len(left) <= 20 and 1 < len(right) <= 20:
                return [left + "。", right + "。"]
        if "方便" in question:
            return ["现在方便。", "现在不方便。"]
        if "负责" in question:
            return ["是的。", "不是，我不负责。"]
    return ["好的。", "是的。"]


def compatible_short_answer(key, previous, candidate):
    if key not in {"是的", "好的", "明白了", "方便"}:
        return True
    if key == "是的" and (not re.search(r"[吗么吧][？?]?$", previous.strip())
                         or re.search(r"还是|或者|是否", previous)):
        return False
    # Acknowledging the speaker supplies neither a second question nor a new
    # business fact. Ignore exclusions following the forecast's positive part.
    positive = re.split(r"且没|且未|但没|但未|[，,]没有|；", candidate.when, maxsplit=1)[0]
    if re.search(r"(?:并|同时|顺便)(?:直接)?(?:问|询问|追问|要求)|记不清|不知道|不确定|不负责|不方便|仍有疑问", positive):
        return False
    if key != "是的" and re.search(r"(?:已|已经|刚刚).{0,5}(?:提交|补交|准备好|传好)|确认自己是负责人", positive):
        return False
    if key == "是的":
        for topic in ("提交", "上传", "准备好", "看清", "负责"):
            if topic in positive and topic not in previous:
                return False
    return True


def match_example(user: str, candidates: list[Candidate], previous: str):
    key = spoken_form(user)
    if not key or re.search(r"但是|不过|另外|还有.*问题|没听|再说一遍|不是这个意思", key):
        return None
    matches = [c for c in candidates if compatible_short_answer(key, previous, c)
               and any(spoken_form(example) == key for example in c.examples)]
    if not matches or any(c.reply != matches[0].reply for c in matches[1:]):
        return None
    return matches[0]
