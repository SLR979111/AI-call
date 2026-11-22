"""Choose where to re-speak a cut-off sentence using its playback receipt."""

import re

from .models import Say


def hesitation(text: str, *, answering_question: bool) -> bool:
    # Short denials, requests to wait, and acknowledgements can be whole turns.
    if "?" in text or "？" in text:
        return False
    if re.fullmatch(r"[（(\[](?:咳嗽声?|清嗓声?|噪音|杂音)[）)\]]", text.strip()):
        return True
    sounds = re.sub(r"[\s，,。.!！、…~～]", "", text)
    if answering_question and re.search("[嗯唔啊]", sounds):
        return False
    return not sounds or bool(re.fullmatch("[呃额嗯唔啊]+", sounds) or re.fullmatch("[呃额]+那个", sounds))


def heard_question(line: Say, spoken: str) -> bool:
    """A question at the end of a long opening may not have been heard yet."""
    if not spoken or not (line.question or line.text.endswith(("?", "？"))):
        return False
    question = line.question or re.split(r"[。！？!?]", line.text[:-1])[-1]
    start = line.text.rfind(question)
    return start >= 0 and len(spoken) >= start + min(2, len(question))


def pickup(line: Say, spoken: str) -> Say:
    """Restart at a nearby clause, keeping conditions and their consequences together."""
    text = line.text
    offset = len(spoken.rstrip())
    if offset == len(text):
        offset = len(spoken.rstrip("，,。！？!?；;、 "))
    if not offset:
        return line
    before = text[:offset]
    sentence = max((m.end() for m in re.finditer(r"[。！？!?；;]", before)), default=0)
    commas = re.finditer(r"(?<!\d)[，,]|[，,](?!\d)", text)
    start = max((m.end() for m in commas if sentence < m.end() <= offset), default=sentence)
    prefix = text[sentence:start]
    if (re.search("如果|假如|只有|只要|若|除非|否则|要是|虽然|即使|因为|之前|之后|一旦|尚未|并非|不是|不代表|不等于|前提|先", prefix)
            or re.match(r"\s*(?:就|才|否则|也|而|再|但|并|以及|所以|那么|这样)", text[start:])):
        start = sentence
    tail = text[start:].strip()
    # An explanatory clause can take a brief lead-in. Questions and instructions
    # start directly, and a second interruption must not accumulate fillers.
    if (start and tail.startswith(("目前", "现在的情况", "主要原因"))
            and not line.question and "？" not in tail and len(tail) + 5 <= 180):
        tail = "是这样的，" + tail
    if not tail:
        return line
    return line.model_copy(update={"text": tail,
                                   "covers": [] if start else line.covers,
                                   "question": line.question if line.question in tail else ""})
