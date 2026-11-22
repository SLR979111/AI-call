# feat: 真实业务系统集成 - 阶段 1 & 2

## 📋 概述

实现真实业务系统网关，支持资质审核驳回外呼场景的完整业务写回功能。

**目标**：
- ✅ 阶段 1：验证流式响应效果（已合并 PR #7）
- ✅ 阶段 2：真实业务写回（本 PR）

---

## 🎯 核心改动

### 1. BusinessGateway 抽象接口 (`app/business/gateway.py`)

定义标准化的业务系统接口：

```python
class BusinessGateway(ABC):
    @abstractmethod
    async def query_rejection(self, request_id: str) -> dict[str, Any]:
        """查询资质审核驳回详情"""
    
    @abstractmethod
    async def create_callback(
        self, request_id: str, reason: str,
        contact_phone: str | None = None,
        notes: str | None = None
    ) -> dict[str, Any]:
        """创建回电工单"""
    
    @abstractmethod
    async def send_jingmai_message(
        self, merchant_id: str, message_type: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        """发送京麦消息推送"""
    
    @abstractmethod
    async def update_qualification_status(
        self, request_id: str, status: str, notes: str | None = None
    ) -> dict[str, Any]:
        """更新资质审核状态"""
```

### 2. RealBusinessGateway HTTP 实现

基于 `httpx.AsyncClient` 的真实业务系统客户端：

**特性**：
- 异步 HTTP 请求（GET/POST/PATCH）
- 超时控制（可配置，默认 5000ms）
- 自动重试（可配置，默认 1 次）
- 错误分类（TIMEOUT, HTTP_*, UNKNOWN）
- Bearer Token 认证

**错误处理**：
```python
class BusinessGatewayError(Exception):
    code: str       # "NOT_FOUND" | "TIMEOUT" | "HTTP_500" | ...
    message: str
    retryable: bool
```

### 3. MockBusinessAdapter 适配器 (`app/business/mock_adapter.py`)

将现有 `MockBusiness` 适配到 `BusinessGateway` 接口：

**模拟数据**：
- 预置资质审核驳回记录（Q983001）
- 三级分类：入驻 → 公司主体 → 主体资质到期
- 回电工单、京麦消息、状态更新

**用途**：
- 测试环境默认使用
- 不依赖外部系统
- 确定性输出

### 4. 集成到 ToolGateway (`app/tools/gateway.py`)

**修改**：
```python
class ToolGateway:
    def __init__(
        self, data_dir: Path,
        mock: MockBusiness | None = None,
        bus: EventBus | None = None,
        business_gateway: Any | None = None  # 新增
    ):
        self.business_gateway = business_gateway
```

**工具路由逻辑**：
1. `callback.create` 优先使用 `BusinessGateway`
2. 失败时自动降级到 `MockBusiness`
3. 其他工具继续使用 `MockBusiness`

---

## ⚙️ 环境变量配置

```bash
# 网关模式（默认: mock）
BUSINESS_GATEWAY=mock|real

# 真实模式配置（BUSINESS_GATEWAY=real 时必填）
BUSINESS_API_BASE_URL=https://api.example.com
BUSINESS_API_KEY=your-api-key-here

# 可选配置
BUSINESS_TIMEOUT_MS=5000      # 超时时间（默认: 5000ms）
BUSINESS_RETRY_COUNT=1        # 重试次数（默认: 1）
```

**工厂函数**：
```python
from app.business.gateway import create_business_gateway

gateway = create_business_gateway()  # 根据环境变量自动选择
```

---

## 🧪 测试结果

### 单元测试

```bash
pytest tests/test_business_gateway.py -v
```

**结果**：✅ **7/7 测试通过**

| 测试 | 说明 | 结果 |
|---|---|---|
| `test_mock_adapter_query_rejection` | 查询驳回详情（存在/不存在） | ✅ |
| `test_mock_adapter_create_callback` | 创建回电工单 | ✅ |
| `test_mock_adapter_send_jingmai_message` | 发送京麦消息 | ✅ |
| `test_mock_adapter_update_qualification_status` | 更新审核状态 | ✅ |
| `test_create_business_gateway_mock` | 工厂函数 Mock 模式 | ✅ |
| `test_create_business_gateway_real_missing_config` | 真实模式配置校验 | ✅ |
| `test_real_gateway_close` | 客户端生命周期管理 | ✅ |

### 集成测试

```bash
pytest tests/test_contract.py -v -k "tool"
```

**结果**：✅ 所有工具相关测试通过

---

## 📁 文件清单

```
app/business/
├── __init__.py              # 模块初始化
├── gateway.py               # BusinessGateway 接口 + RealBusinessGateway 实现
└── mock_adapter.py          # MockBusinessAdapter 适配器

tests/
└── test_business_gateway.py # 单元测试（7 个测试用例）

app/tools/gateway.py         # 集成 BusinessGateway（修改）
```

---

## 🔄 向后兼容性

### 完全兼容
- **接口不变**：ToolGateway 签名保持兼容
- **默认行为不变**：不配置 `business_gateway` 参数时完全使用 Mock
- **环境变量可选**：`BUSINESS_GATEWAY` 默认为 `mock`

### 使用方式

**方式 1：保持现有行为（Mock）**
```python
tools = ToolGateway(data_dir, mock, bus)  # 不传 business_gateway
```

**方式 2：启用真实网关**
```python
from app.business.gateway import create_business_gateway

gateway = create_business_gateway()
tools = ToolGateway(data_dir, mock, bus, business_gateway=gateway)
```

**方式 3：环境变量控制**
```bash
export BUSINESS_GATEWAY=real
export BUSINESS_API_BASE_URL=https://api.example.com
export BUSINESS_API_KEY=xxx
# 应用代码无需修改
```

---

## 🚀 后续工作

### 待扩展功能
1. **更多工具支持**：`order.query`, `ticket.create` 等
2. **批量操作**：支持批量查询和写回
3. **缓存机制**：减少重复请求
4. **监控埋点**：业务网关调用指标

### 待优化项
1. **错误重试策略**：指数退避、熔断
2. **请求去重**：幂等性保证
3. **并发控制**：限流和排队
4. **健康检查**：API 可用性探测

---

## 📝 提交记录

```
451d838 feat(tools): 集成 BusinessGateway 到 ToolGateway
e8da231 feat(business): 实现真实业务网关集成
```

---

## 🔍 Checklist

- [x] 代码实现完成
- [x] 单元测试通过（7/7）
- [x] 集成测试通过
- [x] 向后兼容性验证
- [x] 环境变量配置文档
- [x] 使用示例
- [ ] 生产环境 API 接入（需真实 API 文档）
- [ ] 性能基准测试
- [ ] 错误场景覆盖

---

## 📌 相关 PR

- PR #7: LLM 流式响应实现（已合并）
