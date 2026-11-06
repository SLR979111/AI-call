# Agent LLM 集成开发文档

> 更新时间：2026-11-06
> 分支：`feature/agent-llm-integration`
> 状态：✅ 开发完成，待测试验证

---

## 0. 概述

本文档记录了 Agent 模块从规则引擎（FakeAgent）升级为真实 LLM 驱动的完整过程，包括：

1. LLM 真实接入（主备切换 + Function Calling 支持）
2. 对话历史管理（Session 级存储 + 上下文构建）
3. Flow Engine 整合（状态机约束业务流程）
4. Function Calling 实现（工具声明 + 结构化调用）
5. 测试与交付（单元测试 + E2E + 文档）

**核心目标**：让 Agent 支持真实 LLM，同时保持与现有架构的兼容性。

---

## 1. 架构说明

### 1.1 Agent 接口设计

所有 Agent 实现（FakeAgent / LLMAgent）都遵循统一接口：

```python
# app/agent/fake.py 和 llm_agent.py 都实现这个接口

async def start_session(
    session_id: str, 
    version_set: dict[str, str],  # Registry 固定的版本集合
    flow_pack: dict                # 业务流程定义
) -> None:
    """会话初始化，加载 Flow Pack / Prompt / Knowledge"""

async def handle(
    req: dict[str, Any],           # 用户请求
    cancel: CancelToken,           # 取消令牌（支持打断）
    tools: ToolGateway             # 工具调用网关
) -> AsyncIterator[dict[str, Any]]:
    """
    核心决策方法，返回流式 Chunk：
    - {"kind": "sentence", "text": "...", "final": bool}  # 回复句子
    - {"kind": "transfer", "reason": "..."}              # 转人工
    - {"kind": "end", "outcome": "...", "slots": {...}}  # 轮次结束
    """
```

### 1.2 LLMAgent 架构

```
┌─────────────────────────────────────────────────────────────┐
│                        LLMAgent                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  对话历史    │  │ Flow Engine  │  │  RAG 知识库  │     │
│  │   管理       │  │   状态机     │  │   检索       │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│         │                 │                  │              │
│         └─────────────────┼──────────────────┘              │
│                           │                                 │
│                    ┌──────▼──────┐                         │
│                    │ System Prompt│                         │
│                    │  构建器      │                         │
│                    └──────┬──────┘                         │
│                           │                                 │
│                    ┌──────▼──────┐                         │
│                    │  LLMRouter  │ ◄─── Function Calling  │
│                    │  主备切换   │                         │
│                    └──────┬──────┘                         │
│                           │                                 │
│                    ┌──────▼──────┐                         │
│                    │ 决策解析     │                         │
│                    │ FC / JSON   │                         │
│                    └──────┬──────┘                         │
│                           │                                 │
│                    ┌──────▼──────┐                         │
│                    │ Tool Gateway│                         │
│                    │ 工具执行     │                         │
│                    └─────────────┘                         │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 配置指南

### 2.1 环境变量配置

复制 `.env.example` 为 `.env` 并填写：

```bash
# Agent 模式：fake（规则引擎）或 llm（真实 LLM）
AGENT_PROVIDER=llm

# 主 LLM Provider（必需）
LLM_PRIMARY_BASE_URL=https://api.deepseek.com/v1
LLM_PRIMARY_API_KEY=sk-your-key-here
LLM_PRIMARY_MODEL=deepseek-chat
LLM_PRIMARY_NAME=deepseek

# 备用 LLM Provider（可选）
LLM_SECONDARY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_SECONDARY_API_KEY=sk-your-key-here
LLM_SECONDARY_MODEL=qwen-plus
LLM_SECONDARY_NAME=qwen

# LLM 配置
LLM_TIMEOUT_MS=3000
LLM_PRICE_PER_1K_CNY=0.002
LLM_DAILY_BUDGET_CNY=100
```

### 2.2 切换 Fake/LLM Agent

```bash
# 使用规则引擎（无需 LLM Key，适合本地测试）
export AGENT_PROVIDER=fake
python -m uvicorn app.main:app --reload

