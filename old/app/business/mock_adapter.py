"""MockBusiness 适配器，实现 BusinessGateway 接口。"""
from __future__ import annotations

from typing import Any

from .gateway import BusinessGateway, BusinessGatewayError


class MockBusinessAdapter(BusinessGateway):
    """MockBusiness 适配器，将现有 Mock 接入 BusinessGateway 接口。"""

    def __init__(self) -> None:
        # 模拟数据存储
        self.rejections: dict[str, dict[str, Any]] = {
            "Q983001": {
                "request_id": "Q983001",
                "brand": "演示品牌",
                "status": "rejected",
                "reject_category": {
                    "l1": "入驻",
                    "l2": "公司主体",
                    "l3": "主体资质到期"
                },
                "reject_reason": "营业执照已过期，需更新后重新提交",
                "contact_phone": "138****5678",
                "created_at": "2026-11-01T10:00:00+08:00",
                "updated_at": "2026-11-01T14:30:00+08:00"
            }
        }
        self.callbacks: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self._seq = 0

    async def query_rejection(self, request_id: str) -> dict[str, Any]:
        """查询资质审核驳回详情。"""
        rejection = self.rejections.get(request_id)
        if not rejection:
            raise BusinessGatewayError(
                "NOT_FOUND",
                f"Qualification request {request_id} not found",
                retryable=False
            )
        return dict(rejection)

    async def create_callback(
        self,
        request_id: str,
        reason: str,
        contact_phone: str | None = None,
        notes: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """创建回电工单。"""
        # 验证申请单是否存在
        if request_id not in self.rejections:
            raise BusinessGatewayError(
                "NOT_FOUND",
                f"Qualification request {request_id} not found",
                retryable=False
            )
        
        self._seq += 1
        ticket_id = f"CB{self._seq:06d}"
        
        callback = {
            "ticket_id": ticket_id,
            "request_id": request_id,
            "reason": reason,
            "contact_phone": contact_phone,
            "notes": notes,
            "status": "scheduled",
            "created_at": "2026-11-11T21:00:00+08:00"
        }
        
        self.callbacks[ticket_id] = callback
        return dict(callback)

    async def send_jingmai_message(
        self,
        merchant_id: str,
        message_type: str,
        content: dict[str, Any]
    ) -> dict[str, Any]:
        """发送京麦消息推送。"""
        self._seq += 1
        message_id = f"JM{self._seq:06d}"
        
        message = {
            "message_id": message_id,
            "merchant_id": merchant_id,
            "message_type": message_type,
            "content": content,
            "status": "sent",
            "sent_at": "2026-11-11T21:00:00+08:00"
        }
        
        self.messages[message_id] = message
        return dict(message)

    async def update_qualification_status(
        self,
        request_id: str,
        status: str,
        notes: str | None = None
    ) -> dict[str, Any]:
        """更新资质审核状态。"""
        rejection = self.rejections.get(request_id)
        if not rejection:
            raise BusinessGatewayError(
                "NOT_FOUND",
                f"Qualification request {request_id} not found",
                retryable=False
            )
        
        rejection["status"] = status
        rejection["updated_at"] = "2026-11-11T21:00:00+08:00"
        if notes:
            rejection["notes"] = notes
        
        return {
            "request_id": request_id,
            "status": status,
            "updated_at": rejection["updated_at"]
        }
