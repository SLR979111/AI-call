"""BusinessGateway 幂等与写工具不重试的回归测试。"""
from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from app.business.gateway import (
    BusinessConfig,
    BusinessGatewayError,
    RealBusinessGateway,
    create_business_gateway,
)
from app.tools.gateway import MockBusiness, ToolGateway

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def workspace_tmp() -> Iterator[Path]:
    """在仓库 data/ 下创建临时目录。

    tempfile.TemporaryDirectory 在 Windows 上以 0o700 建目录，受限运行环境里
    新建的子目录不可写；普通 mkdir 可以正常继承工作区权限。
    """
    path = ROOT / "data" / "test-runs" / f"idem-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FailOnceCallbackGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def create_callback(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise BusinessGatewayError("TIMEOUT", "upstream timeout", retryable=True)

    async def close(self) -> None:
        return None


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _mock_client(gateway: RealBusinessGateway, handler: Any) -> None:
    gateway._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=gateway.config.base_url,
    )


@pytest.mark.asyncio
async def test_non_idempotent_callback_is_not_retried():
    gateway = FailOnceCallbackGateway()
    with workspace_tmp() as tmp:
        tools = ToolGateway(tmp, MockBusiness(), None, business_gateway=gateway)
        try:
            result = await tools.invoke(
                "callback.create",
                {"request_id": "Q123456", "reason": "商家要求回电"},
                deadline_ms=1000,
                session_id="s_test",
                turn_id="t_1",
            )
        finally:
            await tools.aclose()
    assert result["ok"] is False
    assert gateway.calls == 1
    assert result["meta"]["attempt"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("ticket.create", {"summary": "需要人工跟进"}),
        ("order.reschedule", {"order_id": "A12345678", "new_time": "2026-11-14T15:00"}),
    ],
)
async def test_mock_write_tools_are_not_retried(tool: str, args: dict[str, Any]):
    mock = MockBusiness()
    mock.fault = "500"
    with workspace_tmp() as tmp:
        tools = ToolGateway(tmp, mock, None)
        try:
            result = await tools.invoke(
                tool,
                args,
                deadline_ms=1000,
                session_id="s_test",
                turn_id="t_1",
            )
        finally:
            await tools.aclose()
    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_5XX"
    assert result["meta"]["attempt"] == 1
    assert mock._seq == 0


@pytest.mark.asyncio
async def test_callback_idempotency_key_is_forwarded():
    class CaptureGateway:
        def __init__(self):
            self.key = None

        async def create_callback(self, **kwargs):
            self.key = kwargs.get("idempotency_key")
            return {"ticket_id": "CB1", "status": "scheduled"}

        async def close(self):
            return None

    gateway = CaptureGateway()
    with workspace_tmp() as tmp:
        tools = ToolGateway(tmp, MockBusiness(), None, business_gateway=gateway)
        try:
            result = await tools.invoke(
                "callback.create",
                {"request_id": "Q123456", "reason": "商家要求回电"},
                deadline_ms=1000,
                session_id="s_test",
                turn_id="t_1",
            )
        finally:
            await tools.aclose()
    assert result["ok"] is True
    assert gateway.key and gateway.key.startswith("sha256:")


@pytest.mark.asyncio
async def test_tool_returned_response_is_redacted():
    class SensitiveGateway:
        async def create_callback(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "ticket_id": "CB1",
                "contact_phone": "13800000000",
                "brand_name": "示例品牌",
                "status": "scheduled",
            }

    bus = RecordingBus()
    with workspace_tmp() as tmp:
        tools = ToolGateway(tmp, MockBusiness(), bus, business_gateway=SensitiveGateway())
        try:
            result = await tools.invoke(
                "callback.create",
                {"request_id": "Q123456", "reason": "商家要求回电"},
                deadline_ms=1000,
                session_id="s_test",
                turn_id="t_1",
            )
        finally:
            await tools.aclose()
    assert result["ok"] is True
    returned = [event for event in bus.events if event["type"] == "ToolReturned"]
    assert len(returned) == 1
    summary = returned[0]["payload"]["data_summary"]
    assert summary["ticket_id"] == "CB1"
    assert summary["contact_phone"] == "***"
    assert summary["brand_name"] == "***"
    assert summary["status"] == "scheduled"


@pytest.mark.asyncio
async def test_real_gateway_callback_is_not_retried_and_sends_idempotency_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"message": "busy"})

    gateway = RealBusinessGateway(
        BusinessConfig(
            base_url="https://biz.example.com",
            api_key="test-key",
            retry_count=3,
        )
    )
    _mock_client(gateway, handler)
    try:
        with pytest.raises(BusinessGatewayError) as exc_info:
            await gateway.create_callback(
                "Q983001",
                "需补充材料",
                idempotency_key="sha256:abc123",
            )
    finally:
        await gateway.close()

    assert exc_info.value.code == "HTTP_503"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].headers["Idempotency-Key"] == "sha256:abc123"


@pytest.mark.asyncio
async def test_real_gateway_patch_is_not_retried():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"message": "boom"})

    gateway = RealBusinessGateway(
        BusinessConfig(
            base_url="https://biz.example.com",
            api_key="test-key",
            retry_count=2,
        )
    )
    _mock_client(gateway, handler)
    try:
        with pytest.raises(BusinessGatewayError) as exc_info:
            await gateway.update_qualification_status("Q983001", "callback_scheduled")
    finally:
        await gateway.close()

    assert exc_info.value.code == "HTTP_500"
    assert len(requests) == 1
    assert requests[0].method == "PATCH"


@pytest.mark.asyncio
async def test_real_gateway_read_is_retried():
    statuses = [503, 200]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json={"request_id": "Q983001", "status": "rejected"})
        return httpx.Response(503, json={"message": "busy"})

    gateway = RealBusinessGateway(
        BusinessConfig(
            base_url="https://biz.example.com",
            api_key="test-key",
            retry_count=1,
        )
    )
    _mock_client(gateway, handler)
    try:
        result = await gateway.query_rejection("Q983001")
    finally:
        await gateway.close()

    assert result["request_id"] == "Q983001"
    assert [request.method for request in requests] == ["GET", "GET"]


def test_real_mode_requires_url_and_key(monkeypatch):
    monkeypatch.setenv("BUSINESS_GATEWAY", "  REAL  ")
    monkeypatch.setenv("BUSINESS_API_BASE_URL", "https://biz.example.com")
    monkeypatch.setenv("BUSINESS_API_KEY", "test-key")

    for missing in ("BUSINESS_API_BASE_URL", "BUSINESS_API_KEY"):
        monkeypatch.delenv(missing, raising=False)
        with pytest.raises(ValueError, match=missing):
            create_business_gateway()
        if missing == "BUSINESS_API_BASE_URL":
            monkeypatch.setenv("BUSINESS_API_BASE_URL", "https://biz.example.com")