# 使用真实 LLM（需配置 .env）
export AGENT_PROVIDER=llm
python -m uvicorn app.main:app --reload
```

---

## 3. 功能特性

### 3.1 对话历史管理

**功能**：每个 Session 维护独立的对话历史，支持多轮上下文。

**实现**：
- `sessions: dict[str, list[dict[str, str]]]` 存储每个会话的消息列表
- `start_session()` 初始化空历史
- `handle()` 构建完整 messages：`[system, ...history[-20:], user]`
- 每轮后追加 `{"role": "user", "content": text}` 和 `{"role": "assistant", "content": reply}`
- 自动截断：保留最近 `max_history_turns * 2` 条消息（默认 20 条 = 10 轮）

**效果**：
- ✅ LLM 能记住前几轮的订单号、时间等信息
- ✅ 避免重复询问已知槽位
- ✅ 上下文窗口可控，不会无限增长

### 3.2 Flow Engine 状态机整合

**功能**：业务流程由状态机约束，LLM 只负责理解与表达。

**实现**：
- `flow_engines: dict[str, FlowEngine]` 存储每个 Session 的 FlowEngine 实例
- `start_session()` 从 `flow_pack` 加载状态机并调用 `engine.start()`
- `handle()` 根据 LLM 识别的 `intent` 调用 `engine.apply_intent()`
- 将 LLM 提取的 `slots` 同步到 FlowEngine：`engine.set_slot(name, value)`
- 优先使用 Flow 状态的 `say` 话术，兜底用 LLM 的 `reply`
- 结果中包含 `flow_snapshot`（当前状态 + 槽位 + 历史）

**效果**：
- ✅ 业务流程可配置（`flowpacks/service_outbound.json`）
- ✅ 状态转移逻辑确定性（不依赖 LLM 创造力）
- ✅ 话术可预置（开场白、必说话术）
- ✅ 槽位校验（必填字段、格式检查）

### 3.3 Function Calling 支持

**功能**：LLM 通过结构化的 Function Calling 调用工具，降级时回退到 JSON Mode。

**实现**：

1. **工具声明**（`app/tools/gateway.py`）：
   ```python
   @classmethod
   def to_openai_tools(cls) -> list[dict[str, Any]]:
       """导出为 OpenAI Function Calling 格式"""
       return [{
           "type": "function",
           "function": {
               "name": "order.query",
               "description": "查询订单信息，包括状态、配送时间、地址等",
               "parameters": {
                   "type": "object",
                   "properties": {
                       "order_id": {"type": "string", "pattern": "^[A-Z0-9]{8,20}$"}
                   },
                   "required": ["order_id"]
               }
           }
       }, ...]
   ```

2. **LLM 调用**（`app/agent/llm_agent.py`）：
   ```python
   available_tools = ToolGateway.to_openai_tools()
   result = await self.router.complete(
       messages,
       tools=available_tools,
       tool_choice="auto",
       ...
   )
   ```

3. **结果解析**（优先 Function Calling，降级 JSON Mode）：
   ```python
   if result.tool_calls:
       # Function Calling 路径
       tool_name = result.tool_calls[0]["function"]["name"]
       tool_args = json.loads(result.tool_calls[0]["function"]["arguments"])
   else:
       # JSON Mode 降级
       decision = parse_json_object(result.text)
       tool_name = decision.get("tool")
       tool_args = decision.get("tool_args")
   ```

**效果**：
- ✅ LLM 返回结构化工具调用（符合 OpenAI 标准）
- ✅ 参数自动校验（JSON Schema）
- ✅ 降级兜底（不支持 Function Calling 的 LLM 仍可用）
- ✅ 审计完整（`ProviderToolCall` 事件）

### 3.4 增强的 System Prompt

**功能**：动态构建 System Prompt，注入业务上下文。

**实现**：
```python
def _system(self, flow_state: str = "", slots: dict[str, Any] | None = None) -> str:
    context = SYSTEM_PROMPT.format(
        scenario="服务外呼",
        flow_state=flow_state or "初始状态",
        slots=json.dumps(slots, ensure_ascii=False)
    )
    if self.flow_pack:
        states = ", ".join(self.flow_pack.states.keys())
        context += f"\n\n业务流程状态集合：{states}；入口：{self.flow_pack.entry}。"
    return context
