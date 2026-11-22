# 04 · Event Schema v0.1

> Owner：天命（Trace/Event）
> 消费方：调试、评测（湖中日月）、RSI、前端时间线（cx）、答辩回放
> 上游：[03 接口契约](03-interface-contract-v0.1.md)
> 原则：**事件是唯一事实来源**。调试、指标、失败归因、RSI 对比都从同一份事件流计算，不允许各自打一套日志。

---

## 1. Envelope（所有事件统一外壳）

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01J...",
  "seq": 42,
  "type": "ASRFinal",
  "ts": "2026-10-31T17:00:01.123+08:00",
  "ts_mono_ms": 123456.789,
  "clock_id": "app-7f3a",
  "session_id": "s_01J...",
  "turn_id": "t_0003",
  "trace_id": "tr_9f2a...",
  "span_id": "sp_1a2b3c4d",
  "parent_span_id": "sp_0f0e0d0c",
  "module": "voice.asr",
  "release_id": "rel_20261101_01",
  "mode": "voice",
  "payload": {},
  "latency_ms": null,
  "ok": true,
  "error": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 事件 schema 版本；只增不改 |
| `event_id` | 是 | 全局唯一，ULID |
| `seq` | 是 | 会话内单调递增，由 EventBus 分配；用于排序与断点续读 |
| `type` | 是 | PascalCase，见第 2 节 |
| `ts` / `ts_mono_ms` / `clock_id` | 是 | 见 03 第 2.2 节 |
| `session_id` | 是 | 除全局配置事件外 |
| `turn_id` | 视事件 | 会话级事件可为 null |
| `trace_id` / `span_id` | 视事件 | 有调用链的必须 |
| `module` | 是 | `media` / `voice.vad` / `voice.asr` / `voice.tts` / `orchestrator` / `agent` / `knowledge` / `tool` / `compliance` / `registry` / `eval` / `client` |
| `release_id` | 是 | 版本固定 |
| `mode` | 是 | `voice` / `text` / `eval` |
| `payload` | 是 | 事件专属字段 |
| `latency_ms` | 否 | 该事件对应阶段的耗时；必须配 `payload.latency_from` |
| `ok` / `error` | 是 | 失败事件 `ok=false`，`error` 用 03 第 2.5 节模型 |

**硬规则**：

- 事件 append-only；不更新、不删除。
- `seq` 只由 EventBus 分配；模块不得自造。
- 客户端事件由服务端摄入后**重新分配 `seq`**，保留 `payload.client_*` 原始时间。
- 任何模块不得写「只对自己有用」的日志格式；调试信息放 `payload.debug`，默认脱敏。

---

## 2. 事件字典

### 2.1 会话 / 轮次

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `SessionStarted` | orchestrator | 建会话成功 | `mode, client_version, scenario_id` | 全量 |
| `SessionReady` | orchestrator | WS/ICE 就绪 | `ice_servers, version_set` | 前端 |
| `VersionPinned` | registry | 会话固定版本 | `release_id, artifacts` | 复现/RSI |
| `TurnStarted` | orchestrator | 新轮开始 | `trigger: user\|client\|timeout` | 全量 |
| `TurnCommitted` | orchestrator | 输入确认 / 结果确认 | `phase: input\|result, text?, result?` | 评测/闭环 |
| `TurnAborted` | orchestrator | 打断/取消/异常 | `reason, elapsed_ms, last_state` | 归因 |
| `TimeoutFired` | orchestrator | 任一超时 | `scope, elapsed_ms, action` | 稳定性 |
| `SessionEnded` | orchestrator | 正常结束 | `reason, duration_ms, turns, outcome` | 全量 |
| `SessionFailed` | orchestrator | 不可恢复 | `error, last_state` | 稳定性 |
| `Degraded` | 任意 | 进入降级 | `component, from, to, reason` | 运维/答辩 |

### 2.2 语音 / VAD / 打断

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `ClientSpeechStarted` | client | 客户端本地 VAD 命中 | `client_ts_ms, client_mono_ms, energy_db` | 打断/时延 |
| `ClientLocalMuted` | client | 本地立即静音 | `client_mono_ms, turn_id` | 打断 |
| `SpeechStarted` | voice.vad | 服务端 VAD 命中 | `energy_db, confidence` | VAD 指标 |
| `SpeechEnded` | voice.vad | 用户停止说话 | `energy_db` | VAD |
| `VadEndpoint` | voice.vad | 端点判定 | `endpoint_delay_ms, reason` | 时延/VAD |
| `BargeInDetected` | orchestrator | 确认打断 | `during_turn_id, at_ms` | 打断 |
| `BargeInCancelled` | orchestrator | 误打断恢复 | `resume_from_sentence` | 打断 |
| `BargeInConfirmed` | orchestrator | 取消完成 | `stop_latency_ms` | 指标 |

