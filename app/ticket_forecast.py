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

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import Candidate, Checkpoint, Say, TaskCard

ENABLED_ENV = "TICKET_FORECAST_ENABLED"
MAX_ENV = "TICKET_FORECAST_MAX"
EXCLUDE_ENV = "TICKET_FORECAST_EXCLUDE"
EXCLUDE_BY_TASK_ENV = "TICKET_FORECAST_EXCLUDE_BY_TASK"
DEFAULT_MAX = 12

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


def excluded_intents_from_env(card: TaskCard | None = None) -> frozenset[str]:
    """Manually audited exclusions, global or keyed by card title.

    ``TICKET_FORECAST_EXCLUDE`` is comma-separated.  An optional JSON object in
    ``TICKET_FORECAST_EXCLUDE_BY_TASK`` can exclude different intents per card
    title, so a family that is dead on broadband does not disappear on JD.
    """
    raw = os.getenv(EXCLUDE_ENV, "")
    result = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if card is not None:
        mapping_raw = os.getenv(EXCLUDE_BY_TASK_ENV, "").strip()
        if mapping_raw:
            try:
                mapping = json.loads(mapping_raw)
            except (TypeError, ValueError):
                mapping = {}
            if isinstance(mapping, dict):
                values = mapping.get(card.title, [])
                if isinstance(values, str):
                    values = values.split(",")
                if isinstance(values, list):
                    result.update(str(part).strip().lower()
                                  for part in values if str(part).strip())
    return frozenset(result)


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
        return "材料补好后，回到上次提交的页面。按提示再提交一次就行。"
    if "页面提示" in text:
        if "材料" in text:
            return "您看一下页面提示。让您补材料就补，没提示就先等。"
        return "您照着页面的提示往下办就行。"
    if "报修" in text:
        return "要报修的话，您找自家宽带的官方客服就行。"
    if "安装" in text or "预约" in text:
        return "安装一般要几个工作日。哪天能装得看安排，我这边没法预约。"
    return "您看页面还缺什么，把那几项补上就行。"


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
        return "还缺品牌方给经销商的那份授权。两份授权得接得上。"
    return "两份授权得接得上，一份是品牌方开的，一份是经销商开的。"


def _timing_reply(card: TaskCard) -> str:
    text = card_text(card)
    if ("加急" in text or "审核一定通过" in text or "一定通过" in text
            or "保证通过" in text or "能否通过" in text or "能不能通过" in text):
        return "什么时候能出结果，我这边说不准。能不能加急、能不能过，也得看审核。"
    if "工作日" in text:
        return "一般得等几个工作日，具体哪天得看安排。"
    return "具体要等多久，我这边说不准，得看实际安排。"


def _missing_reply(card: TaskCard, point: Checkpoint | None) -> str:
    if point is None:
        return ""
    return f"这次还得补一下{material_topic(point)}。"


def _owner_reply(card: TaskCard, point: Checkpoint | None) -> str:
    if point is None:
        return ""
    return f"您这单还缺{material_topic(point)}。"


# --- scenario-specific conservative families -------------------------------
#
# These helpers only restate what the card's own facts say.  They never invent
# a URL, hotline, menu path, delivery time or invoice time.

_PLAN_LINE = re.compile(r"【[^】]*】\s*(\d+)\s*M[，,]\s*(\d+)\s*元\s*/\s*月")


def _plans(card: TaskCard) -> list[dict]:
    plans = []
    for line in card.facts.splitlines():
        match = _PLAN_LINE.search(line)
        if not match:
            continue
        devices = re.search(r"(\d+)\s*台", line)
        plans.append({
            "speed": int(match.group(1)),
            "price": int(match.group(2)),
            "devices": int(devices.group(1)) if devices else None,
        })
    return sorted(plans, key=lambda plan: (plan["speed"], plan["price"]))