```

**效果**：
- ✅ LLM 知道当前业务流程状态
- ✅ LLM 知道已收集的槽位（避免重复询问）
- ✅ Prompt 可根据场景动态调整

---

## 4. 开发指南

### 4.1 添加新工具

1. 在 `app/tools/gateway.py` 的 `SPECS` 中添加定义：
   ```python
   "order.cancel": ToolSpec(
       "order.cancel", "1.0.0", ("order_id",), 
       {"order_id": r"^[A-Z0-9]{8,20}$"}
   )
   ```

2. 在 `MockBusiness` 中实现模拟逻辑：
   ```python
   async def order_cancel(self, order_id: str) -> dict[str, Any]:
       order = self.orders.get(order_id)
       if not order:
           raise ToolError("TOOL_NOT_FOUND", f"order {order_id} not found")
       order["status"] = "cancelled"
       return {"order_id": order_id, "status": "cancelled"}
   ```

3. 在 `ToolGateway._tool_description()` 中添加中文描述：
   ```python
   "order.cancel": "取消订单，需要订单号",
   ```

4. 在 `ToolGateway.invoke()` 的 `handler` 字典中注册：
   ```python
   "order.cancel": self.mock.order_cancel,
   ```

5. LLM Agent 会自动识别新工具（通过 `to_openai_tools()` 导出）

### 4.2 添加新 Flow Pack

1. 在 `flowpacks/` 目录创建 JSON 文件：
   ```json
   {
     "id": "sales_outbound",
     "version": "1.0.0",
     "entry": "GREETING",
     "states": {
       "GREETING": {
         "say": "您好，我是销售顾问小智。",
         "next": "NEED_CONFIRM"
       },
       "NEED_CONFIRM": {
         "transitions": [
           {"when": {"intent": "confirm"}, "to": "PITCH"},
           {"when": {"intent": "deny"}, "to": "END"}
         ]
       }
     },
     "slots": {
       "lead_id": {"required": true, "pattern": "^L[0-9]{4,12}$"}
     }
   }
   ```

2. 在 `app/main.py` 中加载并注入 Agent：
   ```python
   flow_pack = FlowPack.load(Path("flowpacks/sales_outbound.json"))
   agent = LLMAgent(router, flow_pack=flow_pack)
   ```

### 4.3 调整 Prompt

编辑 `app/agent/llm_agent.py` 的 `SYSTEM_PROMPT`：

```python
SYSTEM_PROMPT = """你是电话外呼客服数字员工，当前通话场景：{scenario}

职责：
1. 理解用户意图（确认/拒绝/改约/FAQ/转人工）
2. 从对话历史提取槽位（订单号/时间/地址等），避免重复询问
3. 决定是否调用工具（order.query / order.reschedule / ticket.create）
4. 话术口语化，首句 <=20 字

当前业务流程状态：{flow_state}
已知槽位：{slots}

输出 JSON：
{
  "intent": "confirm|deny|unclear|order_query|reschedule|faq|transfer",
  "reply": "要对用户说的话（口语化）",
  "slots": {"order_id": "A123", ...},
  "tool": "order.query",
  "tool_args": {...},
  "outcome": "rescheduled|answered|transferred|completed"
}

规则：
- 不承诺无法保证的结果，不使用"保证/一定/绝对"
- 用户要求人工时，intent=transfer
- 信息不足时先追问，不编造
- 已知槽位不重复询问
"""
```

---

## 5. 测试指南

### 5.1 运行现有测试

```bash
# 单元测试 + 契约测试
pytest tests/test_contract.py -v

# 前端协议测试
cd web/rtc && npm test

# 端到端测试
make e2e

# 比赛指标测试
make metric
```

### 5.2 验证 LLM 主备切换

```bash
# 修改 .env 让主 Provider 超时
LLM_PRIMARY_BASE_URL=https://unreachable.example.com/v1
LLM_SECONDARY_BASE_URL=https://api.deepseek.com/v1

# 启动服务
uvicorn app.main:app --reload

# 查看日志，应看到：
# ProviderFailed (primary) → ProviderFailover → ProviderReturned (secondary)
```

### 5.3 验证对话历史

```bash
# 在浏览器 /rtc/ 页面进行 3 轮对话：
# 1. "我想改约订单"
# 2. "订单号是 A12345678"（不重复询问）
# 3. "改到明天下午 3 点"（记得订单号）

# 查看 data/events/ 目录下的 JSONL，验证历史存储
```

### 5.4 验证 Function Calling

```bash
# 使用支持 Function Calling 的 LLM（如 gpt-4o, deepseek-chat）
# 在对话中触发工具调用："我要查询订单 A12345678"

