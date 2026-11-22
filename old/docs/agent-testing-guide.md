# Agent LLM 集成测试指南

> 更新时间：2026-11-07
> 分支：`feature/agent-llm-integration`
> 状态：✅ 全部测试通过（21/21）

---

## 1. 测试环境准备

### 1.1 激活虚拟环境

```bash
# 已有虚拟环境，直接激活
source /home/zhoujintian/AI-call/.venv/bin/activate

# 首次使用，创建虚拟环境
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.2 配置环境变量

```bash
# 使用 Fake Agent（无需 LLM Key，适合快速验证）
export AGENT_PROVIDER=fake

# 使用真实 LLM（需配置 .env）
cp .env.example .env
# 编辑 .env 填写 LLM API Key
export AGENT_PROVIDER=llm
```

---

## 2. 运行测试

### 2.1 完整测试套件

```bash
# 运行全部 21 个契约测试
source .venv/bin/activate
pytest tests/test_contract.py -v

# 预期结果：21 passed in ~8s
```

**实际运行结果**（2026-11-07）：

```
============================= test session starts ==============================
platform linux -- Python 3.14.8, pytest-8.4.2, pluggy-1.7.0
rootdir: /home/zhoujintian/AI-call
configfile: pytest.ini
plugins: anyio-4.16.0
collected 21 items

tests/test_contract.py::test_three_turns_and_events PASSED               [  4%]
tests/test_contract.py::test_tool_idempotency PASSED                     [  9%]
tests/test_contract.py::test_tool_failure_retry_and_error PASSED         [ 14%]
tests/test_contract.py::test_invalid_tool_args PASSED                    [ 19%]
tests/test_contract.py::test_compliance_rewrite_and_block PASSED         [ 23%]
tests/test_contract.py::test_compliance_events_are_valid PASSED          [ 28%]
tests/test_contract.py::test_barge_in_cancels_turn PASSED                [ 33%]
tests/test_contract.py::test_http_api_smoke PASSED                       [ 38%]
tests/test_contract.py::test_registry_candidate_promote_rollback PASSED  [ 42%]
tests/test_contract.py::test_llm_failover_and_events PASSED              [ 47%]
tests/test_contract.py::test_llm_budget_and_unconfigured PASSED          [ 52%]
tests/test_contract.py::test_knowledge_search PASSED                     [ 57%]
tests/test_contract.py::test_agent_uses_knowledge_and_cites PASSED       [ 61%]
tests/test_contract.py::test_flowpack_load_and_transition PASSED         [ 66%]
tests/test_contract.py::test_eval_runner_metrics PASSED                  [ 71%]
tests/test_contract.py::test_livekit_token_and_voice_status PASSED       [ 76%]
tests/test_contract.py::test_llm_agent_tool_path PASSED                  [ 80%]
tests/test_contract.py::test_ws_tts_metadata_turn_result_and_hangup PASSED [ 85%]
tests/test_contract.py::test_rsi_gate_pass_and_fail PASSED               [ 90%]
tests/test_contract.py::test_report_from_fixture PASSED                  [ 95%]
tests/test_contract.py::test_ws_golden_fixture PASSED                    [100%]

======================== 21 passed, 1 warning in 7.71s =========================
```

✅ **全部通过**，向后兼容性验证成功。

### 2.2 关键测试覆盖

| 测试用例 | 覆盖功能 | 验证点 |
|---------|---------|--------|
| `test_llm_agent_tool_path` | LLM Agent 工具调用 | Function Calling / JSON Mode 降级 |
| `test_llm_failover_and_events` | LLM 主备切换 | 主失败 → 备用成功 + 事件审计 |
| `test_three_turns_and_events` | 多轮对话 | 对话历史 + 状态保持 |
| `test_flowpack_load_and_transition` | Flow Engine | 状态转移 + 槽位管理 |
| `test_tool_idempotency` | 工具幂等性 | 相同请求 ID 不重复执行 |
| `test_compliance_rewrite_and_block` | 合规检查 | 改写敏感词 + 阻断违规内容 |
| `test_barge_in_cancels_turn` | 打断处理 | CancelToken 生效 |

---

## 3. 手动验证（真实 LLM）

### 3.1 启动服务

```bash
# 配置真实 LLM Key
export AGENT_PROVIDER=llm
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
```

### 3.2 前端集成测试

打开浏览器访问 `http://localhost:8081/rtc/` 进行 3 轮对话：

