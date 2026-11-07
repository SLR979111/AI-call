from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from .pop_audit import HOTLINE, PopAuditAgent


_OPENING = "您好，我是京东商家资质审核 AI 专员，本次通话会录音。请问您是店铺负责人吗？"
_VERIFY = "在说明单据之前需要先确认身份。请问您是这家店铺的负责人吗？"
_CLOSE = "感谢接听，再见。"
_OPT_OUT = re.compile(r"别再?(打|联系|回电)|不要(再)?联系|不(用|要|需要)回电|不用再打|先挂了|不接受.{0,4}录音")
_WRONG_PERSON = re.compile(
    r"打错|找错|不认识.{0,5}(这家|店铺)|不是.{0,12}(负责人|申请人|本人|这家店)|不是我(的|负责)?|我不是"
    r"|(?:店主|负责人|申请人)(?:现在|暂时|目前)?不在|我(?:只是|是).{0,5}(?:前台|代接|家属|同事)"
)
_IDENTITY_YES = re.compile(
    r"(?:(?:嗯|对的?|是的?|没错|您好|你好)[，,、\s]+)?"
    r"(?:我(?:就)?是(?:(?:这家|该|本)?店铺?的?|店里的|这边的)?(?:负责人|申请人|店主|法人|老板)"
    r"|本人在接听|是本人)(?=$|[。！!，,；;、\s])"
)
_CALLBACK = re.compile(
    r"(?:请|麻烦|帮我|给我|能|可以|希望|需要|要求|想|安排|登记|记得).{0,8}(?:回电|回拨|回个?电话)"
    r"|(?:回电|回拨)(?:吧|给我|一下|好吗|.{0,6}(?:再登记|再安排))"
    r"|再打(?:来|给我|一次)|打回来|(?:晚点|稍后|明天|过会儿|待会).{0,5}(?:打|联系)|安排.{0,4}电话"
)
_NO_CALLBACK = re.compile(r"(?:不用|不要|不需要|不必|不安排|不想|别|无需)(?:再|安排|给我|帮我){0,3}(?:回电|回拨|打电话)")
_CALLBACK_STATUS = re.compile(r"回电|回拨|工单|登记")
_RETRY_CALLBACK = re.compile(r"(?:再|重新)(?:登记|安排|试一下|试一次).{0,8}(?:回电|回拨)|(?:回电|回拨).{0,8}(?:再|重新)(?:登记|安排)")
_RISK_CALLBACK = re.compile(r"曝光|媒体|公开.{0,5}(?:平台|问题)|越级|投诉到|12315|监管部门|市场监管")
_HUMAN = re.compile(r"转人工|找人工|接人工|人工客服|人工专员|投诉")
_REPEAT = re.compile(r"没听清|听不清|能再说|再说一遍")
_ACK = re.compile(r"(?:嗯[,，]?)?(?:好的?|我?知道了|我?明白了?|清楚了)(?:[。！!，,\s]|谢谢|再见)*")
_FOLLOWUP_CLOSE = re.compile(r"(?:谢谢|多谢)[，,。 ]*(?:我(?:已经)?(?:知道|明白)了)?[。！! ]*")
_FORMER_CONTACT = re.compile(r"(?:已经|已|早就|刚)离职|不再负责(?:这家|这间|该)?(?:店|申请)")
_CALLBACK_FOLLOWUP = re.compile(r"没安排上|安排不了|联系渠道|什么渠道|怎么联系|记在.{0,8}(?:申请|单)名下")
_ASK_APPROVAL = re.compile(r"(?:能|可|会|包|保|必|就|保证).{0,4}(?:过审|通过)|包过|就能过")
_ASK_CHANNEL = re.compile(r"(?:哪个|什么)(?:入口|页面|位置|渠道)|(?:在哪|从哪|去哪里|在哪里).{0,10}(?:交|传|提交|看|找)")
_ASK_REASON = re.compile(r"具体原因|为什么|卡在哪|(?:不符|对不上|不一致).{0,8}(?:对吗|是吗|原因)")
_ASK_ACTION = re.compile(r"怎么|做什么|补.{0,5}(?:材料|资质)|应该先|重新提交|再提交|上传|证照|更换")
_ASK_PRIVATE = re.compile(r"(?:完整|全部|全号).{0,10}(?:电话|号码|证件)|(?:电话|号码|证件).{0,5}(?:全号|完整)")

