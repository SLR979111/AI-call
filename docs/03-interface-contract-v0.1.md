# 03 · Interface Contract v0.1（契约包）

> Owner：天命（架构/集成）
> 会签：SinCircle（语音）、pseudonym（Agent）、湖中日月（评测）、cx（前端）
> 状态：**待会签**。会签后冻结 2 周；任何破坏性修改需提前 24h 通知并升版本。
> 上游：[01 矩阵](01-requirement-metric-module-matrix.md) · [02 架构](02-architecture-v0.1.md)

---

## 0. 为什么现在必须冻结

三周、五个人、四条并行开发线。**接口漂移是当前第一大系统风险**，比任何模型选型都致命。

本契约只冻结「形状」，不冻结「内容」：

| 冻结 | 不冻结 |
|---|---|
| ID、时间、音频格式、错误模型 | 用哪家 ASR/TTS/LLM |
| 事件 Envelope 与事件名 | Prompt 具体措辞 |
| Agent / Tool / Voice 接口签名 | Agent 内部状态机实现 |
| Flow Pack 与 rules.yaml 的结构 | 具体业务话术 |
| Version Manifest 字段 | 发布审批流程 |

**会签门槛**：四位 owner 能各自用 Fake 依赖跑通本契约第 12 节的契约测试，且不修改契约文件。

---

## 1. 角色映射（先消除代号冲突）

> ⚠️ `01/02` 文档里的 A–E 是「能力角色代号」；《三周分工》里的 A–E 是「成员代号」。**从本文档起，一律用姓名。**

| 成员 | 三周分工 | 架构模块 | 契约角色 | 明确不负责 |
|---|---|---|---|---|
| 天命 | A 后端/集成 | M3 编排器、M5 Tool Gateway、M7 Trace、M9 Registry、媒体服务 | 契约 Owner；唯一能启动/取消 Turn、写 Registry | ASR/TTS 内部、Agent Prompt、Judge |
| SinCircle | B 实时语音 | M2 VAD/ASR/TTS 适配器 | 提供 `AsrStream`/`TtsStream`/`VadEvent` | 业务状态、工具、取消调度 |
| pseudonym | C Agent | M4 Flow Engine + Context + LLM + RAG + 工具决策 | 提供 `Agent.handle()` 与 Flow Pack 内容 | 实际业务 API 调用、TTS、评测基准 |
| 湖中日月 | D 评测/RSI | M8 评测、归因、Candidate、回归 | 提供 `EvalRunner`、指标、合规测试集 | 直接改生产配置、改代码 |
| cx | E 前端/材料 | M10 控制台、WebRTC 客户端、客户端 VAD、可视化 | 提供控制面消息、播放埋点、调试面板 | 服务端媒体、业务逻辑 |

**合规责任（原计划缺失，必须补）**：

| 角色 | 合规职责 |
|---|---|
| pseudonym | 维护 `rules.yaml`、话术模板、改写策略；Agent 侧生成前约束 |
| 天命 | 在编排器关键路径插入 `ComplianceGuard` 调用点；保证 BLOCK 能取消 TTS |
| 湖中日月 | 合规测试集（必说/禁语/对抗）、质检报告、误杀率统计 |
| cx | 展示合规命中与改写前后对照，供答辩演示 |

---

## 2. 全局约定

### 2.1 标识符

| 字段 | 格式 | 生成者 | 语义 |
|---|---|---|---|
| `session_id` | `s_` + ULID（26 字符，可排序） | M3 | 一次完整通话 |
| `turn_id` | `t_` + 4 位十进制，会话内单调递增 | M3 | 一轮「用户说完 → Agent 说完」 |
| `trace_id` | `tr_` + 16 hex | M3 | 一次「用户输入 → 最终回复」调用链；打断产生新 turn 时新建 |
| `span_id` | `sp_` + 8 hex | 各模块 | 调用链节点 |
| `reply_id` | `r_` + turn 序号 + `_` + attempt | M3 | 一次 Agent 回复尝试；重试换 attempt |
| `call_id` | `c_` + UUID | Agent 或 Gateway | 一次工具调用 |
| `release_id` | `rel_` + `YYYYMMDD` + `_` + 序号 | M9 | 一次版本集合 |

