"""Deterministic forecast candidates derived from the current TaskCard.

The forecast model still writes the conversational candidates.  It cannot know
which question families recur for a work order, though, and its parallel
branches often miss half of them.  This module derives the recurring families
from the card itself - checkpoint ids and descriptions, facts and opening -
without calling a model and without adding business words to the rule engine.

Every generated candidate is tagged with:

``checkpoint``
    The TaskCard checkpoint the reply answers, when one applies.
``intent``
    The question family it covers (``missing_material``, ``timing``, ...).
``polarity``
    ``question`` when the caller asks, ``statement`` when the caller reports.

The templates are intentionally conservative: they only say what the card
itself says, never promise an outcome, and never invent a URL, hotline or menu.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable

from .models import Candidate, Checkpoint, Say, TaskCard

ENABLED_ENV = "TICKET_FORECAST_ENABLED"
MAX_ENV = "TICKET_FORECAST_MAX"
DEFAULT_MAX = 10

_LEAD = re.compile(r"^(?:说明|讲清|解释|介绍|告知|确认|核对|按)")
_MATERIAL = re.compile(
    r"(?:缺少的|缺少|缺的|缺失的|缺失|要补的|要补|补充的|补充|补齐|"
    r"补交的|补交|需要补的|需要补)([^，。；、]+)"
)
_MATERIAL_NOUN = re.compile(r"[^，。；、]{0,12}(?:截图|注册证|材料|资料|文件|证明|授权书|授权)")
_NAME_ONLY = re.compile(r"(?:名称|公司名|更名|不一致|填错|主体)")
_META_ACTION = re.compile(r"(?:情况|说明|下一步|步骤|流程|操作)")


def enabled() -> bool:
    """Feature switch for A/B comparison; enabled unless explicitly turned off."""
    raw = os.getenv(ENABLED_ENV, "1").strip().lower()
    return raw not in {"0", "false", "off", "no", "disabled"}


def max_candidates_from_env() -> int:
    raw = os.getenv(MAX_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX
    return value if value > 0 else DEFAULT_MAX


def card_text(card: TaskCard) -> str:
    return " ".join([
        card.title, card.goal, card.facts, card.opening,
        " ".join(point.id + " " + point.description for point in card.checkpoints),
    ])


def _checkpoint(card: TaskCard, pattern: str | None,
                exclude: str | None = None) -> Checkpoint | None:
    """First checkpoint whose id or description names the requested topic."""
    if not pattern:
        return None
    for point in card.checkpoints:
        text = f"{point.id} {point.description}"
        if re.search(pattern, text) and not (exclude and re.search(exclude, text)):
            return point
    return None


def material_topic(point: Checkpoint) -> str:
    """Turn a checkpoint description into a short spoken material phrase."""
    text = _LEAD.sub("", point.description.strip())
    match = _MATERIAL.search(text)
    topic = match.group(1) if match else ""
    if not topic or _META_ACTION.search(topic):
        noun = _MATERIAL_NOUN.search(text)
        topic = noun.group(0) if noun else ""
    topic = re.sub(r"^(?:本次|这个|该|相关|案例的)+", "", topic)
    topic = re.split(r"或|以及|和", topic)[0].strip("，。；、 ")
    topic = re.sub(r"(?:要清晰|要一致|即可|就行了?)$", "", topic).strip()
    return topic[:32] if topic else "相关材料"


def _how_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "提交入口" in text or "京麦" in text or "重新提交" in text:
        return "补齐材料后，回到原提交入口按提示重新提交就行。"
    if "页面提示" in text:
        return "按页面提示补充材料或等待就行。"
    if "报修" in text:
        return "需要检修的话，从自己的运营商官方客服入口报修就行。"
    if "安装" in text or "预约" in text:
        return "安装一般要几个工作日，具体以实际安排为准，这里没法预约。"
    return "按页面提示补齐信息就行。"


def _how_examples(card: TaskCard) -> tuple[str, str]:
    text = card_text(card)
    if "提交入口" in text or "京麦" in text or "重新提交" in text:
        return ("怎么补？", "在哪里提交？")
    if "页面提示" in text:
        return ("怎么补？", "下一步怎么办？")
    if "报修" in text:
        return ("怎么报修？", "下一步怎么办？")
    if "安装" in text or "预约" in text:
        return ("怎么安装？", "怎么办理？")
    return ("怎么补？", "下一步怎么办？")


def _authorization_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "经销商" in text and "品牌方" in text:
        return "还缺品牌方给经销商的那一段授权，得把两段授权接起来。"
    return "授权关系要补齐，品牌方和经销商两段都要对得上。"


def _timing_reply(card: TaskCard) -> str:
    text = card_text(card)
    if ("加急" in text or "审核一定通过" in text or "一定通过" in text
            or "保证通过" in text or "能否通过" in text or "能不能通过" in text):
        return "具体时间以实际审核为准，加急或通过都没法保证。"
    if "工作日" in text:
        return "通常要几个工作日，具体时间以实际安排为准。"
    return "具体时间我这里没法确认，以实际安排为准。"


def _missing_reply(card: TaskCard, point: Checkpoint | None) -> str:
    if point is None:
        return ""
    return f"这次主要要补的是{material_topic(point)}。"


def _fixed(text: str) -> Callable[[TaskCard, Checkpoint | None], str]:
    return lambda _card, _point: text


@dataclass(frozen=True)
class _Family:
    intent: str
    polarity: str
    condition: str
    examples: tuple[str, str]
    reply: Callable[[TaskCard, Checkpoint | None], str]
    checkpoint_pattern: str | None = None
    checkpoint_exclude: str | None = None
    checkpoint_required: bool = True
    require: str | None = None


# Order is priority: when the merged pool hits its cap, the families that
# resolve a rejection or a submission question survive first.
_FAMILIES: tuple[_Family, ...] = (
    _Family(
        "missing_material", "question",
        "用户问这次缺什么材料或还要补什么，且没有提出新的其他问题",
        ("缺什么材料？", "还要补什么？"),
        _missing_reply,
        checkpoint_pattern=r"(?:缺少|缺|补充|补齐|补交|材料|资料|文件|截图|注册证|证明|授权)",
        checkpoint_exclude=_NAME_ONLY.pattern,
    ),
    _Family(
        "how_to_submit", "question",
        "用户问补好之后怎么操作、在哪里提交或下一步做什么",
        ("怎么补？", "在哪里提交？"),
        lambda card, _point: _how_reply(card),
        checkpoint_pattern=r"(?:提交|补交|入口|补充材料|页面提示|报修|安装|预约|办理|下一步)",
    ),
    _Family(
        "authorization", "question",
        "用户问经销商授权、品牌方直接授权或授权链路该怎么补",
        ("还要补经销商授权吗？", "品牌方直接授权行不行？"),
        lambda card, _point: _authorization_reply(card),
        checkpoint_pattern=r"(?:授权|经销商|品牌方|直授|链路)",
        checkpoint_exclude=_NAME_ONLY.pattern,
    ),
    _Family(
        "name_mismatch", "question",
        "用户问授权书上的公司名与店铺主体不一致、写错或更名",
        ("公司名不一致怎么办？", "授权书上的公司名写错了。"),
        _fixed("授权上的公司名要和店铺主体一致，填错的话请原授权方重新开具。"),
        checkpoint_pattern=r"(?:名称|公司名|主体|更名|填错|不一致)",
    ),
    _Family(
        "screenshot", "question",
        "用户问商标状态截图要包含什么、是否清晰，或注册证能否代替",
        ("商标状态截图要包含什么？", "注册证能代替截图吗？"),
        _fixed("需要清晰的商标当前状态截图，要能看清名称、注册号和状态。"),
        checkpoint_pattern=r"(?:截图|注册证|商标状态|商标)",
    ),
    _Family(
        "saved", "statement",
        "用户说只是保存了、还没有提交",
        ("只是保存了。", "只保存还没提交。"),
        _fixed("只保存不算提交，还需要点一下提交。"),
        checkpoint_pattern=r"(?:提交|补交|保存|审核)",
        require=r"(?:保存|没提交|尚未提交|未提交)",
    ),
    _Family(
        "submitted", "statement",
        "用户说已经提交或重新提交，正在等提示",
        ("已经提交了。", "重新提交了。"),
        _fixed("已经提交的话，留意这次申请的最新提示就行。"),
        checkpoint_pattern=r"(?:提交|补交|审核)",
        require=r"(?:提交|补交)",
    ),
    _Family(
        "in_review", "question",
        "用户问审核进度、是不是还在审核中",
        ("还在审核吗？", "审核到哪一步了？"),
        _fixed("审核进度我这里查不到，以页面提示为准。"),
        checkpoint_pattern=r"(?:审核|进度)",
        checkpoint_required=False,
        require=r"(?:审核中|审核进度|审核|进度)",
    ),
    _Family(
        "timing", "question",
        "用户问多久出结果、能不能加急或能不能保证通过",
        ("多久能出结果？", "能加急吗？"),
        lambda card, _point: _timing_reply(card),
        checkpoint_pattern=r"(?:时间|多久|加急|通过|审核|出结果)",
        checkpoint_required=False,
        require=r"(?:多久|时间|加急|工作日|审核|出结果|一定通过|保证通过|能否通过|能不能通过)",
    ),
)


def generate_ticket_candidates(card: TaskCard, *, limit: int | None = None) -> list[Candidate]:
    """Return the deterministic question families that apply to this card."""
    cap = max(1, limit if limit is not None else max_candidates_from_env())
    text = card_text(card)
    candidates: list[Candidate] = []
    for family in _FAMILIES:
        if len(candidates) >= cap:
            break
        if family.require and not re.search(family.require, text):
            continue
        point = _checkpoint(card, family.checkpoint_pattern, family.checkpoint_exclude)
        # A checkpoint-anchored family without a matching checkpoint would be
        # guessing at business content the card does not contain.
        if family.checkpoint_required and family.checkpoint_pattern and point is None:
            continue
        reply = family.reply(card, point)
        if not reply:
            continue
        examples = _how_examples(card) if family.intent == "how_to_submit" else family.examples
        candidates.append(Candidate(
            id=f"t_{family.intent}",
            when=family.condition,
            examples=list(examples),
            reply=Say(text=reply),
            checkpoint=point.id if point is not None else None,
            intent=family.intent,
            polarity=family.polarity,
        ))
    return candidates
