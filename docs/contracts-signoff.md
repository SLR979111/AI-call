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
| cx（前端） | WS 消息、`playback_first_frame`、`local_muted` 可实现 | ☑ **v0.2.1 复签；C1–C6 接受；P1–P5 字段澄清** | 2026-10-31 |

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
| 2026-10-31 | v0.2.1 | cx 复签：C1–C6 接受 + `turn_aborted`；P1–P5 字段级澄清 | cx、天命 |

## 5. cx 评审结论（v0.2）

> cx：有条件同意会签。
> 生效条件：v0.2 补齐 G1–G8；G9 已通过最新仓库解决。
> 未完成但已明确：G3 真实 TTS 时间轴、G11 缓冲恢复、G14 工具进度、G20 AEC 能力 → W2 实现，不阻塞 W1 前端开工。
> 前端承诺：命名对齐 03/04，不私自定义对方可接受但契约没有的字段。

## 6. v0.2.1 复签与 P1–P5（2026-10-31）

> 依据：`docs/reply_cx_v0.2.1.md`；提交 `cbd31b3` / tag `e2e-ws` / 契约点 `v0.2.1-cx-conditions`。
> cx：**C1–C6 全部接受；`turn_aborted` 接受；P1–P5 为字段级澄清，不新增消息。**

| # | 澄清 | 落地 |
|---|---|---|
| P1 | `b0_mono_ms` / `client_mono_ms` / `last_frame_played_mono_ms` / `stop_latency_ms` 分开 | ✅ 代码已透传；`TurnLatency.barge_in_stop_ms` 记录 client 值 |
| P2 | `tts_completed.interrupted=true` 的 `total_ms` = reply 音频全长，另加 `played_ms` | ✅ `played_ms` 已加；`total_ms` 保持全长估算 |
| P3 | `POST /sessions/{id}/token` 返回完整 `ws_url`；重连=新 WS，事件不重放 | ✅ 已返回 `ws_url?token=`；文档已写 |
| P4 | `turn_aborted.reason` 枚举：`BARGE_IN/TIMEOUT/SESSION_END/COMPLIANCE_BLOCK/USER_HANGUP/INTERNAL` | ✅ 代码已归一化，未知值→`INTERNAL` |
| P5 | 把 `e2e-ws` 的收发顺序落成 `contracts/fixtures/ws_session_bargein.jsonl` | ✅ 子 agent 抓包，见文件 |

> 仓库同步说明：当前工作区已包含 `contracts/`、`app/`、`tests/`、`web/`，最新测试 **20 passed**；cx 那份文档里的“18 tests”是 v0.2 时点。