规则：

- ID 不透明，禁止从中解析业务含义（`turn_id` 仅用于排序）。
- `turn_id` 只由 M3 分配；Agent/Gateway 不得自造。
- 一次打断 = 旧 turn `TurnAborted` + 新 turn 开始；不复用 `turn_id`。

### 2.2 时间

```json
{
  "ts": "2026-10-31T17:00:00.123+08:00",
  "ts_mono_ms": 123456.789,
  "clock_id": "app-7f3a"
}
```

- `ts`：墙钟，仅用于展示与跨机对齐。
- `ts_mono_ms`：进程内单调时钟（`time.perf_counter()*1000`），**所有延迟计算只用它**。
- `clock_id`：进程启动 ID；跨进程比较必须做时钟同步，不能直接相减。
- 客户端事件带 `client_ts_ms` 与 `client_mono_ms`；服务端用 3 次 ping/pong 估计偏移（取中位数），偏差 >50ms 标记 `clock_suspect`。

### 2.3 音频

| 方向 | 格式 | 帧长 | 备注 |
|---|---|---|---|
| 上行 | PCM16LE / 16 kHz / 单声道 | 20 ms（320 样本，640 B） | 浏览器 `AudioWorklet` 采集；WebRTC 时 Opus 解码后转此格式 |
| 下行 | PCM16LE / 24 kHz / 单声道 | 20–100 ms/块 | TTS 原生 24k；客户端 `AudioWorklet` 播放 |
| 传输 | 骨架期 WS base64；正式 WebRTC 音频轨（Opus） | — | 控制面 JSON 不变 |

规则：

- 每帧带 `session_id`、`seq`、`turn_id`（下行必带）。
- 下行块必须带 `reply_id`、`sentence_id`、`is_last`；客户端按 `turn_id` 丢弃过期块，防止「幽灵音频」。
- 音频不写事件日志本体，只存 `audio_uri` + 偏移；转写文本按 PII 规则脱敏。

### 2.4 版本集合（Version Set）

每个 session 创建时固定：

```json
{
  "release_id": "rel_20261101_01",
  "flow": "service_outbound@1.0.0",
  "prompt": "agent_system@12",
  "knowledge": "faq@2026-10-31",
  "model": "deepseek-chat@2026-10",
  "config": "runtime@8"
}
```

缺失任一项拒绝建会话。所有事件带 `version_set` 或 `release_id`。

### 2.5 统一错误模型

```json
{
  "code": "LLM_TIMEOUT",
  "message": "first token > 3000ms",
  "retryable": true,
  "severity": "warn",
  "user_message": "抱歉，我这边稍慢，您还在吗？",
  "fallback": "template"
}
```

| code | 触发 | 可重试 | 默认降级 |
|---|---|---|---|
| `ASR_TIMEOUT` / `ASR_EMPTY` | 3s 无 final / 空结果 | 是（重问） | 请用户再说一遍 |
| `LLM_TIMEOUT` | 首 token >3s | 是 | 备用模型 → 模板 |
| `LLM_RATE_LIMIT` | 429 | 是（退避） | 模板 + 转人工 |
| `LLM_INVALID_JSON` | 结构化输出解析失败 | 是（1 次） | 降级纯文本 |
| `TOOL_TIMEOUT` / `TOOL_5XX` | 2s / 5xx | 是（1 次） | 记录待办 + 转人工 |
| `TOOL_INVALID_ARGS` | schema 校验失败 | 否 | 追问补充参数 |
| `TOOL_NOT_FOUND` | 资源不存在 | 否 | 追问/转人工 |
| `TOOL_PERMISSION_DENIED` | 无权限 | 否 | 转人工/记录 |
| `TOOL_RATE_LIMIT` | 429 | 是（退避） | 排队/转人工 |
| `TTS_TIMEOUT` | 首包 >1.5s | 是 | 备用音色 → 预置句 |
| `COMPLIANCE_BLOCK` | 规则命中 | 否 | 安全话术 / 转人工 |
| `MEDIA_DISCONNECTED` | ICE 断开 >3s | 是 | 重连 → 挂断重拨 |
| `SESSION_TIMEOUT` | 总时长/静音超时 | 否 | 结束语 + 挂断 |

