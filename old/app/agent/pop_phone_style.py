"""Reviewed, opt-in telephone wording. The live agent never calls a rewrite model."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .pop_contextual import CONFIRM_QUESTION
from .pop_turn_intent import grounded_followup

PROMPT = """给电商平台写一条打给商家的中文电话话术，TTS 会逐字读。根据这一轮的目的写两种说法。
用“您”。先说商家现在需要的信息，用一到两句短句。每轮最多问一个问题。用电话里常说的动作词，不加寒暄、套话、语气词、标题或列表。
例如：“如需查看办理条件，您可前往详情页”可以说成“您打开详情页，就能看到办理条件。”例子只说明口吻。
保留输入给出的业务事实和限制。缺信息就只说无法确认。别自行承诺、添加操作或声称已经办成。复审、提交、记录分开说。
输出 JSON 对象 {"a":"第一种说法","b":"第二种说法"}。值只填会在电话里读出的句子，两种说法用不同句式。"""

SOURCES = {
    "available": "请问您现在方便沟通资质审核吗？",
    "available_again": "您现在方便听一下资质审核结果吗？",
    "identity_again": "您可以直接回答是的，或者不是。",
    "understood": CONFIRM_QUESTION,
    "offer_end": "结果以复审为准。您听明白整改办法了吗？",
    "notice": grounded_followup("channel", "", "")[0],
    "review": grounded_followup("review", "", "")[0],
    "unknown": grounded_followup("unknown_detail", "", "")[0],
    "retry": "您要我再试一次记录吗？",
}
# Verifiable wording anchors supplement review of the full spoken text.
REQUIRED = {
    "available": ("现在", "方便", "资质审核", "？"),
    "available_again": ("现在", "方便", "审核结果", "？"),
    "identity_again": ("负责", "？"),
    "understood": ("怎么", "？"),
    "offer_end": ("复审", "怎么", "？"),
    "notice": ("京麦", "原申请", "书面", "意见"),
    "review": ("提交", "复审", "结果"),
    "unknown": ("确认",),
    "retry": ("再试", "？"),
}
BRIEFS = {
    "available": "已经确认负责人。问现在方不方便听这笔资质审核的情况。",
    "available_again": "还没有问出对方能不能聊。简短再问现在是否方便听审核结果。",
    "identity_again": "接听人身份还不明确。问这笔申请是不是他在负责。别透露任何审核信息。",
    "understood": "刚讲清下一步。只问商家是否清楚接下来怎么改，语气自然。",
    "offer_end": "说明是否通过由复审决定，然后问商家是否知道下一步怎么改。",
    "notice": "对方问书面意见在哪。请商家打开京麦，进入这笔原申请，在里面看书面意见。",
    "review": "对方问交完资料是不是就过了。重新提交后仍会复审，能否通过由复审结果确定。",
    "unknown": "电话里没法确认对方问的那项具体信息。简单说明当前无法确认，别加其他建议。",
    "retry": "刚才已经说明这次记录失败。只问要不要现在再试一次记录。",
}
FORBIDDEN = re.compile(r"[0-9a-zA-Z#*_<>\n]|一定(?:能)?通过|保证通过|已经通过|替您提交|已(?:登记|记录)")


@dataclass(frozen=True)
class PhoneStyle:
    version: str
    replacements: dict[str, str]

    def render(self, sentences):
        result = []
        for sentence in sentences:
            if sentence == SOURCES["identity_again"] and result and "负责人" in result[-1]:
                # The immediately preceding identity question already explains what to answer.
                result[-1] = self.replacements[sentence]
            else:
                result.append(self.replacements.get(sentence, sentence))
        return result


def load_phone_style(path):
    raw = path.read_bytes()
    replies = json.loads(raw.decode("utf-8"))["replies"]
    if set(replies) != SOURCES.keys():
        raise ValueError("Phone style keys differ from the supported lines")
    for key, line in replies.items():
        if (not isinstance(line, str) or not 5 <= len(line) <= 65 or line != line.strip()
                or FORBIDDEN.search(line) or line.count("？") > 1
                or not all(anchor in line for anchor in REQUIRED[key])):
            raise ValueError(f"Invalid phone style line: {key}")
    return PhoneStyle("pop-phone-" + sha256(raw).hexdigest()[:12],
                      {original: replies[key] for key, original in SOURCES.items()})