### 2.3 ASR

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `ASRPartial` | voice.asr | 流式中间结果 | `text, confidence, audio_start_ms` | 调试/前端字幕 |
| `ASRFinal` | voice.asr | 最终结果 | `text, confidence, audio_start_ms, audio_end_ms, latency_ms` | 评测/时延 |
| `ASRError` | voice.asr | 超时/空/异常 | `error, audio_ms` | 归因 |

> `ASRPartial` 采样落盘：默认每 3 个 partial 记 1 个，避免日志膨胀；`ASRFinal` 全记。

### 2.4 Agent

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `AgentStarted` | orchestrator | 送入 Agent | `user_text, dialog_state_hash` | 时延 |
| `AgentFirstToken` | agent | 首个 token/首句开始 | `latency_ms` | **核心时延** |
| `AgentSentence` | agent | 产出一句 | `reply_id, sentence_id, text, final` | 合规/评测 |
| `AgentCompleted` | agent | 本轮结束 | `reply_id, outcome, result, latency_ms` | 闭环 |
| `AgentError` | agent | LLM/解析异常 | `error` | 归因 |
| `AgentCancelled` | agent | 取消生效 | `reply_id, at_sentence` | 打断 |

### 2.5 合规

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `ComplianceChecked` | compliance | 每次检查 | `scope: input\|sentence\|tool, decision, rule_ids, latency_ms` | 合规率 |
| `ComplianceRewritten` | compliance | REWRITE | `before, after, rule_ids` | 质检/答辩 |
| `ComplianceBlocked` | compliance | BLOCK | `text, rule_ids, severity, action_taken` | **硬门槛** |
| `ComplianceTransfer` | compliance | TRANSFER | `reason, queue` | 合规/闭环 |
| `ComplianceReviewPending` | compliance | 异步复核排队 | `text_hash, rule_ids` | 误杀分析 |

### 2.6 工具

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `ToolCalled` | tool | 发起调用 | `tool, args_redacted, call_id, idempotency_key, deadline_ms` | 工具正确率 |
| `ToolReturned` | tool | 成功 | `tool, data_summary, latency_ms, attempt, cached` | 回写/评测 |
| `ToolFailed` | tool | 失败 | `tool, error, latency_ms, attempt` | 归因 |
| `ToolRetried` | tool | 重试 | `tool, attempt, reason, backoff_ms` | 稳定性 |
| `ToolCachedReplay` | tool | 幂等重放 | `tool, key, original_latency_ms` | 幂等验证 |

### 2.7 TTS / 播放

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `TTSStarted` | voice.tts | 开始合成 | `reply_id, sentence_id, text_hash` | 时延 |
| `TTSFirstChunk` | voice.tts | 首包 | `latency_ms, bytes` | **核心时延** |
| `TTSChunkSent` | orchestrator | 下行一块 | `seq, bytes, turn_id, reply_id` | 调试（采样） |
| `TTSCompleted` | voice.tts | 合成结束 | `total_ms, chunks` | 质量 |
| `TTSCancelled` | voice.tts | 取消生效 | `at_chunk, elapsed_ms` | 打断 |
| `TTSFailed` | voice.tts | 失败 | `error` | 降级 |
| `PlaybackStarted` | client | 开始播放 | `client_mono_ms` | 播放 |
| `PlaybackFirstFrame` | client | **首个采样播出** | `client_mono_ms, audio_ctx_time, turn_id, reply_id` | **官方时延关键** |
| `PlaybackEnded` | client | 播放完成 | `client_mono_ms, played_ms` | 打断/质量 |
| `PlaybackCancelled` | client | 本地静音 | `client_mono_ms, played_ms` | 打断 |

