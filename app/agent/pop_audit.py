from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..rag.knowledge import KnowledgeBase
from .fake import FakeAgent


DEFAULT_REQUEST_ID = "Q000001"
DEFAULT_BRAND = "相关"
HOTLINE = "4006180092"


# 驳回原因 -> 可执行的整改动作。数据里的标准话术保留原模板，
# 这里只把“怎么改”抽成可被 Agent 复述的短句，避免把整段书面话术直接念给商家。
REMEDIATION: dict[str, str] = {
    "商标状态": "提供符合申请状态要求的有效商标",
    "商标注册时间": "提供有效期内的商标，或待时间满足后重新提交",
    "授权书内容不完整": "补齐授权书缺少的盖章、身份证复印件或独占性材料",
    "授权链路不完整": "补齐从品牌方到开店公司的完整授权链路",
    "授权时间到期": "重新开具在有效期内的授权书",
    "授权方未回复": "先按京麦驳回处理，待品牌方回复后可重新提交",
    "品牌方不认可授权": "先与品牌方确认授权，再重新提交",
    "品牌类目不符": "调整申请类目，或补充对应类目的授权范围",
    "类目超商标范围": "删除超范围类目，或补充商标授权范围",
    "产品线不匹配": "更换品牌或更换到与商标匹配的类目",
    "商标无效": "更换有效商标后重新申请",
    "图形商标": "更换文字商标后重新申请",
    "店铺名称": "按命名规则修改店铺名称，或重新提交一致的授权书",
    "发票": "补充符合时间、金额、验真要求的发票及支付流水",
    "流水": "补充对应单据的支付流水并加盖开店主体公章",
    "公章": "补齐开店主体公章后重新提交",
    "资质造假": "提供真实有效的发票、合同、流水等材料",
    "治理停用": "该品牌当前受平台管控，暂不支持申请",
    "无品牌": "更换为有授权的品牌后重新申请",
    "禁售": "更换其他可申请品牌",
    "不符合业务要求": "该品牌暂不招商或为邀请制，建议更换品牌",
    "主体资质": "按国家企业信用信息公示系统状态处理后再提交",
    "商标注册证为境内": "全球购场景需提供境外有效注册证",
}


