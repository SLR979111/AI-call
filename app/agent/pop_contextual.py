"""Handle knowledge gaps and multi-turn repairs after merchant verification."""
from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter

from .pop_guidance import guidance_reply, guidance_topics
from .pop_fast_reply import fast_reply
from .pop_repair import needs_replay
from .pop_resilient import evidence_reply

DEFINITION = re.compile(r"(?:资质审核|资审).{0,8}(?:是什么|做什么|干什么|什么意思)|什么(?:叫|是)(?:资质审核|资审)")
REVIEW_MEANING = "资质审核是核对开店或经营所需的证明材料，比如证照或授权文件。"
CONTROL = re.compile(r"忽略.{0,8}(?:规则|指令|要求)|(?:规则|规定|指令)作废|(?:系统|内部)提示词|"
                     r"(?:标记为|改成|判成|设为).{0,7}(?:通过|过审)|替我结案")
CREDENTIALS = re.compile(r"验证码|登录密码|短信校验码|替我登录|帮我登录")
LOGISTICS = re.compile(r"快递|物流|没送到|配送|运单")
AFTERSALE = re.compile(r"退货|退款|退运费|买家.{0,10}运费|售后")
OTHER_APPLICATION = re.compile(r"另(?:外)?(?:一|个|家|笔).{0,4}(?:店|申请|单)|别家店|其他店铺|别的申请")
PRIVATE_REQUEST = re.compile(r"(?:号码|电话|手机号).{0,6}(?:全号|全念|念出来)|完整.{0,6}(?:手机号|号码|电话)")
EXPIRY_DETAIL = re.compile(r"(?:为啥|为什么|怎么).{0,10}(?:失效|到期|过期)|"
                           r"(?:哪份|哪张|哪天|几号|具体时间|有效期).{0,9}(?:过期|失效|到期)|"
                           r"(?:过期|失效|到期).{0,24}(?:哪份|哪张|哪天|具体时间|几号|昨晚|上周)|"
                           r"(?:两张|一张.{0,12}一张).{0,16}(?:不会挑|选哪|用哪)")
EXACT_UI = re.compile(r"(?:左上|右上|左下|右下)|(?:按钮|菜单).{0,8}(?:叫什么|第几个|什么颜色)")
HELP = re.compile(r"不会(?:填|弄|改|操作|修改)|不会(?=[。！？，,!?\s]*$)|还是.{0,6}(?:不懂|不明白|搞不懂)|"
                  r"不知道.{0,4}(?:从哪|如何|怎么|选哪)|带着我.{0,5}(?:走|操作)")
FOUND = re.compile(r"^(?:(?:我)?(?:已经)?(?:找到|打开|看见|看到)了)[。！!，,\s]*$")
BLOCKED = re.compile(r"(?:都|也)?不能点|啥都.{0,3}不了|怎么点都.{0,4}(?:不行|没反应)")
SPLIT_CLAUSES = re.compile(r"[，。；！？,;!?]|再(?:说|问)一下|再说下")
CONFIRM_QUESTION = "您清楚这笔申请的整改办法了吗？"


@dataclass
class FollowupContext:
    step: str = ""
    can_confirm: bool = True
    focus: str = ""
    misses: int = 0