### 2.8 取消 / 版本 / 评测

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `CancelRequested` | orchestrator | 发起取消 | `turn_id, reason, target` | 打断 |
| `CancelAcked` | agent/tts/tool | 确认取消 | `target, latency_ms` | 打断 |
| `CancelSlow` | orchestrator | >300ms 未确认 | `target, elapsed_ms` | 稳定性 |
| `ReleasePromoted` | registry | 晋升 | `from, to, gate` | RSI |
| `ReleaseRolledBack` | registry | 回滚 | `from, to, reason` | RSI |
| `EvalCaseStarted` | eval | 单案例开始 | `scenario_id, release_id` | 评测 |
| `EvalCaseEnded` | eval | 单案例结束 | `metrics, failure_labels` | 评测/RSI |
| `CandidateCreated` | eval | 候选生成 | `candidate_id, diff_summary` | RSI |
| `RegressionCompleted` | eval | 回归完成 | `baseline, candidate, delta, ci` | RSI |
| `PromotionDecision` | eval/registry | 晋升决策 | `decision, reasons` | RSI |

### 2.9 LLM Provider（云 LLM 专用）

| type | 生产者 | 触发时机 | 关键 payload | 主要消费 |
|---|---|---|---|---|
| `ProviderCalled` | agent/router | 发起云 LLM 请求 | `provider, model, request_id, prompt_version` | 时延/成本 |
| `ProviderFirstToken` | agent/router | 首个 token | `latency_ms` | **TTFT** |
| `ProviderReturned` | agent/router | 成功返回 | `latency_ms, usage, cost_cny` | 成本 |
| `ProviderFailed` | agent/router | 失败 | `error, status, retryable` | 归因 |
| `ProviderFailover` | agent/router | 主备切换 | `from, to, reason` | 稳定性 |
| `BudgetExceeded` | agent/router | 预算熔断 | `scope, limit, spent` | 成本 |

> ASR/TTS 默认本地，不产生 Provider 事件；若接云应急，复用 `Provider*` 并加 `kind: asr|tts`。

### 2.10 关键聚合事件：`TurnLatency`

每次 `TurnCommitted(phase=result)` 后由 M3 产出，**所有时延指标只从它算**：

```json
{
  "type": "TurnLatency",
  "payload": {
    "t0_last_voiced_ms": 1710000000000,
    "t1_endpoint_ms": 1710000000250,
    "t2_asr_final_ms": 1710000000350,
    "t3_agent_start_ms": 1710000000370,
    "t4_first_token_ms": 1710000000720,
    "t5_first_sentence_ms": 1710000000840,
    "t6_compliance_ms": 1710000000865,
    "t7_tts_first_chunk_ms": 1710000001045,
    "t8_first_downlink_ms": 1710000001075,
    "t9_first_playback_ms": 1710000001135,
    "e2e_ms": 1135,
    "e2e_official_proxy_ms": 885,
    "vad_to_playback_ms": 885,
    "clock_offset_ms": 12.4,
    "clock_suspect": false,
    "barge_in_stop_ms": null
  }
}
```

- `e2e_ms = t9 - t0`（保守口径）。
- `e2e_official_proxy_ms = t9 - t1`（官方可能从端点算）。
- `clock_suspect=true` 时该条不计入正式指标，只进调试。

---

## 3. 客户端埋点（cx 必做）

| 埋点 | 实现 | 用途 |
|---|---|---|
| `playback_first_frame` | `AudioWorklet` 第一个输出采样时，用 `AudioContext.currentTime` + `performance.now()` 记录 | 官方时延 |
| `local_muted` | 本地 VAD 命中后立即 `gain=0` 并上报 | 打断停止时延 |
| `client_speech_started` | 本地能量/零率阈值 + 200ms 持续 | 打断识别 |
| `clock_sync` | 每 30s ping/pong，取 RTT 最小样本估计偏移 | 跨端时间对齐 |

要求：

- 不在 `onended` 才报 `PlaybackEnded`；用实际播放进度。
- 所有时间戳用 `performance.now()`（单调），不要 `Date.now()`。
- 上报失败不能阻塞播放；本地缓存，重连后补发。

---

## 4. 存储与回放

### 4.1 目录

```text
data/
  sessions/2026-11-01/s_01J.../
    meta.json          # 会话元信息、version_set、结果
    events.jsonl       # 全量事件（append-only）
    result.json        # 结构化回写结果
    metrics.json       # 本会话聚合指标
    audio/             # 可选：上行/下行 wav（脱敏后）
  evals/2026-11-01/es_.../
    events.jsonl
    result.json
  index/
    events.db          # SQLite 索引
```

### 4.2 SQLite 索引

```sql
CREATE TABLE sessions(
  session_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
  mode TEXT, release_id TEXT, outcome TEXT, turns INTEGER
);
CREATE TABLE events(
  event_id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER,
  turn_id TEXT, trace_id TEXT, type TEXT, ts TEXT,
  module TEXT, ok INTEGER, latency_ms REAL, payload_json TEXT
);
CREATE INDEX idx_events_session_seq ON events(session_id, seq);
CREATE INDEX idx_events_type ON events(type);
CREATE INDEX idx_events_turn ON events(turn_id);
```

