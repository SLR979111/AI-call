# 22 · 84 Agent 接入调试

> 状态：实验分支，仅用于验证 Agent / LLM 接入。
> 入口：`http://127.0.0.1:8084/rtc/`
> 验证通过后再更新 81（主开发）和 91（比赛）。

## 0. 更新内容（待检查，不自动提交）

- 84 LLM 接入：`app/main.py` 注入 `POP_CONTEST_CONTEXT`；
- `app/agent/llm_agent.py`：确认词处理、短确认跳过 RAG、真实上下文优先回复；
- 本文件：当前主任务、速度目标、双路线说明。

**注意：不得直接提交/上传，先走更新内容检查。**

## 1. 这里在调什么

```text
Orchestrator
  → LLMAgent
    → LLMRouter
      → Ollama / Qwen3-4B
```

84 已接入：

- `POP_CONTEST_CONTEXT`：request_id / brand / reject_l1/l2/l3；
- 确认词处理：可以/好/方便/是/对/嗯；
- 短确认不触发无关 RAG；
- 有真实上下文时，LLM 的具体回复不被 FlowEngine 的泛化话术覆盖；
- 前端最新回复 + 后端事件面板。

## 2. 当前主要任务

```text
1. 完成 AI 接入：Agent 能理解真实单子、确认、拒绝、追问。
2. 把回复速度调整到可用：当前单轮约 5–14 秒，目标 P50 ≤2 秒。
```

当前瓶颈：

- Qwen3-4B thinking token 占比高；
- 4B 单轮 5–14 秒；
- 本地 GPU 8GB，并发会排队；
- 还不能作为实时主链路。

下一步速度方向：

```text
关闭/压缩 thinking
缩短 max_tokens / 首句
小模型/云端模型对照
常见确认走确定性快路径
缓存 RAG 结果
```

## 3. 双路线（保留，不合并）

### 路线 A：稳定级联（之前的主路线）

```text
81 主开发 / 91 比赛
ASR → Agent（规则或 LLM）→ TTS
半双工、回合制
```

- 81：主开发，规则版；
- 91：比赛入口，规则/Resilient；
- 84：只作为 LLM Agent 实验，不是双工；
- 这条路线必须保持可交付、可回退。

### 路线 B：伪双工实验

```text
feature/duplex-adapter（本地 worktree .duplex-adapter）
  MockDuplexProvider
  DuplexSession / DuplexProvider
  scripts/duplex_mock_demo.py
  手动页 8096
```

- 只有事件协议和 Mock，没有真实音频；
- 没有接入 `/rtc`；
- 没有真实双工模型；
- 8GB 显存不足以跑 Moshi/MiniCPM；
- **不合并到路线 A，不混入 84 的 Agent 提交**。

### 结论

```text
A：稳定级联，81/91 默认使用
B：伪双工实验，仅备选；手动启动 8096
84：Agent LLM 实验，不是双工
```

**默认路线是 A；B 只作备选，不得默认启用；84 也不是默认入口。**

“双工”不是 84 的一部分；不要因为 84 有 LLM 就把它叫双工。

## 4. 验收后如何更新

84 验证通过后：

1. 把 `LLMAgent` 上下文/确认修复合并到主分支；
2. 81 增加 `AGENT_PROVIDER=llm` 可选；
3. 91 增加 `--agent rule|llm`，保留比赛 profile；
4. 双工适配层单独评审，不混入 84 的 Agent 接入提交；
5. 路线 A 和 B 都保留，不做二选一。
