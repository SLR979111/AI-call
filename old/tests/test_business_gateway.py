"""测试 BusinessGateway 集成。"""
import pytest

from app.business.gateway import (
    BusinessGatewayError,
    RealBusinessGateway,
    BusinessConfig,
    create_business_gateway
)
from app.business.mock_adapter import MockBusinessAdapter


@pytest.mark.asyncio
async def test_mock_adapter_query_rejection():
    """测试 Mock 适配器的驳回详情查询。"""
    adapter = MockBusinessAdapter()
    
    # 查询存在的申请单
    result = await adapter.query_rejection("Q983001")
    assert result["request_id"] == "Q983001"
    assert result["brand"] == "演示品牌"
    assert result["status"] == "rejected"
    assert result["reject_category"]["l1"] == "入驻"
    assert result["reject_category"]["l2"] == "公司主体"
    assert result["reject_category"]["l3"] == "主体资质到期"
    
    # 查询不存在的申请单
    with pytest.raises(BusinessGatewayError) as exc_info:
        await adapter.query_rejection("Q999999")
    assert exc_info.value.code == "NOT_FOUND"
    assert not exc_info.value.retryable


@pytest.mark.asyncio
async def test_mock_adapter_create_callback():
    """测试 Mock 适配器的回电工单创建。"""
    adapter = MockBusinessAdapter()
    
    # 创建回电工单
    result = await adapter.create_callback(
        request_id="Q983001",
        reason="需要补充材料说明",
        contact_phone="138****5678",
        notes="客户要求上午回电"
    )
    
    assert result["ticket_id"].startswith("CB")
    assert result["request_id"] == "Q983001"
    assert result["reason"] == "需要补充材料说明"
    assert result["status"] == "scheduled"
    
    # 对不存在的申请单创建工单
    with pytest.raises(BusinessGatewayError) as exc_info:
        await adapter.create_callback("Q999999", "test")
    assert exc_info.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_mock_adapter_send_jingmai_message():
    """测试 Mock 适配器的京麦消息推送。"""
    adapter = MockBusinessAdapter()
    
    result = await adapter.send_jingmai_message(
        merchant_id="M12345",
        message_type="qualification_update",
        content={
            "title": "资质审核驳回通知",
            "body": "您的入驻申请因主体资质到期被驳回"
        }
    )
    
    assert result["message_id"].startswith("JM")
    assert result["merchant_id"] == "M12345"
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_mock_adapter_update_qualification_status():
    """测试 Mock 适配器的资质审核状态更新。"""
    adapter = MockBusinessAdapter()
    
    result = await adapter.update_qualification_status(
        request_id="Q983001",
        status="callback_scheduled",
        notes="已安排回电"
    )
    
    assert result["request_id"] == "Q983001"
    assert result["status"] == "callback_scheduled"
    
    # 更新后查询验证
    rejection = await adapter.query_rejection("Q983001")
    assert rejection["status"] == "callback_scheduled"
    assert rejection["notes"] == "已安排回电"


@pytest.mark.asyncio
async def test_create_business_gateway_mock(monkeypatch):
    """测试 create_business_gateway 默认返回 Mock。"""
    monkeypatch.delenv("BUSINESS_GATEWAY", raising=False)
    
    gateway = create_business_gateway()
    assert isinstance(gateway, MockBusinessAdapter)


@pytest.mark.asyncio
async def test_create_business_gateway_real_missing_config(monkeypatch):
    """测试 create_business_gateway 真实模式缺少配置时报错。"""
    monkeypatch.setenv("BUSINESS_GATEWAY", "real")
    monkeypatch.delenv("BUSINESS_API_BASE_URL", raising=False)
    
    with pytest.raises(ValueError) as exc_info:
        create_business_gateway()
    assert "BUSINESS_API_BASE_URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_real_gateway_close():
    """测试 RealBusinessGateway 的客户端关闭。"""
    config = BusinessConfig(
        base_url="https://api.example.com",
        api_key="test-key"
    )
    gateway = RealBusinessGateway(config)
    
    # 未创建客户端时关闭不报错
    await gateway.close()
    
    # 创建客户端后关闭
    await gateway._get_client()
    await gateway.close()
    assert gateway._client is None