`severity`：`info | warn | error | fatal`。`fatal` 必须触发 `SessionFailed` 并写根因。

### 2.6 取消语义

```python
class CancelToken:
    def cancelled(self) -> bool: ...
    def reason(self) -> str | None: ...   # BARGE_IN | TIMEOUT | SESSION_END | COMPLIANCE_BLOCK | USER_HANGUP | INTERNAL
    async def wait(self) -> None: ...
```

- 协作式取消：每个异步循环在 **chunk 边界**检查一次，不硬杀协程。
- `cancel()` 幂等；重复调用只记一次事件。
- 被取消方必须在 **300ms** 内回 `CancelAcked`；超时记 `CancelSlow`。
- 下行音频按 `turn_id` 过滤；取消后旧块直接丢弃，不做「快速淡出」以外的补偿。

---

## 3. 运行时契约

### 3.1 正常一轮的事件顺序（必须严格）

```text
SpeechStarted(client) → VadEndpoint → ASRFinal
→ TurnCommitted → AgentStarted → AgentFirstToken → AgentSentence(1..n)
→ ComplianceChecked(each) → TTSStarted → TTSFirstChunk → PlaybackFirstFrame
→ TTSCompleted → PlaybackEnded → TurnCommitted(result)
```

> `TurnCommitted` 有两个语义：用户输入确认（ASR final 后）和结果确认（回写后）。用 `phase: "input" | "result"` 区分。

### 3.2 打断的事件顺序

```text
ClientSpeechStarted → ClientLocalMuted → BargeInDetected
→ CancelRequested → Agent/TTS CancelAcked
→ TurnAborted(old) → (可选) BargeInCancelled(误打断) → TurnStarted(new)
```

时序约束：`ClientLocalMuted` 必须发生在客户端检测到语音后 **≤50ms**；`CancelRequested` 在服务端收到 `ClientSpeechStarted` 后 **≤100ms**。

---

## 4. 接口 A：客户端 ↔ 编排器

### 4.1 HTTP 控制面

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/sessions` | `{mode:"voice"\|"text", scenario_id?, release_id?}` | `{session_id, token, ws_url, version_set}` |
| GET | `/sessions/{id}/state` | — | `{turn_state, flow_state, slots, turn_id}` |
| POST | `/sessions/{id}/cancel` | `{turn_id, reason}` | `{ok}`（仅调试/管理） |
| GET | `/sessions/{id}/events` | `?since_seq=` | NDJSON 事件流 |
| POST | `/sessions/{id}/text` | `{text}` | SSE/NDJSON：AgentChunk + 最终结果（D 的文本评测入口） |
| GET | `/healthz` | — | `{status, deps}` |
| GET | `/metrics` | — | Prometheus 文本 |

Token：JWT，`aud=session_id`，TTL 30min，含 `release_id`；WebSocket 握手校验。

### 4.2 WebSocket / DataChannel 消息

客户端 → 服务端：

```json
{"type":"hello","session_id":"s_...","client_version":"web@0.1","capabilities":["local_vad","aec"]}
{"type":"speech_started","turn_id":null,"client_ts_ms":1710000000123,"client_mono_ms":12345.6,"energy_db":-32.1,"source":"local_vad"}
{"type":"speech_ended","client_ts_ms":1710000001450,"reason":"local_vad"}
{"type":"playback_first_frame","turn_id":"t_0003","reply_id":"r_0003_1","client_mono_ms":13000.2,"audio_ctx_time":12.84}
{"type":"playback_ended","turn_id":"t_0003","reply_id":"r_0003_1"}
{"type":"local_muted","turn_id":"t_0003","client_mono_ms":13001.0,"reason":"barge_in"}
{"type":"ping","t":1710000002000}
```

服务端 → 客户端：

```json
{"type":"session_ready","session_id":"s_...","version_set":{...},"ice_servers":[...]}
{"type":"state","turn_state":"LISTENING","flow_state":"NEED_CONFIRM","slots":{...},"turn_id":"t_0004"}
{"type":"transcript","turn_id":"t_0004","text":"我想改到明天下午","final":true}
{"type":"agent_text","turn_id":"t_0004","reply_id":"r_0004_1","sentence_id":1,"text":"可以的，我先帮您看一下订单。","final":false}
{"type":"audio_chunk","turn_id":"t_0004","reply_id":"r_0004_1","sentence_id":1,"seq":1,"pcm_b64":"...","is_last":false}
{"type":"cancel","turn_id":"t_0004","reason":"BARGE_IN"}
{"type":"pong","t":1710000002000,"server_ts":"...","server_mono_ms":...}
{"type":"error","error":{...}}
```

规则：

- `audio_chunk` 仅骨架期使用；WebRTC 后音频走轨，控制面不再传 PCM。
- `playback_first_frame` 是官方时延的关键埋点，**必须实现**，且不得省略 `audio_ctx_time`。
- 客户端本地 VAD 命中后先 `local_muted`，再发 `speech_started`；服务端允许先收后者，幂等处理。

---

## 5. 接口 B：语音适配器

```python
class AsrAdapter(Protocol):
    async def start(self, session_id: str, *, sample_rate: int = 16000,
                    hotwords: list[str] | None = None) -> "AsrStream": ...