async def reply(text, progress, *, reason, action, doc_id, semantic_topics, fast=False, resilient=False,
                scene_policy=None):
    """Return speech, an auditable route, and progress toward a clear result."""
    def answer(lines, route, *, topics=(), step="", can_confirm=False, focus="", misses=0, **telemetry):
        return lines, {"source": route, "topics": list(topics), **telemetry}, FollowupContext(step, can_confirm, focus, misses)

    if CONTROL.search(text):
        return answer(["我无法修改审核结果。请通过京麦原申请查看书面意见。"], "workflow_protected")
    if CREDENTIALS.search(text):
        return answer(["请不要提供密码或验证码。我无法代您登录或提交，请在自己的京麦账号里查看原申请。"],
                      "credentials_protected")
    if OTHER_APPLICATION.search(text):
        return answer(["我这里只能处理本次核验的申请。请由对应店铺的负责人在京麦查询那笔申请。"], "outside_application")
    if AFTERSALE.search(text):
        return answer(["退款或售后请在对应订单里查看售后详情或联系该订单的客服。"], "outside_aftersale")
    if LOGISTICS.search(text):
        return answer(["这通电话无法查物流。请打开对应的京东订单，查看物流信息。"], "outside_logistics")
    if DEFINITION.search(text):
        return answer([REVIEW_MEANING, "您想问的是这笔申请里的哪一步？"], "explained_definition")
    if EXPIRY_DETAIL.search(text) and any(word in reason for word in ("到期", "过期", "失效")):
        return answer(["我能看到这笔申请的审核记录显示材料已过期，但无法确认是哪份文件、哪一天过期。",
                       "请在京麦原申请查看书面意见，核对相应证照或授权文件上的有效期。"], "missing_expiry_detail")
    if EXACT_UI.search(text):
        return answer(["我看不到您当前的页面，无法确认按钮在哪个位置。",
                       "请在京麦找到原申请，按该页面的提示处理。找不到入口时，可通过京麦联系审核支持。"],
                      "unverified_interface")
    if doc_id == "pop-scene-023" and re.search(r"(?:图案|图形).{0,12}文字|文字.{0,12}(?:图案|图形)", text):
        return answer(["您可以在权大师用商标注册号查询。文字商标会显示名称，图形商标会显示图形。"],
                      "scoped_qa", citation="特殊场景!A15:D15")

    topics = guidance_topics(text)
    if topics == ("private",) or PRIVATE_REQUEST.search(text):
        public = tuple(dict.fromkeys(topic for part in SPLIT_CLAUSES.split(text) for topic in guidance_topics(part)
                                     if topic not in ("private", "approval") and not PRIVATE_REQUEST.search(part)))
        return answer(guidance_reply(("private",) + public, reason=reason, action=action), "privacy_then_business",
                      topics=("private",) + public)
    if topics == ("approval",):
        return answer(guidance_reply(topics, reason=reason, action=action), "no_approval_promise", topics=topics)
    if resilient:
        known = evidence_reply(text, focus=progress.focus, reason=reason, action=action)
        if known:
            return answer(list(known.sentences), known.route, focus=known.focus)
    if fast:
        ready = fast_reply(text, action)
        if ready:
            route, lines = ready
            return answer(lines, route)
    if scene_policy and "blocked" not in topics:
        scoped = scene_policy.reply(doc_id, text, action, progress.focus)
        if scoped:
            intent, lines = scoped
            return answer(lines, f"scene_{intent}", focus=f"scene_{intent}",
                          citation=doc_id, scene_policy=scene_policy.version)
    if "blocked" in topics or BLOCKED.search(text) or (progress.step == "read_notice" and HELP.search(text)):
        if progress.step or re.search(r"(?:打开|找到)了", text):
            return answer(["如果这笔申请仍无法修改或重新提交，请通过京麦联系审核支持，说明您已经做到哪一步。"],
                          "blocked_support", topics=("blocked",))
        return answer(guidance_reply(("blocked",), reason=reason, action=action), "blocked_support", topics=("blocked",))
    if progress.step in {"open_application", "locate_problem"} and FOUND.fullmatch(text):
        return answer(["打开书面驳回意见后，请按当单要求处理。", f"具体需要{action.removeprefix('请').rstrip('。')}。",
                       "您看得到这条驳回意见吗？"], "next_after_open", topics=("action",), step="read_notice")
    if HELP.search(text):
        if progress.step == "locate_problem":
            return answer(["您可以在京麦联系审核支持，请对方和您一起核对这笔原申请。"], "repeated_help_support")
        if progress.step == "open_application":
            return answer(["您是找不到原申请，还是已经打开了、但不知道改哪一项？您说一下是哪一种。"],
                          "narrow_help", step="locate_problem")
        return answer(["请先打开京麦，找到被驳回的原申请。找不到的话，您可以先告诉我。"],
                      "help_first_step", step="open_application")
    if needs_replay(text):
        summary = "审核时发现，您提交的开店主体证明材料已过有效期。" if reason == "主体资质到期" else f"这笔申请退回的问题是{reason}。"
        return answer([summary, "请先在京麦原申请看书面驳回意见。", "您是想先弄清哪份材料有问题，还是想了解怎么修改？"],
                      "plain_rephrase", topics=("reason", "channel"), step="open_application")

    source, telemetry = "grounded_followup", {}
    if not topics and semantic_topics and len(text) >= 8 and re.search(r"[？?]|吗|咋|怎么|哪|啥|为何", text):
        started = perf_counter()
        inferred, model_status = await semantic_topics(text)
        telemetry = {"model_status": model_status, "semantic_ms": round((perf_counter() - started) * 1000, 1)}
        # Never infer a sensitive request or a compound plan from an ambiguous utterance.
        if len(inferred) == 1 and inferred[0] in {"channel", "process", "material", "submit", "blocked", "review"}:
            topics, source = inferred, "model_grounded_followup"
    if topics:
        return answer(guidance_reply(topics, reason=reason, action=action) + [CONFIRM_QUESTION],
                      source, topics=topics, can_confirm=True, focus=topics[0], **telemetry)
    if resilient:
        if progress.misses:
            return answer(["这个问题还需要核对。您可以在京麦联系审核支持，请对方和您一起查看这笔申请。"],
                          "support_after_clarification", focus=progress.focus, misses=progress.misses, **telemetry)
        return answer(["我没有足够信息核实这件事。您是想查这笔申请的材料要求、提交入口，还是审核进度？"],
                      "focused_clarification", focus=progress.focus, misses=1, **telemetry)
    return answer(["您具体想问哪一步，是要准备哪份材料，还是想知道怎么处理这笔申请？"],
                  "focused_clarification", **telemetry)
