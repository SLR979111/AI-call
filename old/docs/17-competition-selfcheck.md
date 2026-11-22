# 17 · 赛事资料缺口与自建基线（v1.0）

> 背景：赛事方确认无法提供官方 Mock 接口、统一评测集、时延工具、合规红线和参考架构。  
> 本文按 `docs/reference/competition-brief.pdf` 的“企业提供支持”逐项自建替代基线。  
> 性质：**团队自建、可复现、非官方口径**；官方资料到位后应以官方版本覆盖本文。

## 1. 与 Brief 的对应关系

| Brief 承诺 | 自建替代物 | 位置 |
|---|---|---|
| 脱敏业务资料包 | 已有 POP 话术/QA/规则 + 本文件的业务假设 | `knowledge/pop_audit.json`、`docs/16` |
| Mock 业务接口/沙箱 | 自建 Mock Business API | `mock_api/server.py` |
| 统一评测用例/口径 | 自建 12 条用例 + 评分表 | `eval/selfcheck/` |
| 统一时延工具/指标 | 复用 `scripts/metric_check.py`，本文件固化口径 | `scripts/metric_check.py` |
| 参考架构/技术文档 | 现有 M1–M9 架构 + 本文件映射 | `docs/02`、`docs/03`、`docs/13` |
| 合规红线 | 本文件第 6 节自建清单 | 本文 |

## 2. Mock Business API

### 2.1 启动

```powershell
$env:PYTHONPATH = "H:\aicall\.web311"
H:\pytorch-env\python.exe -m uvicorn mock_api.server:app --host 127.0.0.1 --port 8095
```

环境变量：

```text
MOCK_API_KEY=dev-mock-key   # 仅本地/演示，生产不得使用
```

### 2.2 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查，不需要鉴权 |
| GET | `/v1/customers/{customer_id}` | 名单/商家查询 |
| GET | `/v1/orders/{order_id}` | 订单查询 |
| POST | `/v1/tickets` | 工单/回电登记 |
| GET | `/v1/tickets/{ticket_id}` | 查询登记结果 |

鉴权：

```http
X-API-Key: dev-mock-key
```

工单登记必须带：

```http
Idempotency-Key: <stable-key>
```

请求：

```json
{
  "request_id": "Q983001",
  "customer_id": "CUST-001",
  "type": "callback",
  "summary": "商家要求晚上回电",
  "payload": {"preferred_time": "19:00"}
}
```

成功：

```json
{
  "ticket_id": "T...",
  "request_id": "Q983001",
  "status": "created",
  "created_at": "2026-11-08T...",
  "idempotent_replay": false
}
```

错误 envelope：

```json
{"error": {"code": "ORDER_NOT_FOUND", "message": "order not found", "retryable": false}}
```

幂等规则：

- 同一 `Idempotency-Key` + 同一请求体 → 返回原结果；
- 同一 key + 不同请求体 → `409 IDEMPOTENCY_CONFLICT`；
- 缺 key → `400 IDEMPOTENCY_KEY_REQUIRED`。

### 2.3 数据边界

- 全部是合成/脱敏数据，不含真实手机号、地址、商家信息；
- 只监听 `127.0.0.1`；
- 不得向真实号码发起外呼；
- 只用于接口契约、幂等和写回演练，不代表真实业务系统。

## 3. 自建评测集

### 3.1 用例

`eval/selfcheck/cases.json`：

- 前 6 条：可自动执行的 POP 基线（原因、整改、京麦、回电、投诉）；
- 后 6 条：人工对抗检查（错人、隐私、复述、承诺、改约、含糊）。

用法：

- 自动 6 条可用现有 `EvalRunner`/`pytest tests/test_pop_audit.py::test_pop_eval_suite` 跑；
- 人工 6 条按 `checks` 字段逐项判定，记录在自测报告中；
- 所有结果必须保留原始日志和会话 ID。

### 3.2 评分表

`eval/selfcheck/rubric.json`：

| 维度 | 权重 |
|---|---|
| 实时语音交互 | 25 |
| 任务完成 | 30 |
| 业务知识与工具 | 20 |
| 合规与质检 | 15 |
| 工程与可复现 | 10 |

硬门槛：