class AsrStream(Protocol):
    async def push(self, frame: "AudioFrame") -> None: ...
    async def finish(self) -> None: ...
    def events(self) -> AsyncIterator["AsrEvent"]: ...
    async def close(self) -> None: ...

class AsrEvent:
    kind: Literal["partial", "final", "error"]
    text: str
    confidence: float
    audio_start_ms: int
    audio_end_ms: int
    error: AppError | None

class TtsAdapter(Protocol):
    async def synthesize(self, text: str, *, voice: str = "default",
                         sample_rate: int = 24000,
                         cancel: CancelToken) -> AsyncIterator["AudioChunk"]: ...

class AudioChunk:
    pcm: bytes            # PCM16LE
    seq: int
    sentence_id: int
    is_last: bool
    ts_mono_ms: float

class Vad(Protocol):
    def push(self, frame: "AudioFrame") -> list["VadEvent"]: ...
```

`VadEvent`：`SpeechStarted | SpeechEnded | Endpoint`，带 `ts_mono_ms`、`energy_db`、`confidence`。

约束：

- 适配器**不感知** session/turn/业务；只处理音频与文本。
- 适配器内部不重试；错误以 `AppError` 抛出，由编排器决定降级。
- `AsrStream.finish()` 后必须在 300ms 内产出 final 或 error。
- TTS 必须在 `cancel.cancelled()` 后停止产出，并关闭底层连接。

**边界提醒（SinCircle）**：你只负责「检测到用户开始说话」并发出 `SpeechStarted`；「取消 TTS、重置轮次、重新调度」是 M3 的职责。不要在语音模块里直接杀 Agent。

---

## 6. 接口 C：Agent

```python
class Agent(Protocol):
    async def start_session(self, session_id: str, version_set: dict[str, str],
                            flow_pack: dict) -> None: ...
    async def handle(self, req: "AgentRequest", cancel: CancelToken,
                     tools: "ToolClient") -> AsyncIterator["AgentChunk"]: ...

@dataclass
class AgentRequest:
    session_id: str
    turn_id: str
    trace_id: str
    reply_id: str               # 由 M3 分配，Agent 原样透传
    user_text: str
    asr_conf: float
    dialog_state: dict          # Flow Engine 快照，只读
    recent_turns: list[dict]    # 最近 6 轮摘要
    language: str = "zh-CN"