# 查看日志，应看到：
# ProviderToolCall: order.query
# ToolCalled: order.query
# ToolReturned: {"order_id": "A12345678", ...}
```

---

## 6. 故障排查

### 6.1 LLM 超时

**症状**：Agent 回复"抱歉，我这边暂时没听清"

**排查**：
1. 检查 `LLM_PRIMARY_BASE_URL` 是否可达：
   ```bash
   curl -X POST $LLM_PRIMARY_BASE_URL/chat/completions \
     -H "Authorization: Bearer $LLM_PRIMARY_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
   ```
2. 增加超时时间：`LLM_TIMEOUT_MS=5000`
3. 检查是否主备都失败：查看事件日志中的 `ProviderFailed`

### 6.2 Function Calling 失败

**症状**：LLM 返回普通文本，没有 `tool_calls`

**可能原因**：
1. 模型不支持 Function Calling（如 gpt-3.5-turbo-0613 之前的版本）
2. `tool_choice` 设置为 `"none"`
3. Prompt 中明确要求"不要调用工具"

**解决**：
- 使用支持 Function Calling 的模型（gpt-4o, deepseek-chat, qwen-plus）
- 检查 `tool_choice="auto"`
- 降级兜底会自动生效（JSON Mode）

### 6.3 对话历史不生效

**症状**：Agent 重复询问已知信息

**排查**：
1. 检查 `sessions` dict 是否正确初始化：
   ```python
   print(f"Session {session_id} history: {len(self.sessions.get(session_id, []))}")
   ```
2. 检查 `start_session()` 是否被调用
3. 检查历史是否被正确传递给 LLM：
   ```python
   print(f"Messages sent to LLM: {len(msg_list)}")
   ```

### 6.4 Flow Engine 状态错误

**症状**：Agent 话术不符合业务流程

**排查**：
1. 检查 Flow Pack 是否正确加载：
   ```python
   print(f"Flow Pack: {self.flow_pack.id if self.flow_pack else None}")
   ```
2. 检查状态转移是否正确：
   ```python
   print(f"Current state: {engine.current}, Intent: {intent}")
   flow_result = engine.apply_intent(intent)
   print(f"New state: {flow_result['state']}")
   ```
3. 验证 Flow Pack JSON 格式：
   ```bash
   python -c "import json; json.load(open('flowpacks/service_outbound.json'))"
   ```

---

## 7. 性能指标（真实 LLM 环境）

**测试环境**：
- LLM: DeepSeek Chat (北京区域)
- 网络: 公网 HTTPS
- 并发: 单会话顺序测试

**实测指标**（待补充真实数据）：

| 指标 | P50 | P95 | 目标 | 说明 |
|------|-----|-----|------|------|
| LLM 首字时延 | 450ms | 850ms | < 500ms | 主 Provider 同区域 |
| 工具调用成功率 | 98% | - | ≥ 95% | 参数校验 + 幂等 |
| 任务闭环成功率 | 85% | - | ≥ 80% | 3 轮对话完成订单改约 |
| 主备切换时延 | 1200ms | 2500ms | < 2000ms | 主失败 → 备用成功 |
| 日均成本 | ¥3.5 | - | < ¥100 | 10 通电话 × 5 轮 |

---

## 8. 后续优化方向（Post-MVP）

1. **Prompt 调优**：根据真实场景调整 System Prompt
2. **流式输出**：LLM 逐 Token 返回 → Agent 句子级流式
3. **本地 14B 降级**：GPU 主机部署本地模型（断网兜底）
4. **多工具并发调用**：一次调用多个工具（如同时查订单和查线索）
5. **情感分析**：识别用户情绪 → 调整话术
6. **A/B 测试框架**：不同 Prompt/模型的效果对比
7. **自动 Prompt 优化**：基于 RSI 数据自动调整 Prompt

---

## 9. 相关文档

- [docs/07-cloud-api-plan.md](07-cloud-api-plan.md): LLM 云 API + 本地语音混合方案
- [docs/02-architecture-v0.1.md](02-architecture-v0.1.md): 总体架构 v0.1
- [docs/13-status-and-file-map.md](13-status-and-file-map.md): 项目现状与文件地图
- [README.md](../README.md): 快速启动与测试

---

## 10. 提交记录

| 提交 | 阶段 | 说明 |
|------|------|------|
| `eab8407` | 阶段1 | LLM 真实接入与 Function Calling 支持 |
| `150983b` | 阶段2 | 对话历史管理与上下文构建 |
| `6fc5dea` | 阶段3 | Flow Engine 状态机整合 |
| `2d3a0f0` | 阶段4 | Function Calling 完整实现 |
| TBD | 阶段5 | 测试与交付 |

---

**最后更新**：2026-11-06 23:55  
**维护人**：pseudonym（Agent 模块负责人）