# Concise actions from the primary scene table and accompanying Q&A.
# Missing concrete values (for example a replacement document status) remain unspecified.
_SOURCE_ACTIONS = {
    "授权方未回复": "向品牌方确认授权回复，核实后按驳回意见重新提交",
    "授权方联系不上": "与品牌方确认可核实的授权联系信息，再提交",
    "授权核实无果": "补充可核实的授权证明或在职证明，并请品牌方配合核实",
    "品牌方明确告知假授权": "与品牌方确认授权无误后再重新提交真实授权",
    "持有人不一致（经营品牌与新增品牌持有人）": "在京麦的品牌管理中先停用原经营品牌，再新增品牌",
    "持有人不一致": "核对商标网的实际持有人，修正持有人信息后重新提交",
    "主体资质列严列异": "联系当地工商部门处理经营异常或严重违法失信记录，移除后再提交",
    "主体资质到期": "更新已失效的主体资质，上传有效材料后重新提交",
    "主体名称": "核对营业执照号码和公司名称，按证照上的信息修改后重新提交",
    "法人证件类型": "选择正确的法人证件类型，并上传与所选类型相符的证件",
    "法人证件信息错误": "核对并改正填写的法人证件信息后重新提交",
    "税务信息不符": "核对商家信息，改为与该主体一致的税务识别号",
    "证件信息不符": "核对营业执照信息和上传的图片，改为一致后再提交",
    "医药类目链路不完整": "补充购货发票或完整授权链路",
    "授权书不符": "上传品牌方出具、主体和品牌匹配且符合要求的授权书",
    "授权书内容不完整": "补齐驳回意见列明的盖章、授权声明或必要资质材料后重新提交",
    "资质上传有误": "核对驳回意见，补传缺失资质或更换上传错误的材料",
    "资质与类目不符": "上传覆盖所申请类目的资质材料，或调整申请类目",
    "资质已过期": "更新过期的资质，上传有效材料后重新提交",
    "资质不全": "按驳回意见补齐缺少的对应资质材料后重新提交",
    "无品牌": "当前类目或店铺类型不支持无品牌，改用符合要求且获授权的品牌",
    "缺少资质": "按驳回意见补齐缺少的公章、流水或品牌资质后重新提交",
    "类似群不符合": "更换为商标有效类似群覆盖的类目或更换品牌后重新提交",
    "品牌不一致": "更换品牌或调整类目，使品牌与所申请类目一致后再提交",
    "特殊事业部不支持申请": "所属事业部暂不支持自行提交产品线；如需新增，请联系对接采销负责人",
    "品牌混淆": "通过京麦核实是否对外招商或属于邀请制，再选择可申请品牌",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


@dataclass
class _Call:
    challenged: bool = False
    verified: bool = False
    closed: bool = False
    transferred: bool = False
    pending_callback: str = ""
    callback_attempted: bool = False
    ticket: str = ""
    explained: bool = False
    request_id: str | None = None


class PopStrategyAgent(PopAuditAgent):
    """Opt-in rule policy used by the isolated POP text harness."""

    def __init__(self, knowledge_path: Path, pause_ms: int, *, identity_first: bool,
                 case_grounding: bool, callback_receipt: bool, answer_followups: bool = False) -> None:
        super().__init__(knowledge_path, pause_ms)
        self.identity_first = identity_first
        self.case_grounding = case_grounding
        self.callback_receipt = callback_receipt
        self.answer_followups = answer_followups
        self.calls: dict[str, _Call] = {}

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        self.calls[session_id] = _Call()

    def _find_case_doc(self, context: dict[str, Any]) -> dict[str, Any] | None:
        keys = {level: _normalize(str(context.get(f"reject_{level}") or "")) for level in ("l1", "l2", "l3")}
        if not keys["l3"]:
            return None
        matches = [doc for doc in self.knowledge.docs if all(
            not key or _normalize(str((doc.get("scene") or {}).get(level, ""))) == key
            for level, key in keys.items()
        )]
        if not matches or len({doc["scene"].get("mode") for doc in matches}) != 1:
            return None
        return matches[0]

    @staticmethod
    def _source_action(reason: str) -> str:
        normalized = _normalize(reason)
        for label, action in _SOURCE_ACTIONS.items():
            if _normalize(label) in normalized:
                return action
        return PopAuditAgent._remediation(normalized)

    async def _callback(self, req: dict[str, Any], call: _Call, cancel: CancelToken, tools: Any,
                        request_id: str) -> tuple[list[str], str, dict[str, Any]]:
        call.callback_attempted = True
        if not call.ticket and re.fullmatch(r"Q[0-9]{6,}", request_id):
            cancel.check()
            key = hashlib.sha256(f"{req['session_id']}|callback.create|{request_id}".encode("utf-8")).hexdigest()
            try:
                returned = await tools.invoke(
                    "callback.create", {"request_id": request_id, "reason": "商家要求回电"},
                    idempotency_key=f"sha256:{key}", session_id=req["session_id"],
                    turn_id=req.get("turn_id", "t_0000"), trace_id=req.get("trace_id"),
                )
            except (TimeoutError, OSError):
                returned = {}
            data = returned.get("data") or {}
            if returned.get("ok") is True and data.get("request_id") == request_id and data.get("status") == "scheduled":
                if isinstance(data.get("ticket_id"), str) and data["ticket_id"].strip():
                    call.ticket = data["ticket_id"]
            cancel.check()
        return self._callback_status(call)

    @staticmethod
    def _callback_status(call: _Call) -> tuple[list[str], str, dict[str, Any]]:
        if call.ticket:
            return ["已为您登记回电任务，后续由资质审核专员跟进。"], "callback_scheduled", {"ticket": call.ticket}
        return [f"这次没能登记回电。您可通过京麦联系审核支持，或拨打{HOTLINE}。"], "callback_failed", {}

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", "")).strip()
        session_id = req.get("session_id", "s_dev")
        reply_id = req.get("reply_id", "r_dev_1")
        context = req.get("business_context") or {}
        call = self.calls.setdefault(session_id, _Call())
        cancel.check()
        request_id = str(context.get("request_id") or "")
        if call.request_id is None:
            call.request_id = request_id
        no_callback = bool(_NO_CALLBACK.search(text))
        if no_callback:
            call.pending_callback = ""
        callback = not no_callback and bool(_CALLBACK.search(text) or
                                            (self.callback_receipt and _RISK_CALLBACK.search(text)))
        result: dict[str, Any] = {}
        slots: dict[str, Any] = {}

        wrong_person = _WRONG_PERSON.search(text) or (self.answer_followups and _FORMER_CONTACT.search(text))
        if call.closed or call.request_id != request_id or _OPT_OUT.search(text) or (not call.verified and wrong_person):
            call.closed = True
            sentences, outcome = [f"明白了，本次通话到这里。{_CLOSE}"], "safe_exit"
        elif call.transferred or (_HUMAN.search(text) and not callback):
            call.transferred = True
            yield {"kind": "transfer", "reply_id": reply_id, "reason": "user_request", "queue": "human"}
            return
        elif self.identity_first and not call.verified and not (
            call.challenged and _IDENTITY_YES.match(text)
        ):
            if callback:
                call.pending_callback = text
            sentences, outcome = [_VERIFY if call.challenged else _OPENING], "pending_identity"
            call.challenged = True
        else:
            call.verified = self.identity_first
            callback_status = _CALLBACK_STATUS.search(text) or _ACK.fullmatch(text)
            if self.answer_followups:
                callback_status = callback_status or _CALLBACK_FOLLOWUP.search(text)
            if (call.callback_attempted and not no_callback and not _RETRY_CALLBACK.search(text)
                    and callback_status):
                slots = {"request_id": request_id}
                sentences, outcome, result = self._callback_status(call)
                if self.answer_followups and _CALLBACK_FOLLOWUP.search(text):
                    sentences = (
                        ["回电登记已关联到本次申请，后续由资质审核专员跟进。"] if call.ticket else
                        [f"您可以在京麦联系审核支持，或拨打{HOTLINE}。刚才未登记成功。"]
                    )
            elif callback or (call.pending_callback and not no_callback):
                callback_text = text if callback else call.pending_callback
                call.pending_callback = ""
                if not self.callback_receipt:
                    async for chunk in super().handle({**req, "user_text": callback_text}, cancel, tools):
                        yield chunk
                    return
                slots = {"request_id": request_id}
                sentences, outcome, result = await self._callback({**req, "session_id": session_id}, call, cancel, tools, request_id)
            elif self.case_grounding:
                doc = self._find_case_doc(context)
                sentences, outcome, slots, result = self._explain(text, doc, context)
                if outcome in {"rejected", "jd_rejected"}:
                    if call.explained:
                        if self.answer_followups:
                            sentences = self._followup(text, sentences, slots)
                        elif _ACK.fullmatch(text):
                            sentences = [f"如有疑问，可拨打{HOTLINE}。{_CLOSE}"]
                    call.explained = True
            else:
                async for chunk in super().handle(req, cancel, tools):
                    yield chunk
                return

        for index, sentence in enumerate(sentences, 1):
            await asyncio.sleep(self.pause_s)
            cancel.check()
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": index,
                   "text": sentence, "final": index == len(sentences)}
        cancel.check()
        yield {"kind": "end", "reply_id": reply_id, "outcome": outcome, "slots": slots, "result": result}

    @staticmethod
    def _followup(text: str, explanation: list[str], slots: dict[str, Any]) -> list[str]:
        action = slots["remediation"]
        if _ASK_PRIVATE.search(text):
            return ["完整联系方式或证件号码不能在电话里告知。请通过京麦或您已知的品牌方官方渠道核实。"]
        if _ASK_APPROVAL.search(text):
            return [f"审核结果以补交后的复审为准。您可以先按这一条处理：{action}。"]
        if _ACK.fullmatch(text) or _FOLLOWUP_CLOSE.fullmatch(text):
            return [f"如有疑问，可拨打{HOTLINE}。{_CLOSE}"]
        if _REPEAT.search(text):
            return [f"我简要说一下，下一步是{action}。"]
        if _ASK_CHANNEL.search(text):
            return [f"请打开京麦里的原申请，查看驳回意见。按提示{action}。"]
        if _ASK_REASON.search(text):
            return [explanation[0], "具体以这张申请的驳回意见为准。"]
        if _ASK_ACTION.search(text):
            return [f"您下一步可以{action}。"]
        return ["您是想确认驳回原因，还是想了解材料怎么补？"]

    def _explain(self, text: str, doc: dict[str, Any] | None,
                 context: dict[str, Any]) -> tuple[list[str], str, dict[str, Any], dict[str, Any]]:
        if not doc or doc["scene"].get("mode") not in {"电话", "京麦驳回"}:
            return ["我暂时无法核实这笔申请的驳回详情，请通过京麦核对单据意见。"], "route_unknown", {}, {}
        scene = doc["scene"]
        reason = re.sub(r"\s+", "", scene["l3"])
        action = self._source_action(reason)
        spoken_reason = reason
        if "造假" in reason:
            content = str(doc.get("content", ""))
            spoken_reason = "提交的资质信息未能核实"
            if "失真" in content or "修改信息" in content:
                spoken_reason = "提交材料存在信息失真，真实性未能核实"
            action = "请在京麦核对单据意见并提供真实、可核验的资质材料"
        elif "明确告知假授权" in reason:
            spoken_reason = "品牌方未认可本次授权"
        mode = scene["mode"]
        brand = str(context.get("brand") or "相关")
        lead = "我再说明一下。" if _REPEAT.search(text) else ""
        sentences = [f"{lead}您申请的{brand}品牌资质审核未通过，原因是{spoken_reason}。", f"处理方式：{action}。"]
        if mode == "京麦驳回":
            sentences.append("这个情况走京麦站内驳回，请以京麦单据上的书面意见为准。")
        else:
            sentences.append("您可在京麦查看单据意见。请问对处理方式还有疑问吗？")
        slots = {"request_id": str(context.get("request_id") or ""), "brand": brand,
                 "reject_l1": scene.get("l1"), "reject_l2": scene.get("l2"), "reject_l3": reason,
                 "handling_mode": mode, "remediation": action}
        result = {"handling_mode": mode, "remediation": action, "citation": {"doc_id": doc.get("id")}}
        outcome = "jd_rejected" if mode == "京麦驳回" else "rejected"
        return sentences, outcome, slots, result
