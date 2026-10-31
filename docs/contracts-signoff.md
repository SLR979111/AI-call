# 契约会签（Contract Sign-off）

> 目标：四位 owner 各自用 Fake 依赖跑通契约测试，确认 03/04 的字段与语义可以落地。
> 会签后冻结 2 周；破坏性修改提前 24h 通知并升版本。

## 1. 会签前自动检查

```bash
# 安装依赖后执行
python contracts/validate.py contracts/fixtures
python -m pytest tests/test_contract.py -q
```

两项都通过，才进入人工会签。

## 2. 会签表

| Owner | 检查项 | 结果 | 日期 |
|---|---|---|---|
| 天命（架构/集成） | Envelope、状态机、取消、Tool Gateway、事件顺序 | ☐ | |
| SinCircle（语音） | `AsrStream`/`TtsStream`/`VadEvent` 可被 Fake 实现，错误可注入 | ☐ | |
| pseudonym（Agent） | `AgentRequest`/`AgentChunk` 能表达追问/工具/转人工/结束 | ☐ | |
| 湖中日月（评测） | 事件能算出 TurnLatency、工具/合规指标；headless 可跑 | ☐ | |
| cx（前端） | WS 消息、`playback_first_frame`、`local_muted` 可实现 | ☐ | |

## 3. 已确认的事实（v0.1 代码）

- `EventBus` 写入 `data/sessions/<session_id>/events.jsonl` + SQLite 索引。
- `TurnLatency` 含 `t0..t9`、`e2e_ms`、`e2e_official_proxy_ms`。
- `ToolGateway` 支持 schema 校验、幂等、超时、1 次重试、审计事件。
- `/eval/sessions` 与 `/eval/run` 可无浏览器跑文本批次；默认套件 5/5。
- `/metrics` 提供 `aicall_up`、`aicall_sessions`。
- `/registry/*` 支持 candidate → production → previous 与回滚。
- LLM Router 支持主/备、超时、熔断、预算；无 Key 返回 `LLM_NOT_CONFIGURED`。
- `AGENT_PROVIDER=llm` 时走 `LLMAgent`（JSON 决策 + ToolClient），默认仍为 Fake。
- RAG 检索、Flow Pack、LiveKit Token 边界已落地（真实模型/媒体服务待接）。
- 契约测试：4 个 fixture / 47 条事件通过 schema；20 个测试通过（含打断、合规、幂等、超时、Registry、LLM、RAG、Flow、LLMAgent、WS 元数据/挂断、RSI 门禁、报告）。
- v0.2 补丁（回应 cx 评审）：[11-contract-v0.2-patch.md](11-contract-v0.2-patch.md)；G1–G8 已给协议与代码入口，G9 已解决。

## 4. 变更记录

| 日期 | 版本 | 变更 | 影响 |
|---|---|---|---|
| 2026-10-31 | v0.1 | 初始契约与 Fake 骨架 | 全部模块 |
| 2026-10-31 | v0.2 | 补 G1–G9：音频帧/媒体信令/TTS 元数据/降级/挂断/鉴权/枚举/b3 | 全部模块 |

## 5. cx 评审结论（v0.2）

> cx：有条件同意会签。
> 生效条件：v0.2 补齐 G1–G8；G9 已通过最新仓库解决。
> 未完成但已明确：G3 真实 TTS 时间轴、G11 缓冲恢复、G14 工具进度、G20 AEC 能力 → W2 实现，不阻塞 W1 前端开工。
> 前端承诺：命名对齐 03/04，不私自定义对方可接受但契约没有的字段。