```

`AgentChunk` 联合类型（JSON 示例）：

```json
{"kind":"sentence","reply_id":"r_0004_1","sentence_id":1,"text":"可以的，我先帮您看一下订单。","final":false}
{"kind":"sentence","reply_id":"r_0004_1","sentence_id":2,"text":"订单目前显示明天上午送达。","final":true}
{"kind":"transfer","reply_id":"r_0004_1","reason":"user_request","queue":"human","summary":"用户要求人工"}
{"kind":"end","reply_id":"r_0004_1","outcome":"rescheduled","slots":{"order_id":"A12345678"},"result":{"new_time":"2026-11-01T15:00"}}
{"kind":"error","reply_id":"r_0004_1","error":{"code":"LLM_TIMEOUT","message":"...","retryable":true}}
```

规则：

- Agent **只产出文本/决策**，绝不产出音频、绝不直接调业务 HTTP。
- 首句必须 ≤20 字（可配置），保证 TTS 首包尽早；`final=true` 表示本轮语义结束。
- 工具调用通过注入的 `ToolClient`（即 Gateway 接口）完成；Agent 决定「调什么、参数是什么」，Gateway 决定「怎么调、能否重试」。
- Agent 抛出的任何异常必须转成 `error` chunk，不能向上冒泡导致 turn 悬挂。
- `end` 必须带结构化 `result`，供回写与评测。

**边界提醒（pseudonym）**：你决定工具与参数，但不实现重试/幂等/鉴权；不要绕过 `ToolClient` 直连 Mock API，否则评测无法归因。

---

## 7. 接口 D：Tool Gateway

### 7.1 注册表

`tools/order.query.yaml`：

```yaml
name: order.query
version: 1.2.0
description: 按订单号查询订单与配送状态
side_effect: read
idempotent: true
timeout_ms: 2000
input_schema:
  type: object
  required: [order_id]
  properties:
    order_id: {type: string, pattern: "^[A-Z0-9]{8,20}$"}
output_schema:
  type: object
  required: [order_id, status]
  properties:
    order_id: {type: string}
    status: {type: string, enum: [created, shipped, delivering, delivered, exception]}
    promised_at: {type: string, format: date-time}
auth: mock_token
```

### 7.2 调用

```python
class ToolClient(Protocol):
    async def invoke(self, tool: str, args: dict, *,
                     idempotency_key: str | None = None,
                     deadline_ms: int = 2000) -> "ToolResult": ...
```

等价 HTTP（内部）：

```http
POST /internal/tools/order.query/invoke
X-Session-Id: s_...
X-Turn-Id: t_0004
X-Trace-Id: tr_...

{"args":{"order_id":"A12345678"},"idempotency_key":"...","deadline_ms":2000}
```

成功：

```json
{"ok":true,"data":{"order_id":"A12345678","status":"delivering"},"meta":{"attempt":1,"cached":false,"latency_ms":123,"tool_version":"order.query@1.2.0"}}
```

失败：

```json
{"ok":false,"error":{"code":"TOOL_TIMEOUT","message":"deadline exceeded","retryable":true},"meta":{"attempt":2,"latency_ms":2001}}
```

### 7.3 幂等与重试

- 幂等键：调用方优先；缺省为 `sha256(session_id|turn_id|tool|canonical_json(args)|business_key)`。
- 存储：SQLite `idempotency(key TEXT PRIMARY KEY, response_json, created_at, expires_at)`；TTL 24h。
- 重放：返回原响应，`meta.cached=true`；副作用不得重复。
- 重试：仅 `TOOL_TIMEOUT`/`TOOL_5XX`/`TOOL_RATE_LIMIT`，最多 1 次，退避 200ms，总时长不超 `deadline_ms`。
- 不重试：`TOOL_INVALID_ARGS`、`TOOL_NOT_FOUND`、`TOOL_PERMISSION_DENIED`。

### 7.4 Mock 业务系统

天命负责 `mock-business` 服务（Docker Compose 内）：

- `GET /mock/orders/{id}`、`POST /mock/orders/{id}/reschedule`、`GET /mock/leads/{id}`、`POST /mock/tickets`。
- 固定随机种子；支持 `?fault=timeout|500|slow|invalid` 注入。
- 所有写操作返回 `ticket_id` 与幂等键，便于验证「不重复建单」。

---

## 8. 接口 E：Version Registry

### 8.1 Manifest

```json
{
  "release_id": "rel_20261101_01",
  "created_at": "2026-11-01T10:00:00+08:00",
  "status": "candidate",
  "parent_release_id": "rel_20261031_02",
  "artifacts": {
    "flow": {"name":"service_outbound","version":"1.0.0","sha256":"..."},
    "prompt": {"name":"agent_system","version":"12","sha256":"..."},
    "knowledge": {"name":"faq","version":"2026-10-31","sha256":"..."},
    "model": {"name":"deepseek-chat","version":"2026-10","sha256":"..."},
    "config": {"name":"runtime","version":"8","sha256":"..."}
  },
  "metrics": {"task_success":0.82,"p50_ms":1180,"p95_ms":1900,"compliance_pass":1.0},
  "notes": "D 生成：修复改约意图漏识别"
}
```

### 8.2 状态与操作

```text
candidate → (gate) → production
production → previous（被新版本替换）
任何版本 → archived
```

| 操作 | 谁 | 语义 |
|---|---|---|
| `POST /registry/candidates` | D | 写候选 manifest + 工件，不生效 |
| `POST /registry/promote` | 天命 | 校验 gate 结果，原子切换 production/previous |
| `POST /registry/rollback` | 天命 | 指针切回 previous，≤60s |
| `GET /registry/production` | 全部 | 当前生产 release |
| `GET /registry/releases` | 全部 | 列表 + 指标 |

存储：`registry/releases/{release_id}/` + `registry/state.json`（指针）+ `registry/index.sqlite`。原子写：临时文件 + rename。

**禁止**：D 直接改 `production`；C 直接改生产 prompt 文件；任何模块在运行时改 `state.json`。

---

## 9. 接口 F：评测 Headless Runner

D 的 Week 1 阻塞点。必须支持「无音频、纯文本」跑完整业务：

```http
POST /eval/sessions
{"scenario_id":"delivery_reschedule_001","release_id":"rel_20261101_01","mode":"text"}
→ {"eval_session_id":"es_...","ws_url":"/eval/sessions/es_.../stream"}

