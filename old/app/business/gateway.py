"""真实业务系统网关接口定义和实现。"""
from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class BusinessConfig:
    """业务系统配置。"""
    base_url: str
    api_key: str
    timeout_ms: int = 5000
    retry_count: int = 1


class BusinessGatewayError(Exception):
    """业务系统调用错误。"""
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class BusinessGateway(ABC):
    """业务系统网关抽象接口。"""

    @abstractmethod
    async def query_rejection(self, request_id: str) -> dict[str, Any]:
        """查询资质审核驳回详情。
        
        Args:
            request_id: 申请单号（格式：Q + 6位以上数字）
        
        Returns:
            {
                "request_id": str,
                "brand": str,
                "status": str,  # "rejected" | "pending" | "approved"
                "reject_category": {
                    "l1": str,  # 一级分类：入驻/产品/经营
                    "l2": str,  # 二级分类：公司主体/产品资质/...
                    "l3": str   # 三级分类：主体资质到期/信息不符/...
                },
                "reject_reason": str,
                "contact_phone": str | None,
                "created_at": str,
                "updated_at": str
            }
        
        Raises:
            BusinessGatewayError: 查询失败
        """
        pass

    @abstractmethod
    async def create_callback(
        self,
        request_id: str,
        reason: str,
        contact_phone: str | None = None,
        notes: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """创建回电工单；写操作不自动重试，优先使用幂等键。"""
        pass

    @abstractmethod
    async def send_jingmai_message(
        self,
        merchant_id: str,
        message_type: str,
        content: dict[str, Any]
    ) -> dict[str, Any]:
        """发送京麦消息推送。
        
        Args:
            merchant_id: 商家ID
            message_type: 消息类型（"qualification_update" | "callback_scheduled" | ...）
            content: 消息内容
        
        Returns:
            {
                "message_id": str,
                "status": str,  # "sent" | "failed"
                "sent_at": str
            }
        
        Raises:
            BusinessGatewayError: 发送失败
        """
        pass

    @abstractmethod
    async def update_qualification_status(
        self,
        request_id: str,
        status: str,
        notes: str | None = None
    ) -> dict[str, Any]:
        """更新资质审核状态。
        
        Args:
            request_id: 申请单号
            status: 新状态（"callback_scheduled" | "in_review" | ...）
            notes: 备注信息
        
        Returns:
            {
                "request_id": str,
                "status": str,
                "updated_at": str
            }
        
        Raises:
            BusinessGatewayError: 更新失败
        """
        pass


class RealBusinessGateway(BusinessGateway):
    """真实业务系统网关实现（HTTP API）。"""

    def __init__(self, config: BusinessConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=self.config.timeout_ms / 1000.0,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json"
                }
            )
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs: Any
    ) -> dict[str, Any]:
        """发送 HTTP 请求并处理错误；写请求默认不重试。"""
        client = await self._get_client()
        attempts = self.config.retry_count + 1 if retry else 1

        for attempt in range(attempts):
            try:
                response = await client.request(method, path, **kwargs)
                
                if response.status_code >= 400:
                    error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    retryable = response.status_code in {429, 500, 502, 503, 504}
                    raise BusinessGatewayError(
                        f"HTTP_{response.status_code}",
                        error_data.get("message", f"HTTP {response.status_code}"),
                        retryable=retryable
                    )
                
                return response.json()
            
            except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                if retry and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise BusinessGatewayError("TIMEOUT", str(exc), retryable=True)
            
            except BusinessGatewayError as exc:
                if retry and exc.retryable and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
            
            except Exception as exc:
                raise BusinessGatewayError("UNKNOWN", str(exc), retryable=False)
        
        raise BusinessGatewayError("MAX_RETRY", "Max retry exceeded", retryable=False)

    async def query_rejection(self, request_id: str) -> dict[str, Any]:
        """查询资质审核驳回详情。"""
        return await self._request("GET", f"/api/v1/qualifications/{request_id}")

    async def create_callback(
        self,
        request_id: str,
        reason: str,
        contact_phone: str | None = None,
        notes: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """创建回电工单；写调用不自动重试。"""
        payload = {
            "request_id": request_id,
            "reason": reason
        }
        if contact_phone:
            payload["contact_phone"] = contact_phone
        if notes:
            payload["notes"] = notes
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        
        return await self._request(
            "POST", "/api/v1/callbacks", retry=False, json=payload, headers=headers
        )

    async def send_jingmai_message(
        self,
        merchant_id: str,
        message_type: str,
        content: dict[str, Any]
    ) -> dict[str, Any]:
        """发送京麦消息推送。"""
        payload = {
            "merchant_id": merchant_id,
            "message_type": message_type,
            "content": content
        }
        return await self._request("POST", "/api/v1/jingmai/messages", retry=False, json=payload)

    async def update_qualification_status(
        self,
        request_id: str,
        status: str,
        notes: str | None = None
    ) -> dict[str, Any]:
        """更新资质审核状态。"""
        payload = {
            "status": status
        }
        if notes:
            payload["notes"] = notes
        
        return await self._request(
            "PATCH", f"/api/v1/qualifications/{request_id}/status", retry=False, json=payload
        )


def create_business_gateway() -> BusinessGateway:
    """根据环境变量创建业务网关实例。
    
    环境变量：
        BUSINESS_GATEWAY: "mock" | "real" (默认: "mock")
        BUSINESS_API_BASE_URL: 真实业务系统 API 地址
        BUSINESS_API_KEY: API 密钥
        BUSINESS_TIMEOUT_MS: 超时时间（默认: 5000）
        BUSINESS_RETRY_COUNT: 重试次数（默认: 1）
    """
    gateway_type = os.getenv("BUSINESS_GATEWAY", "mock").strip().lower()
    
    if gateway_type == "real":
        base_url = os.getenv("BUSINESS_API_BASE_URL", "").strip()
        api_key = os.getenv("BUSINESS_API_KEY", "").strip()
        
        if not base_url or not api_key:
            raise ValueError(
                "BUSINESS_GATEWAY=real requires BUSINESS_API_BASE_URL and BUSINESS_API_KEY"
            )
        
        config = BusinessConfig(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout_ms=int(os.getenv("BUSINESS_TIMEOUT_MS", "5000")),
            retry_count=int(os.getenv("BUSINESS_RETRY_COUNT", "1"))
        )
        
        return RealBusinessGateway(config)
    
    # 默认返回 Mock（从 MockBusiness 适配）
    from .mock_adapter import MockBusinessAdapter
    return MockBusinessAdapter()