class PopAuditAgent:
    """POP 品牌入驻资质审核驳回外呼的规则 Agent。

    数据来源：赛题《资审关单场景话术 V1.0》《驳回外呼 QA》。
    这层只负责把“场景 + 驳回原因 + 整改动作”说清楚；真正的 LLM 版本仍走
    LLMAgent，外层接口保持不变。
    """

    def __init__(self, knowledge_path: Path | None = None, pause_ms: int = 200) -> None:
        if knowledge_path is None:
            candidate = Path(__file__).resolve().parents[2] / "knowledge" / "pop_audit.json"
            knowledge_path = candidate if candidate.exists() else None
        self.knowledge = KnowledgeBase(knowledge_path)
        # 旧订单/退款演示仍可用：非 POP 意图交给通用规则 Agent，避免默认场景切换
        # 把历史 demo 和评测脚本全部打断。
        generic = Path(__file__).resolve().parents[2] / "knowledge" / "faq.json"
        self.generic = FakeAgent(generic if generic.exists() else None, pause_ms)
        self.pause_s = max(0.0, pause_ms / 1000.0)

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        # 当前规则 Agent 不需要按 session 预加载；保留接口给 LLMAgent/Flow 版本。
        return None

    def _scene(self, text: str) -> dict[str, Any] | None:
        """按三级分类和标签做确定性匹配，找不到时回退到首条商标场景。"""
        best: tuple[int, dict[str, Any]] | None = None
        for doc in self.knowledge.docs:
            scene = doc.get("scene") or {}
            if not scene.get("l3"):
                continue
            score = 0
            for level, weight in (("l1", 1), ("l2", 2), ("l3", 4)):
                value = str(scene.get(level, ""))
                if value and value in text:
                    score += weight
            for tag in doc.get("tags", []):
                if tag and str(tag) in text:
                    score += 1
            if score and (best is None or score > best[0]):
                best = (score, doc)
        if best:
            return best[1]
        return next((d for d in self.knowledge.docs if d.get("scene", {}).get("l3")), None)

    @staticmethod
    def _remediation(reason: str) -> str:
        for key, value in REMEDIATION.items():
            if key in reason:
                return value
        return "按京麦驳回意见补齐或修正对应材料后重新提交"

    @staticmethod
    def _intent(text: str) -> str:
        if any(word in text for word in ("投诉", "转人工", "找人工", "人工客服")):
            return "complaint"
        if any(word in text for word in ("京麦", "站内驳回", "不用电话", "短信")):
            return "jd_reject"
        if any(word in text for word in ("回电", "再打", "没听清", "稍后联系")):
            return "callback"
        if any(word in text for word in ("怎么改", "怎么处理", "需要什么", "补什么", "材料")):
            return "ask_action"
        if any(word in text for word in ("为什么", "原因", "哪里", "不合格", "驳回")):
            return "ask_reason"
        if any(word in text for word in ("好的", "知道了", "可以", "明白", "确认")):
            return "confirm"
        return "unclear"

    async def handle(self, req: dict[str, Any], cancel: CancelToken,
                     tools: Any) -> AsyncIterator[dict[str, Any]]:
        text = str(req.get("user_text", "")).strip()
        reply_id = req.get("reply_id", "r_dev_1")
        turn_id = req.get("turn_id", "t_0000")
        trace_id = req.get("trace_id")
        session_id = req.get("session_id", "s_dev")

        async def pause() -> None:
            await asyncio.sleep(self.pause_s)
            cancel.check()

        if any(word in text for word in ("投诉", "转人工", "找人工")):
            await pause()
            yield {"kind": "transfer", "reply_id": reply_id, "reason": "user_request", "queue": "human"}
            return

        # 兼容旧演示：订单/退款/配送等非 POP 意图沿用原规则 Agent。
        if any(word in text for word in ("订单", "改约", "改到", "改吧", "配送", "退款", "权益", "保修")):
            async for chunk in self.generic.handle(req, cancel, tools):
                yield chunk
            return

        scene_doc = self._scene(text)
        scene = (scene_doc or {}).get("scene") or {}
        reason = str(scene.get("l3") or "申请材料不符合要求")
        mode = str(scene.get("mode") or "电话")
        remediation = self._remediation(reason)
        brand = DEFAULT_BRAND
        request_id = DEFAULT_REQUEST_ID
        intent = self._intent(text)

        # 回电请求落到工单；即使业务查询失败，也要让商家听到明确下一步。
        if intent == "callback":
            await pause()
            try:
                result = await tools.invoke(
                    "callback.create",
                    {"request_id": request_id, "reason": "商家要求回电"},
                    session_id=session_id, turn_id=turn_id, trace_id=trace_id,
                )
                ticket = (result.get("data") or {}).get("ticket_id", "")
            except Exception:
                ticket = ""
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": 1,
                   "text": "好的，我先为您登记回电任务，稍后由资质审核专员跟进。", "final": True}
            await pause()
            yield {"kind": "end", "reply_id": reply_id, "outcome": "callback_scheduled",
                   "slots": {"request_id": request_id, "brand": brand, "handling_mode": mode},
                   "result": {"ticket": ticket or "pending"}}
            return

        # 先确认身份；后续回复同时给出原因和整改动作，避免商家听完原因却不知道怎么做。
        await pause()
        if intent == "ask_action":
            reply = f"处理方式：{remediation}。补齐后可以在京麦重新提交，不需要重复提交原单据。"
            outcome = "rejected"
            sentences = [reply]
        elif intent == "ask_reason":
            reply = f"您申请的{brand}品牌，因{reason}，本次资质审核未通过。"
            outcome = "rejected"
            sentences = [reply]
        elif intent == "jd_reject":
            reply = f"您申请的{brand}品牌，因{reason}，该场景按京麦驳回处理，驳回原因和整改意见会留在单据里，无需强制回电。"
            outcome = "jd_rejected"
            sentences = [reply]
        elif mode == "京麦驳回":
            reply = f"您申请的{brand}品牌，因{reason}，该场景按京麦驳回处理，驳回原因和整改意见会留在单据里，无需强制回电。"
            outcome = "jd_rejected"
            sentences = [reply]
        else:
            sentences = [
                "您好，我是京东商家资质审核专员，请问您是店铺负责人吗？",
                f"您申请的{brand}品牌，因{reason}，本次资质审核未通过。",
                f"处理方式：{remediation}。补齐后可以在京麦重新提交。",
                f"如有疑问可拨打{HOTLINE}。感谢您的接听，祝您生活愉快，再见。",
            ]
            outcome = "rejected"

        for index, sentence in enumerate(sentences, 1):
            await pause()
            yield {"kind": "sentence", "reply_id": reply_id, "sentence_id": index,
                   "text": sentence, "final": index == len(sentences)}
        await pause()
        yield {"kind": "end", "reply_id": reply_id, "outcome": outcome,
               "slots": {
                   "request_id": request_id,
                   "brand": brand,
                   "reject_l1": scene.get("l1"),
                   "reject_l2": scene.get("l2"),
                   "reject_l3": reason,
                   "handling_mode": mode,
                   "remediation": remediation,
               },
               "result": {"handling_mode": mode, "remediation": remediation}}