GET /eval/sessions/{id}
→ {
  "eval_session_id":"es_...",
  "scenario_id":"...",
  "release_id":"...",
  "metrics":{"task_success":true,"tool_args_correct":true,"turns":4},
  "turns":[{"user":"...","agent":"...","tools":[...]}],
  "failure_labels":[],
  "artifacts":{"events":"data/evals/es_.../events.jsonl","result":"...","audio":null}
}
```

约束：

- 复用同一套 Orchestrator/Agent/Gateway/Compliance，只替换语音适配器为文本输入。
- 事件与线上同 schema，`mode:"text"` 写入事件。
- 支持 `scenario_id` 批量：`python -m eval.run --suite dev --release ... --out data/evals/`。
- D 不依赖浏览器、不依赖 GPU、不依赖麦克风。

---

## 10. 合规契约

### 10.1 `rules.yaml`

```yaml
version: 3
must_say:
  - id: ai_identity
    stage: OPEN
    patterns: ["智能客服", "AI", "数字员工"]
  - id: recording_notice
    stage: OPEN
    patterns: ["录音", "通话记录"]
forbidden:
  - id: false_promise
    patterns: ["保证", "一定", "绝对", "百分百"]
    action: REWRITE
    rewrite: "以实际结果为准"
  - id: privacy_leak
    patterns: ["身份证号", "银行卡号", "完整手机号"]
    action: BLOCK
    fallback: "抱歉，这个问题我无法提供。"
transfer:
  - id: user_request_human
    patterns: ["转人工", "找人工", "投诉"]
```

### 10.2 守卫接口

```python
class ComplianceGuard(Protocol):
    async def check_input(self, text: str, ctx: dict) -> "GuardResult": ...
    async def check_sentence(self, text: str, ctx: dict) -> "GuardResult": ...
    async def check_tool(self, tool: str, args: dict, ctx: dict) -> "GuardResult": ...

class GuardResult:
    decision: Literal["ALLOW", "REWRITE", "BLOCK", "TRANSFER"]
    text: str
    rule_ids: list[str]
    severity: Literal["info", "warn", "error", "fatal"]
    latency_ms: float