- 至少 3 轮连续语音；
- 首字 P50 ≤ 2000ms；
- 至少 1 个六阶段闭环；
- 无严重合规违规。

说明：权重/阈值是**团队自定**，不是官方分数；官方 rubric 到位后直接替换。

## 4. 时延测量口径

### 4.1 定义

```text
首字时延 = 用户最后一个有声帧
          → 浏览器首次收到可播放的 agent 首字/首段音频
```

不是“ASR final → 文本返回”，也不是“整句 TTS 完成”。

### 4.2 样本与统计

- 预热 2 轮，不计入；
- 至少 30 轮有效对话；
- 报告 P50、P95、样本数、设备、模型、网络；
- 硬门槛只看 P50 ≤ 2000ms；P95 作为观察值。

### 4.3 工具

使用：

```bash
make metric-latency
# 或
python scripts/metric_check.py --base http://127.0.0.1:8081 --section latency
```

要求同时保留：

- 服务端 `TurnLatency` / `OutboundOpeningLatency` JSONL；
- 浏览器端首帧日志；
- 测试日期、机器、麦克风、模型版本。

## 5. 六阶段任务闭环

```text
OPEN → IDENTITY_CONFIRM → NEED_CONFIRM → OFFER → RESULT_CONFIRM → WRITEBACK
```

验收必须同时满足：

- 身份确认；
- 驳回原因；
- 整改动作；
- 电话/京麦分流；
- 结果确认；
- 结构化写回（至少本地 Mock 工单）。

## 6. 合规红线（自建）

### 6.1 必说

- 自报身份：京东商家资质审核；
- 通话录音告知；
- 驳回原因；
- 整改动作；
- 统一热线 `4006180092`；
- 礼貌结束。

### 6.2 禁说

- “保证通过 / 一定可以 / 百分百”；
- 承诺审核结果；
- 泄露其他商家、名单、手机号、地址；
- 诱导、辱骂、歧视、威胁；
- 冒充商家或品牌方；
- 未确认身份即披露驳回原因。

### 6.3 质检

- 每通会话保留 `ComplianceChecked`、`ComplianceBlocked` 事件；
- 必说话术缺失记为不合规；
- 禁语命中必须阻断；
- 人工抽检 + 规则扫描 + LLM Judge 三选二交叉。

## 7. 参考架构映射

| Brief 能力 | 本项目模块 |
|---|---|
| 实时语音/ASR/TTS/VAD | M2：sherpa ASR + Melo/Qwen TTS + 本地 VAD |
| WebRTC/实时传输 | M1：`/rtc`，当前 `ws_pcm`，LiveKit 可选 |
| 任务型 Agent/槽位/状态 | M4：Flow + POP Agent |
| Function Calling/工具 | M5：Tool Gateway + Mock API |
| 合规/质检 | M6：Compliance Guard + 事件 |
| 观测/评测/RSI | M7/M8/M9：EventBus、Eval、Registry/RSI |
| 一键启动/可复现 | `scripts/run_local_voice.ps1`、Dockerfile |

## 8. 自测 Runbook

```bash
# 1. Mock API
python -m uvicorn mock_api.server:app --port 8095

# 2. 本地真语音
.\scripts\run_local_voice.ps1

# 3. 契约/场景测试
python -m pytest tests/test_contract.py tests/test_pop_audit.py -q

# 4. 指标
python scripts/metric_check.py --base http://127.0.0.1:8081 --section all

# 5. 评测
python scripts/eval_pop.py --base http://127.0.0.1:8081
```

## 8.1 预测式主链路自检

```text
POST /predictive/v1/handle
text=好
stage=REASON
```

必须满足：

```text
schema_version=1.0
命中：hit=true，candidate 非空，prepared_candidates 非空
未命中：hit=false，filler_used=true，needs_llm=true，text 非空
任何非空输入都不能无输出
```

分类器当前为规则实现；后续可替换为模型/向量/embedding，但契约和 fallback 语义不变。

## 9. 限制

- 本文件不是官方评测口径；
- 用例规模只有 12 条，不能替代隐藏测试集；
- Mock API 不是真实资质/工单系统；
- 时延数据必须用真麦克风复测，不能用 Fake 结果代替；
- 官方资料到位后，本文的 Mock/评测/时延结论都需要重新对齐。