> 不引入 ClickHouse/Kafka。单机 SQLite WAL + JSONL 足够；分析用 DuckDB 直接读 JSONL/Parquet。

### 4.3 回放

- `ReplayReader(session_id)` 按 `seq` 排序产出事件。
- 前端时间线：事件类型 → 颜色/泳道；点击事件看 payload。
- 评测：从 `TurnLatency` 算 P50/P95；从 `ComplianceBlocked` 算合规召回；从 `ToolCalled/Returned` 算工具正确率。
- 复现：`session replay --session s_... --to-seq N` 重放前 N 个事件给 Fake 依赖，验证状态机。

---

## 5. 隐私与安全

| 数据 | 处理 |
|---|---|
| 原始音频 | 默认不落盘；需要时存 `audio/`，本地加密，赛后删除 |
| 转写文本 | 本地保存；导出/展示前脱敏手机号、身份证、地址、姓名 |
| 工具参数 | 事件里存 `args_redacted`；完整参数只在工具审计表，访问受控 |
| Prompt | 只记 `prompt_version` + hash；完整 prompt 仅 debug 模式、脱敏 |
| 密钥 | 永不入事件、永不入 Git |
| 事件导出 | 默认脱敏版本；原始版本需架构师批准 |

`payload.pii` 字段标记：`["phone","id_card","address"]`，供评测与导出过滤器使用。

---

## 6. 校验与 CI

```text
contracts/
  schemas/event.v1.json
  fixtures/*.jsonl
  validate_events.py
```

- 每个事件必须通过 JSON Schema（required + 类型；`additionalProperties: true` 允许扩展）。
- 未知 `type` 记 warning，不阻塞；连续 3 次未知则 CI 失败（防止私加事件）。
- `seq` 必须严格递增；重复/跳号失败。
- 事件顺序断言见 03 第 3 节。
- 每次 PR 跑 `make contract-test`；主分支每晚跑全量夹具。

---

## 7. 指标 → 事件映射（评测只认这张表）

| 指标 | 计算来源 |
|---|---|
| 首字 P50/P95 | `TurnLatency.e2e_ms` / `e2e_official_proxy_ms` |
| LLM TTFT | `ProviderFirstToken.latency_ms` |
| 打断停止 | `BargeInConfirmed.stop_latency_ms` 或 `PlaybackCancelled - ClientSpeechStarted` |
| VAD 端点延迟 | `VadEndpoint.endpoint_delay_ms` |
| ASR CER | `ASRFinal.text` vs 参考转写（离线） |
| 意图/槽位 | `AgentCompleted.result` + `TurnCommitted(phase=result)` |
| 工具正确率 | `ToolCalled.args_redacted` + `ToolReturned.data_summary` |
| 合规召回/误杀 | `ComplianceChecked` + `ComplianceBlocked/Rewritten` |
| 闭环率 | `TurnCommitted(phase=result).result.outcome` + 六阶段事件齐全 |
| 失败归因 | `TurnAborted/TimeoutFired/SessionFailed` + 对应 `error` |
| 成本 | `ToolCalled`/`AgentStarted` 里 token 计数 + `TTSChunkSent.bytes` |

---

## 8. 事件覆盖门槛（Gate 检查）

| 门槛 | 必须存在的事件 | 检查方式 |
|---|---|---|
| ≥3 轮连续对话 | `TurnStarted/Committed` ≥3，且无 `TurnAborted(deadlock)` | 回放脚本 |
| 首字 P50≤2s | `TurnLatency` 每条有 `t0..t9`，`clock_suspect=false` | 报表 |
| 完整闭环 | `SessionStarted` 后六阶段 `TurnCommitted` + `ToolReturned` + `result.json` | 场景断言 |
| 合规 | `ComplianceChecked` 覆盖每条 Agent 句；`ComplianceBlocked` 有后续动作 | 覆盖率 |
| 打断 | `ClientSpeechStarted → CancelRequested → CancelAcked → TurnAborted` | 顺序断言 |

---

## 9. 变更规则

- 事件只增不改：新增字段可选；删除/改义必须升 `schema_version` 并双写一个版本周期。
- 新增事件必须同时更新：本文档、`event.v1.json`、`指标映射`（如有）、至少一条夹具。
- 任何人不得在业务代码里拼「临时日志」代替事件；临时调试用 `payload.debug`，上线前清理。