```

### 10.3 三个调用点（缺一不可）

1. **生成前**：`rules.yaml` 的 `forbidden` 注入 system prompt + Flow Pack 状态约束。
2. **句级门禁**：每个 `AgentChunk.sentence` 在送 TTS 前检查首句；`BLOCK` 必须取消当前 reply 的 TTS。
3. **工具守卫**：`check_tool` 在 Gateway 前执行；`BLOCK` 不允许发出请求。

**性能约束**：首句检查 P95 ≤ 50ms；正则/Aho-Corasick 快路径，LLM 复核异步（不阻塞首句）。

---

## 11. Flow Pack 契约

```yaml
id: service_outbound
version: 1.0.0
entry: OPEN
states:
  OPEN:
    say: "您好，我是{company}智能客服{name}，本次通话会录音。请问是{name}本人吗？"
    must_say: [ai_identity, recording_notice]
    next: IDENTITY_CONFIRM
  IDENTITY_CONFIRM:
    transitions:
      - when: {intent: confirm}
        to: NEED_CONFIRM
      - when: {intent: deny}
        to: END
      - when: {intent: unclear}
        to: REPEAT
  NEED_CONFIRM:
    ask: "想跟您确认一下，您今天方便收货吗？"
    slots: [order_id]
    next: OFFER
  OFFER:
    say: "可以帮您改到明天下午 3 点，您看方便吗？"
    next: RESULT_CONFIRM
  RESULT_CONFIRM:
    transitions:
      - when: {intent: confirm}
        to: WRITEBACK
      - when: {intent: deny}
        to: OFFER_ALT
  WRITEBACK:
    tool: order.reschedule
    next: CLOSE
  CLOSE:
    say: "已为您改约成功，稍后会收到短信。祝您生活愉快。"
    next: END
slots:
  order_id: {required: true, ask: "方便提供订单号吗？", pattern: "^[A-Z0-9]{8,20}$"}
tools:
  order.reschedule: {when: "state == WRITEBACK"}
```

规则：

- 状态/转移由 pseudonym 维护；加载器由天命实现；测试用例由湖中日月生成。
- 未知 intent 默认走 `fallback` 状态，不允许「LLM 自由跳转」。
- 每个状态声明 `must_say`；离开状态前未满足则强制补话术。
- 版本随 release 固定；运行时不可热改。

---

## 12. 契约测试与夹具

```text
contracts/
  schemas/
    event.v1.json
    agent_chunk.v1.json
    tool_request.v1.json
    tool_response.v1.json
    flow_pack.v1.json
    manifest.v1.json
  fixtures/
    session_normal.jsonl
    session_bargein.jsonl
    session_tool_fail.jsonl
    session_compliance_block.jsonl
  validate.py
```

`make contract-test` 必须：

1. 用 Fake 适配器跑 4 条夹具会话。
2. 校验所有事件符合 `event.v1.json`（required + 类型；允许扩展字段）。
3. 校验 AgentChunk / Tool / Manifest。
4. 断言事件顺序（第 3 节）。
5. 输出 `artifacts/contract_report.json`。

**前端 cx 可先用 fixtures 渲染**，不必等后端；D 可先用 fixtures 写指标脚本。

---

## 13. 会签检查清单

| 检查 | Owner | 截止 |
|---|---|---|
| `event.v1.json` 字段无歧义，示例可解析 | 天命 | D+1 |
| `AgentChunk` 能表达追问/工具/转人工/结束 | pseudonym | D+1 |
| `AsrStream/TtsStream` 可被 Fake 实现，错误可注入 | SinCircle | D+1 |
| 前端能消费 fixtures 渲染字幕与状态 | cx | D+2 |
| 文本 headless 能跑通 1 条场景 | 湖中日月 + 天命 | D+2 |
| `ToolClient` 幂等/超时用例通过 | 天命 + pseudonym | D+2 |
| `rules.yaml` 必说/禁语各 3 条可触发 | pseudonym + 湖中日月 | D+2 |

---

## 14. 变更流程

- 契约版本 `v0.1` → `v0.2`：仅允许**加字段/加可选值**；破坏性修改必须升 `v1.0` 并提前 24h 通知。
- 每次变更更新 `contracts/CHANGELOG.md`，写「谁、为什么、影响谁」。
- 任何模块不得私自定义「对方能接受但契约没有」的字段；联调发现缺口，先改契约再改代码。
- 事件 schema 与接口 schema 分开版本；事件只增不改。

---

## 15. 未决项（不阻塞会签）

1. 媒体传输：WS 骨架 → WebRTC 的时间点与方案（LiveKit vs aiortc），见 05 计划。
2. TTS 采样率：24k 统一，还是按供应商自适应。
3. LLM 结构化输出：JSON mode 的失败率与降级策略。
4. 官方时延口径：`t0` 是用户最后一个有声帧还是 VAD 端点。
5. 场景 Flow Pack：等赛题资料后替换，但 schema 不变。