def _typical_plan(plans: list[dict]) -> dict | None:
    if not plans:
        return None
    return plans[len(plans) // 2]


def _price_reply(card: TaskCard) -> str:
    plans = _plans(card)
    if not plans:
        return ""
    shown = plans[:3]
    joined = "，".join(f"{plan['speed']}M {plan['price']}元每月" for plan in shown)
    return joined + ("等。" if len(plans) > len(shown) else "。")


def _price_examples(card: TaskCard) -> tuple[str, str]:
    typical = _typical_plan(_plans(card))
    if typical is None:
        return ("套餐多少钱？", "怎么收费？")
    return (f"{typical['speed']}M 多少钱？", "套餐怎么收费？")


def _recommend_reply(card: TaskCard) -> str:
    plans = _plans(card)
    with_devices = [plan for plan in plans if plan["devices"] is not None]
    if len(plans) < 2 or len(with_devices) < 2:
        return ""
    typical = with_devices[len(with_devices) // 2]
    largest = with_devices[-1]
    if typical is largest:
        typical = with_devices[0]
    return (f"先看常用设备数和预算：{typical['devices']}台以内可看"
            f"{typical['speed']}M {typical['price']}元每月，"
            f"{largest['devices']}台左右再看"
            f"{largest['speed']}M {largest['price']}元每月。")


def _install_reply(card: TaskCard) -> str:
    text = card_text(card)
    days = re.search(r"(\d+)\s*至\s*(\d+)\s*个工作日", text)
    lead = (f"安装通常要{days.group(1)}至{days.group(2)}个工作日"
            if days else "安装通常要几个工作日")
    if "无法查询地址" in text or "不能确认或预约" in text or "没法预约" in text:
        return f"{lead}，但这里查不到地址能不能装，也没法预约具体日期。"
    return f"{lead}，具体时间以实际安排为准。"


def _connectivity_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "光猫" in text:
        return "先看看光猫的电源和灯色，再确认是家里都上不了网，还是只有一台设备不行。"
    if "完全断网" in text:
        return "先确认是完全上不了网，还是只有部分设备或应用不行。"
    return "先确认是全部上不了网，还是只有一台设备或一个应用不行。"


def _slow_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "靠近路由器" in text:
        return "先看是一台设备慢，还是多台设备都慢；只有一台的话，靠近路由器再试同一个网页。"
    return "先确认是一台设备还是多台设备都慢，再决定下一步。"


def _router_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "Reset" in text or "恢复出厂" in text:
        return ("路由器可以按电源重启，别按 Reset 恢复出厂；"
                "重启会让其他设备短暂断网，后台我这边看不到。")
    return "路由器可以先按电源重启，重启会让其他设备短暂断网。"


def _device_check_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "一台" in text and "多台" in text:
        return ("先确认是一台设备还是多台设备都不行；只有一台的话，"
                "先看它连的是不是家里正确的无线网络。")
    return "先确认是一台设备还是多台设备都不行，再决定下一步。"


def _optical_light_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "光信号" in text and "红灯" in text:
        return ("如果确认是光信号或 LOS 灯亮红灯，可能是光信号异常，"
                "得找自家运营商核查；别的红灯不能直接当成光纤故障。")
    return "如果光信号灯亮红灯，可能是光信号异常，需要找自家运营商核查。"


def _repair_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "派单" in text:
        return ("需要线路、账号或设备检修时，从自己的运营商官方客服入口报修；"
                "我这里不能派单，也不猜号码或承诺维修时间。")
    return "需要检修时，从自己的运营商官方客服入口报修；具体号码和时间我这里没法确认。"


def _upload_issue_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "京麦" in text and "驳回意见页" in text:
        return ("您打开京麦里这笔品牌申请，看退回意见。按那里的提示重新上传，再点提交。"
                "再往下点哪儿我这边看不到，也没法替您上传。")
    if "上传" in text and "提交入口" in text:
        return ("您回到上次提交的页面，重新上传，再点提交。"
                "再往下点哪儿我这边看不到，也没法替您上传。")
    return ("您照着页面提示重新上传，再回到上次的页面提交。"
            "再往下点哪儿我这边看不到，也没法替您上传。")


def _contract_reply(card: TaskCard) -> str:
    text = card_text(card)
    months = re.search(r"(\d+)\s*个月合约", text)
    fee = re.search(r"剩[余下]?合约月数乘\s*(\d+)\s*元", text)
    cap = re.search(r"上限\s*(\d+)\s*元", text)
    if not months or not fee:
        return ""
    tail = f"，最多{cap.group(1)}元" if cap else ""
    return (f"{months.group(1)}个月合约；提前结束按剩余合约月数乘"
            f"{fee.group(1)}元{tail}。")


def _coverage_reply(card: TaskCard) -> str:
    text = card_text(card)
    area = re.search(r"超过\s*(\d+)\s*平方米", text)
    walls = re.search(r"穿过\s*([一二两三四五六七八九十\d]+)\s*堵", text)
    node = re.search(r"(\d+)\s*元的覆盖节点", text)
    if not area or not node:
        return ""
    wall_text = walls.group(1) if walls else "两"
    return (f"超过{area.group(1)}平方米或穿过{wall_text}堵以上墙，"
            f"需要额外{node.group(1)}元覆盖节点；不满足条件不能保证全屋覆盖。")


def _promotion_reply(card: TaskCard) -> str:
    return "本次资料没有优惠、折扣或赠送，也不能申请特殊审批或隐藏套餐。"


def _lost_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "运单状态" in text and "物流记录" in text:
        return ("一直没收到或显示签收异常的话，先看运单状态和最近物流记录，"
                "再联系承运快递员或官方客服核实。")
    return "一直没收到的话，先查运单状态和最近的物流记录，再联系承运方核实。"


def _logistics_status_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "运单状态" in text and "物流记录" in text:
        return ("这里能看到运单状态和最近一条物流记录，更细的网点内部进度看不到；"
                "一直没更新就联系承运快递员或官方客服。")
    return "先查一下运单状态和最近的物流记录，更细的内部进度看不到。"


def _logistics_contact_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "不承诺今天送达" in text:
        return "可以联系承运快递员或官方客服；具体联系方式我这边没有，也不承诺今天送到。"
    return "可以联系承运快递员或官方客服，具体联系方式我这边没有。"


def _logistics_address_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "不能直接修改地址" in text:
        return "改地址需要联系承运方，我这里不能直接修改，也不保证一定能改成功。"
    return "改地址需要联系承运方，我这里不能直接修改。"


def _invoice_type_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "普通发票和专用发票需要的资料不同" in text:
        return "普通发票和专用发票需要的资料不同，具体资料以开票页面提示为准。"
    return "普通发票和专用发票要求不同，具体以开票页面提示为准。"


def _invoice_header_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "抬头填错通常需要先作废原申请再重新提交" in text:
        return ("发票抬头要和主体信息一致；抬头填错通常要先作废原申请再重新提交，"
                "具体以页面提示为准。")
    return "发票抬头要和主体信息一致，具体以页面提示为准。"


def _invoice_status_reply(card: TaskCard) -> str:
    text = card_text(card)
    if "只能看到申请记录和页面状态" in text:
        return ("这里只能看到申请记录和页面状态，不能直接开票；"
                "重开通常要先作废原申请再重新提交，具体以页面提示为准。")
    return "这里只能看到申请记录和页面状态，不能直接开票，具体以页面提示为准。"


def _fixed(text: str) -> Callable[[TaskCard, Checkpoint | None], str]:
    return lambda _card, _point: text


@dataclass(frozen=True)
class _Family:
    intent: str
    polarity: str
    condition: str
    examples: tuple[str, str] | Callable[[TaskCard], tuple[str, str]]
    reply: Callable[[TaskCard, Checkpoint | None], str]
    checkpoint_pattern: str | None = None
    checkpoint_exclude: str | None = None
    checkpoint_required: bool = True
    require: str | None = None
    require_all: tuple[str, ...] = ()


# Order is priority: when the merged pool hits its cap, the families that
# resolve a rejection or a submission question survive first.
_FAMILIES: tuple[_Family, ...] = (
    _Family(
        "owner_confirmed", "statement",
        "用户确认自己是这次资料的负责人，没有提出其他问题",
        ("是的。", "是我。"),
        _owner_reply,
        checkpoint_pattern=r"(?:授权|经销商|品牌方|材料|资料|文件|证明|截图)",
        checkpoint_exclude=_NAME_ONLY.pattern,
        require=r"(?:负责.*[吗么]|是不是负责|您是负责)",
    ),
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
        _how_examples,
        lambda card, _point: _how_reply(card),
        checkpoint_pattern=r"(?:提交|补交|入口|补充材料|页面提示|报修|安装|预约|办理|下一步)",
    ),
    _Family(
        "upload_issue", "question",
        "用户说上传失败、材料传不上去，或问材料怎么重新上传",
        ("上传失败了怎么办？", "材料怎么重新传？"),
        lambda card, _point: _upload_issue_reply(card),
        checkpoint_pattern=r"(?:提交|上传|入口|驳回|补交)",
        require=r"(?:上传|京麦|驳回意见页)",
        require_all=(r"(?:提交|上传)",),
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
        _fixed("授权书上得写开店那家公司的全名。写错了，就请原来开授权的那家重开一份。"),
        checkpoint_pattern=r"(?:名称|公司名|主体|更名|填错|不一致)",
        checkpoint_exclude=r"(?:发票|抬头)",
    ),
    _Family(
        "screenshot", "question",
        "用户问商标状态截图要包含什么、是否清晰，或注册证能否代替",
        ("商标状态截图要包含什么？", "注册证能代替截图吗？"),
        _fixed("要截商标现在的状态。图上能看清商标名、注册号和状态就行。"),
        checkpoint_pattern=r"(?:截图|注册证|商标状态|商标)",
    ),
    _Family(
        "saved", "statement",
        "用户说只是保存了、还没有提交",
        ("只是保存了。", "只保存还没提交。"),
        _fixed("还得点一下提交。只保存的话，材料还没交上去。"),
        checkpoint_pattern=r"(?:提交|补交|保存|审核)",
        require=r"(?:保存|没提交|尚未提交|未提交)",
    ),
    _Family(
        "submitted", "statement",
        "用户说已经提交或重新提交，正在等提示",
        ("已经提交了。", "重新提交了。"),
        _fixed("既然已经提交了，您就留意这笔申请有没有新消息。"),
        checkpoint_pattern=r"(?:提交|补交|审核)",
        require=r"(?:提交|补交)",
    ),
    _Family(
        "in_review", "question",
        "用户问审核进度、是不是还在审核中",
        ("还在审核吗？", "审核到哪一步了？"),
        _fixed("我这边查不到审到哪儿了。您看申请页上最新的提示就行。"),
        checkpoint_pattern=r"(?:审核|进度)",
        checkpoint_required=False,
        require=r"(?:审核中|审核进度|审核)",
    ),
    _Family(
        "handoff", "question",
        "用户问材料交到哪、下一步怎么交，或问能不能催办",
        ("材料交到哪？", "能催一下吗？"),
        _fixed("材料照工单页面的提示补就行。催到哪一步，我这边看不到，您可以看看页面有没有新提示。"),
        checkpoint_pattern=r"(?:补充材料|页面提示|下一步|交付|催办)",
        require=r"工单",
    ),
    _Family(
        "connectivity", "question",
        "用户说断网、上不了网，或问网络完全不通怎么办",
        ("上不了网怎么办？", "家里断网了。"),
        lambda card, _point: _connectivity_reply(card),
        checkpoint_pattern=r"(?:判断|核查|评估|排查|故障)",
        require=r"(?:断网|上不了网|光猫|指示灯)",
    ),
    _Family(
        "slow", "question",
        "用户说网速慢、卡顿、信号差或间歇掉线",
        ("网速很慢怎么办？", "一直卡顿。"),
        lambda card, _point: _slow_reply(card),
        checkpoint_pattern=r"(?:判断|核查|评估|排查)",
        require=r"(?:网速|卡顿|弱信号|间歇掉线)",
    ),
    _Family(
        "router", "question",
        "用户问路由器怎么重启、后台怎么进或能否恢复出厂",
        ("路由器怎么重启？", "后台怎么进？"),
        lambda card, _point: _router_reply(card),
        checkpoint_pattern=r"(?:下一步|排查|重启|设备|后台)",
        require=r"(?:Reset|恢复出厂|指示灯|光猫)",
    ),
    _Family(
        "device_check", "question",
        "用户问怎么判断是一台设备还是多台设备有问题",
        ("怎么判断是一台还是多台设备？", "只有一台手机上不了网。"),
        lambda card, _point: _device_check_reply(card),
        checkpoint_pattern=r"(?:判断|核查|评估|排查|设备)",
        require=r"(?:一台|多台)",
    ),
    _Family(
        "optical_light", "question",
        "用户问光猫亮红灯、光信号或 LOS 灯亮红灯怎么办",
        ("光猫亮红灯怎么办？", "光信号灯是红的。"),
        lambda card, _point: _optical_light_reply(card),
        checkpoint_pattern=r"(?:光信号|指示灯|光猫|信号|判断)",
        require=r"(?:光信号|LOS)",
        require_all=(r"(?:红灯|指示灯)",),
    ),
    _Family(
        "repair", "question",
        "用户问怎么报修、能不能派人上门或派单",
        ("报修找谁？", "你们能派人上门吗？"),
        lambda card, _point: _repair_reply(card),
        checkpoint_pattern=r"(?:报修|下一步|检修|派单|办理)",
        require=r"(?:报修|派单|上门)",
        require_all=(r"(?:运营商|报修|派单)",),
    ),
    _Family(
        "price", "question",
        "用户问套餐价格、月费或怎么收费",
        _price_examples,
        lambda card, _point: _price_reply(card),
        checkpoint_pattern=r"(?:费用|价格|月费|套餐|收费)",
        require=r"(?:元/月|元每月|月费|套餐)",
    ),
    _Family(
        "recommend", "question",
        "用户问推荐哪个套餐、哪个适合自己",
        ("推荐哪个套餐？", "哪个适合我？"),
        lambda card, _point: _recommend_reply(card),
        checkpoint_pattern=r"(?:推荐|建议|取舍)",
        require=r"(?:套餐|推荐|适合)",
    ),
    _Family(
        "install", "question",
        "用户问什么时候能安装、能不能预约或覆盖范围",
        ("什么时候能装？", "能预约吗？"),
        lambda card, _point: _install_reply(card),
        checkpoint_pattern=r"(?:安装|预约|覆盖|上门)",
        require=r"安装",
    ),
    _Family(
        "contract", "question",
        "用户问有没有合约、合约多久、提前取消要赔多少",
        ("有合约吗？", "提前取消要赔多少？"),
        lambda card, _point: _contract_reply(card),
        checkpoint_pattern=r"(?:合约|退出|条件|费用)",
        require=r"(?:合约|违约|提前结束)",
        require_all=(r"(?:合约|退出成本)",),
    ),
    _Family(
        "coverage", "question",
        "用户问能不能覆盖全屋、面积大要不要加钱",
        ("能覆盖全屋吗？", "面积大要加钱吗？"),
        lambda card, _point: _coverage_reply(card),
        checkpoint_pattern=r"(?:覆盖|安装|条件)",
        require=r"(?:覆盖|面积|墙体|平方米)",
        require_all=(r"(?:平方米|覆盖)",),
    ),
    _Family(
        "promotion", "question",
        "用户问有没有优惠、折扣、赠送或特殊审批",
        ("有优惠吗？", "能打折吗？"),
        lambda card, _point: _promotion_reply(card),
        checkpoint_pattern=r"(?:优惠|折扣|费用|条件)",
        require=r"(?:优惠|折扣|赠送)",
        require_all=(r"(?:优惠|折扣|赠送)",),
    ),
    _Family(
        "logistics_status", "question",
        "用户问快递到哪了、物流为什么一直没更新或签收异常",
        ("到哪了？", "物流一直没更新。"),
        lambda card, _point: _logistics_status_reply(card),
        checkpoint_pattern=r"(?:运单|物流|状态|签收)",
        require=r"(?:运单|物流|快递)",
    ),
    _Family(
        "logistics_contact", "question",
        "用户问快递员或官方客服的联系方式",
        ("快递员电话多少？", "怎么联系客服？"),
        lambda card, _point: _logistics_contact_reply(card),
        checkpoint_pattern=r"(?:快递员|客服|联系)",
        require=r"(?:快递员|承运|客服)",
        require_all=(r"(?:运单|物流|快递)",),
    ),
    _Family(
        "logistics_address", "question",
        "用户问能不能改地址、地址写错了怎么办",
        ("能改地址吗？", "地址写错了。"),
        lambda card, _point: _logistics_address_reply(card),
        checkpoint_pattern=r"(?:地址|签收|修改)",
        require=r"(?:改地址|承运方|地址)",
        require_all=(r"(?:运单|物流|快递|承运)",),
    ),
    _Family(
        "lost_or_damaged", "question",
        "用户说快递一直没收到、怀疑丢件或显示签收异常",
        ("快递一直没收到。", "是不是丢件了？"),
        lambda card, _point: _lost_reply(card),
        checkpoint_pattern=r"(?:签收|运单|物流|状态)",
        require=r"(?:未收到|没收到|丢件|签收异常)",
        require_all=(r"(?:运单|物流|快递)",),
    ),
    _Family(
        "invoice_type", "question",
        "用户问普通发票和专用发票的区别、该开哪种",
        ("普票和专票有什么区别？", "我要开专票。"),
        lambda card, _point: _invoice_type_reply(card),
        checkpoint_pattern=r"(?:普通发票|专用发票|类型)",
        require=r"(?:发票类型|普通发票|专用发票)",
    ),
    _Family(
        "invoice_header", "question",
        "用户问发票抬头怎么填、抬头写错了怎么办",
        ("抬头写错了怎么办？", "发票抬头要填什么？"),
        lambda card, _point: _invoice_header_reply(card),
        checkpoint_pattern=r"(?:抬头|主体信息)",
        require=r"抬头",
    ),
    _Family(
        "invoice_status", "question",
        "用户问开票到哪一步了、能不能重开或作废",
        ("开票到哪一步了？", "能重开吗？"),
        lambda card, _point: _invoice_status_reply(card),
        checkpoint_pattern=r"(?:开票状态|重开|作废|申请)",
        require=r"(?:开票|作废|重开|申请记录)",
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


def generate_ticket_candidates(card: TaskCard, *, limit: int | None = None,
                               exclude: Iterable[str] | None = None) -> list[Candidate]:
    """Return the deterministic question families that apply to this card.

    ``exclude`` follows ``TICKET_FORECAST_EXCLUDE`` when omitted.  Exclusions
    are an explicit audit decision; nothing is dropped automatically.
    """
    cap = max(1, limit if limit is not None else max_candidates_from_env())
    excluded = (frozenset(part.strip().lower() for part in exclude if part.strip())
                if exclude is not None else excluded_intents_from_env(card))
    text = card_text(card)
    candidates: list[Candidate] = []
    for family in _FAMILIES:
        if len(candidates) >= cap:
            break
        if family.intent in excluded:
            continue
        if family.require and not re.search(family.require, text):
            continue
        if any(not re.search(pattern, text) for pattern in family.require_all):
            continue
        point = _checkpoint(card, family.checkpoint_pattern, family.checkpoint_exclude)
        # A checkpoint-anchored family without a matching checkpoint would be
        # guessing at business content the card does not contain.
        if family.checkpoint_required and family.checkpoint_pattern and point is None:
            continue
        reply = family.reply(card, point)
        if not reply:
            continue
        examples = family.examples(card) if callable(family.examples) else family.examples
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
