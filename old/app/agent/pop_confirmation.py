"""Conservative evidence for consent and result confirmation in a POP call."""
from __future__ import annotations

import re

CONTINUE_SPEECH = r"(?:(?:您|你)?请?(?:继续)?(?:说|讲)(?:吧|啊|的)?|继续)"
AVAILABLE_NOW = re.compile(
    rf"(?:(?:我)?(?:现在|这会儿)?(?:方便(?:听)?|有空|有时间|不忙|可以(?:听|说|讲)?|没问题)(?:的|啊|呀)?"
    rf"(?:{CONTINUE_SPEECH})?|{CONTINUE_SPEECH}(?:我(?:现在|这会儿)?(?:方便|有空))?)"
)
OWN_ROLE = re.compile(
    r"(?:(?:对的?|是的?|没错))?(?:确实)?(?:(?:这边|店里|这家店的)?负责人(?:就)?是我|是我本人|"
    r"(?:我)?(?:就是|是)(?:这|本|该)(?:家)?店(?:铺|里)?的?(?:负责人|店主|老板))(?:啊|呀)?(?:请说|您请说|请讲)?"
)
STOP_NOW = re.compile(r"^(?:先)?(?:别|不要|不用)(?:再|继续)?(?:说|讲|打扰|联系)(?:了|啦)?(?:谢谢)?$")
LISTENING = re.compile(r"(?:(?:嗯|哦|噢){1,4}|我在听)")
NEEDS_ACTION_HELP = re.compile(
    r"(?:我)?(?:也|还|还是)?(?:不(?:太)?(?:知道|清楚|明白)"
    r"|没(?:弄|搞|听)?(?:清楚|明白|懂))"
    r"(?:(?:下一步|接下来)?怎么(?:改|补|弄|做|办|处理))?(?:啊|呀|呢)?"
)

COMPREHENSION = (
    r"(?:行|好(?:的)?|对)?(?:这次)?(?:我)?(?:已经|都)?"
    r"(?:(?:听明白|听清楚|听懂|明白|理解|清楚|知道|懂)(?:这笔(?:申请)?的)?(?:整改办法|处理办法)?(?:了|啦)?"
    r"|知道(?:怎么|如何)(?:补(?:材料)?|操作|处理)了)"
)
REFERENCE = r"按(?:照)?(?:(?:您|你)说的|(?:这笔申请的|本次|原申请的)?要求|(?:页面|原申请)提示)"
LOCATION = (
    r"(?:(?:回(?:到)?|在|到|从)(?:京麦(?:原申请)?|原申请|(?:这|那)笔申请|申请(?:页|页面))(?:里|上)?|回去)"
)
ACTION = r"(?:去|再|先)?(?:补(?:交|传)?(?:材料|文件)?|更新(?:材料|文件)?|重新(?:提交|上传)|提交|上传|修改|更正|处理)"
NEXT_STEP = (
    rf"(?:我)?(?:等会儿|等下|一会儿|稍后|晚点|回头|过会儿)?(?:我)?(?:会|就|打算|先)?(?:自己)?"
    rf"(?:{REFERENCE}(?:{LOCATION})?|{LOCATION}(?:{REFERENCE})?){ACTION}"
)
CONFIRM_PLAN = re.compile(rf"{COMPREHENSION}(?:{NEXT_STEP})?(?:谢谢(?:您|你)?|多谢)?")


def explicit_understanding(normalized: str) -> bool:
    return CONFIRM_PLAN.fullmatch(normalized) is not None