**测试场景：服务外呼改约**

1. **用户**："我想改约订单"
   - **Agent**："好的，请问您的订单号是多少？"
   - ✅ 验证：LLM 理解意图 `intent=reschedule`

2. **用户**："订单号是 A12345678"
   - **Agent**："好的，订单 A12345678 当前预约时间是明天上午 10 点，您想改到什么时间？"
   - ✅ 验证：工具调用 `order.query`，对话历史保留订单号

3. **用户**："改到明天下午 3 点"
   - **Agent**："已为您将订单 A12345678 改约到明天下午 3 点，还有什么可以帮您的吗？"
   - ✅ 验证：工具调用 `order.reschedule`，使用历史中的订单号

### 3.3 验证 Function Calling

查看服务端日志，应看到：

```
[2026-11-07 00:05:12] ProviderCalled: primary (deepseek-chat)
[2026-11-07 00:05:13] ProviderToolCall: order.query (order_id=A12345678)
[2026-11-07 00:05:13] ToolCalled: order.query v1.0.0
[2026-11-07 00:05:13] ToolReturned: {"order_id": "A12345678", "status": "scheduled", ...}
[2026-11-07 00:05:13] ProviderReturned: 1245 tokens in 850ms
```

### 3.4 验证主备切换

```bash
# 修改 .env 让主 Provider 失败
LLM_PRIMARY_BASE_URL=https://unreachable.example.com/v1
LLM_SECONDARY_BASE_URL=https://api.deepseek.com/v1

# 重启服务，再次测试
# 应看到日志：
# [2026-11-07 00:10:15] ProviderFailed: primary (timeout after 3000ms)
# [2026-11-07 00:10:15] ProviderFailover: secondary
# [2026-11-07 00:10:16] ProviderReturned: 1340 tokens in 1120ms
```

---

## 4. 性能验证

### 4.1 首字时延测试

```bash
# 使用真实 LLM 运行性能测试
source .venv/bin/activate
pytest tests/test_contract.py::test_llm_agent_tool_path -v --durations=10
```

**预期指标**（真实 LLM 环境）：

| 指标 | 目标 | 说明 |
|------|------|------|
| LLM 首字时延 P50 | < 500ms | 主 Provider 同区域 |
| LLM 首字时延 P95 | < 1000ms | 含少量冷启动 |
| 工具调用成功率 | ≥ 95% | 参数校验 + 幂等 |
| 主备切换时延 | < 2000ms | 主失败 → 备用成功 |

### 4.2 压力测试（可选）

```bash
# 使用 locust 进行并发测试
pip install locust
locust -f tests/locustfile.py --host=http://localhost:8081
```

---

## 5. 故障排查

### 5.1 测试失败排查

**症状**：`test_llm_agent_tool_path` 失败，提示 "LLM returned None"

**可能原因**：
1. `AGENT_PROVIDER=llm` 但未配置 API Key
2. LLM API 超时或限流
3. Function Calling 格式错误

**解决**：
```bash
# 检查环境变量
echo $AGENT_PROVIDER
cat .env | grep LLM_PRIMARY

# 切换到 Fake Agent 验证架构
export AGENT_PROVIDER=fake
pytest tests/test_contract.py::test_llm_agent_tool_path -v
```

### 5.2 对话历史不生效

**症状**：Agent 重复询问已知信息

**排查**：
```python
# 在 app/agent/llm_agent.py 中添加调试日志
print(f"[DEBUG] Session {session_id} history length: {len(self.sessions.get(session_id, []))}")
print(f"[DEBUG] Messages sent to LLM: {len(msg_list)}")
```

