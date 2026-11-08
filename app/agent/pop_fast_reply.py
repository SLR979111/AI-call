"""Answer known knowledge gaps with a meaningful first sentence."""
from __future__ import annotations

import re

PORTAL = re.compile(r"(?:京麦|金脉|商家后台|店铺后台).{0,20}(?:app|网页|软件|客户端|下载|安装|叫啥|叫什么|从哪|怎么进)", re.I)
FORMAT = re.compile(r"截图|拍屏|复印件|扫描件|(?:盖.{0,3}章|纸).{0,14}电子|电子.{0,14}(?:纸|盖.{0,3}章)|拍哪几页")
UI = re.compile(r"暂存.{0,15}提交|提交.{0,15}暂存|哪(?:一)?栏|哪个菜单|挪到哪|页面.{0,12}改版")
DEADLINE = re.compile(r"赶.{0,12}(?:活动|店庆|开业)|(?:多久|哪天|几天|啥时候).{0,6}(?:有结果|出结果)|"
                      r"(?:今晚|今天).{0,10}(?:完|过审|有结果)|(?:晾|熬|等).{0,8}(?:哪天|啥时候|什么时候)|"
                      r"(?:活动|店庆|开业|开店).{0,12}(?:赶得上|准点|来得及)|催.{0,4}(?:审核|进度)")
APPROVAL_OVERRIDE = re.compile(r"(?:口头|嘴上|先说).{0,20}(?:审核|系统).{0,8}(?:过了|通过)|"
                               r"(?:标成|写成|标记).{0,8}(?:通过|过审)")


def fast_reply(text: str, action: str) -> tuple[str, list[str]] | None:
    if APPROVAL_OVERRIDE.search(text):
        return "fast_workflow", ["我不能宣称审核已通过，实际结果以平台审核为准。"]
    if PORTAL.search(text):
        return "fast_portal", ["京麦是商家后台，请在这笔原申请里查看书面驳回意见。",
                               "具体客户端和界面我无法核实，找不到入口时可联系京麦审核支持。"]
    if FORMAT.search(text):
        return "fast_material_format", ["我无法确认您手头这份材料的格式是否可用。",
                                        f"本单要求是{action.rstrip('。')}。请在京麦原申请核对书面意见。"]
    if UI.search(text):
        return "fast_page", ["我看不到您的页面，无法确认具体按钮或栏目。",
                              "请按京麦原申请的页面提示处理，找不到入口时可联系审核支持。"]
    if DEADLINE.search(text):
        return "fast_review_time", ["我无法核实复审完成的时间。您可以在京麦原申请查看进度。"]
    return None