**验证**：
- 第 1 轮：`history length: 0` → `Messages sent: 2` (system + user)
- 第 2 轮：`history length: 2` → `Messages sent: 4` (system + history[2] + user)
- 第 3 轮：`history length: 4` → `Messages sent: 6` (system + history[4] + user)

### 5.3 Function Calling 失败

**症状**：LLM 返回普通文本，没有 `tool_calls`

**排查**：
```bash
# 检查 LLM 模型是否支持 Function Calling
curl -X POST $LLM_PRIMARY_BASE_URL/chat/completions \
  -H "Authorization: Bearer $LLM_PRIMARY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [{"type": "function", "function": {"name": "test", "parameters": {}}}]
  }'
```

**降级兜底**：即使 Function Calling 失败，Agent 会自动降级到 JSON Mode。

---

## 6. 回归测试清单

每次修改后必须运行的测试：

- [ ] `pytest tests/test_contract.py -v` 全部通过
- [ ] Fake Agent 模式正常工作（`AGENT_PROVIDER=fake`）
- [ ] LLM Agent 模式正常工作（`AGENT_PROVIDER=llm`）
- [ ] 前端 `/rtc/` 页面 3 轮对话成功
- [ ] 主备切换正常（日志中有 `ProviderFailover`）
- [ ] Function Calling 正常（日志中有 `ProviderToolCall`）
- [ ] 对话历史正常（第 2 轮不重复询问第 1 轮信息）
- [ ] Flow Engine 状态转移正常（日志中有状态变化）

---

## 7. 交付标准

### 7.1 代码质量

- ✅ 全部现有测试通过（21/21）
- ✅ 无新增 linter 警告
- ✅ 向后兼容（Fake Agent 仍可用）
- ✅ 环境变量配置完整（`.env.example` 更新）

### 7.2 文档完整性

- ✅ [docs/agent-llm-integration.md](agent-llm-integration.md): 完整开发文档
- ✅ [docs/agent-testing-guide.md](agent-testing-guide.md): 本测试指南
- ✅ `.env.example`: LLM 配置模板
- ✅ `config/app.example`: 完整配置示例

### 7.3 功能完整性

- ✅ LLM 真实接入（主备切换 + Function Calling）
- ✅ 对话历史管理（Session 级存储 + 上下文构建）
- ✅ Flow Engine 整合（状态机约束业务流程）
- ✅ Function Calling 实现（工具声明 + 结构化调用 + 降级）
- ✅ 测试与文档（单元测试 + E2E + 文档）

---

## 8. 下一步

### 8.1 合并前检查

```bash
# 1. 确认所有提交都在分支上
git log --oneline feature/agent-llm-integration ^master

# 2. 运行完整测试
pytest tests/ -v

# 3. 确认无未提交修改
git status

# 4. 推送到远程
git push origin feature/agent-llm-integration

# 5. 创建 PR
gh pr create --title "feat(agent): Agent LLM 集成开发完成" \
  --body "$(cat docs/agent-llm-integration.md)"
```

### 8.2 部署验证

```bash
# 1. 在测试环境部署
export AGENT_PROVIDER=llm
uvicorn app.main:app --host 0.0.0.0 --port 8081

# 2. 真实场景验证（至少 10 通电话）
# 3. 监控指标：首字时延、任务成功率、错误率
# 4. 收集反馈并迭代
```

### 8.3 后续优化（Post-MVP）

1. **Prompt 调优**：根据真实场景调整 System Prompt
2. **流式输出**：LLM 逐 Token 返回 → Agent 句子级流式
3. **本地 14B 降级**：GPU 主机部署本地模型（断网兜底）
4. **多工具并发调用**：一次调用多个工具
5. **情感分析**：识别用户情绪 → 调整话术
6. **A/B 测试框架**：不同 Prompt/模型的效果对比

---

**最后更新**：2026-11-07 00:15  
**测试人**：pseudonym（Agent 模块负责人）
