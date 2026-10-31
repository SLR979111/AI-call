# 11 · Interface Contract v0.2 补丁（回应 cx 评审）

> 对象：[`review_cx_契约评审_v0.1.md`](review_cx_契约评审_v0.1.md)
> 基线：[`03-interface-contract-v0.1.md`](03-interface-contract-v0.1.md) / [`04-event-schema-v0.1.md`](04-event-schema-v0.1.md)
> 状态：**代码已落地的标 ✅；只冻结协议、实现待 W2 的标 🟡；明确不做的标 ⛔。**
> 原则：不推翻骨架；用最小消息集把 W1→W2 的换轨缺口补上。

---

## 0. 结论

cx 的 9 个 P0 中，**8 个接受，1 个是版本时差**：

| # | 结论 | 说明 |
|---|---|---|
| G1 上行音频帧 | ✅ 接受 | 定义 WS 二进制 `audio_frame`；WebRTC 期停用，音频走 RTP |
| G2 WebRTC 信令 | ✅ 接受（选 LiveKit） | **只做 LiveKit**；控制面仍走自有 WS；不做 aiortc SDP |
| G3 换轨后元数据 | ✅ 接受 | 新增 `tts_started / tts_sentence / tts_completed`；Fake 期 `tts_alignment=false` |
| G4 降级协议 | ✅ 接受 | 新增 `transport_degraded / switched / failed`；判据固定 |
| G5 挂断 | ✅ 接受 | WS `hangup` + `session_ended` |
| G6 WS 鉴权 | ✅ 接受 | `?token=`；失败 `4401`；`POST /sessions/{id}/token` 续期 |
| G7 枚举 | ✅ 接受 | 冻结 `turn_state / flow_state` + 六阶段映射 |
| G8 b3 | ✅ 接受 | `local_muted` 增加 b3/played/char；两个时延分开命名 |
| G9 交付物 | ⚠️ 已解决 | 代码/契约已在仓库；cx 看到的是旧快照，见 §9 |

**G9 不是设计问题，是交付时差。** 仓库现在有 `contracts/`、`app/`、`tests/`、`README.md`，最新 commit `023630a`。

---

## 1. G1 上行音频帧（WS 回退/骨架）

**协议**：C→S 二进制帧，仅 `transport=ws_pcm` 时使用；WebRTC 期停用。

```text
偏移 长度 字段
0    1   magic 0xA1
1    1   version 1
2    2   sample_rate u16BE = 16000
4    2   samples_per_frame u16BE = 320
6    4   frame_seq u32BE（从 0 递增）
10   4   client_mono_ms u32BE（距会话开始）
14   2   flags u16BE（bit0 静音 bit1 DTX bit2 语音段首帧）
16   640 PCM16LE（20ms）
```

冻结：20ms / 320 样本 / 640B / 16k 单声道 / PCM16LE。

**代码状态**：✅ 服务端 WS 已接受二进制帧并计数；🟡 帧头解析与 ASR 接入属 W2 的 `SherpaCpuAsr`。
**替代方案**：调试期允许 JSON `audio_frame` + base64；正式期禁用。

---

## 2. G2 媒体信令（只选 LiveKit）

**决策**：**LiveKit 只做媒体，控制面继续走自有 `/ws/session/{id}`。** 不引入 `livekit-client` 的房间事件语义，不写 aiortc SDP 两套。

```jsonc
// C→S：请求媒体凭证（也可走 HTTP POST /sessions/{id}/rtc）
{"type":"media_request"}

// S→C
{"type":"media_ready","transport":"livekit",
 "provider":"livekit","url":"wss://...","room":"aicall-s_...",
 "token":"<JWT>","identity":"s_...","ttl_s":1800}
```

- `ice_servers` 仅作兼容字段；LiveKit 客户端用 `url + token` 入房。
- **不允许** LiveKit DataChannel 发 `state/transcript/agent_text/cancel`；这些只在自有 WS。
- aiortc/SDP 形态写入 backlog（G2b），不在 W1/W2 实现。

**代码状态**：✅ `POST /sessions/{id}/rtc`、WS `media_request → media_ready`；未配置时 `MEDIA_NOT_CONFIGURED`。🟡 LiveKit 服务端与前端入房属 W2。

---

## 3. G3 播报元数据（换轨后不丢）

新增三条控制消息，**与媒体无关**，WS/RTC 通用：

```jsonc
// S→C：一轮播报开始（增量模式允许 sentences 为空）
{"type":"tts_started","turn_id":"t_0004","reply_id":"r_0004_1",
 "sample_rate":24000,"channels":1,"tts_alignment":false,"sentences":[]}

// S→C：逐句时间轴（Fake 期 start/end 为估算）
{"type":"tts_sentence","turn_id":"t_0004","reply_id":"r_0004_1","sentence_id":2,
 "text":"订单目前显示明天上午送达。","start_ms":1980,"end_ms":4100,
 "char_start":15,"char_end":29,"is_last":true}

// S→C：播报结束
{"type":"tts_completed","turn_id":"t_0004","reply_id":"r_0004_1",
 "total_ms":4100,"interrupted":false}
```

**硬要求**：

- `start_ms` 必须是真实音频偏移；做不到就 `tts_alignment=false`，前端按 `total_ms` 均分估算。
- 当前 Fake TTS 是浏览器 `speechSynthesis`，**没有真实音频时间轴**，所以 `tts_alignment=false`；`start_ms/end_ms` 是按字数估算，仅用于联调 UI。
- 接 CosyVoice/云 TTS 后，SinCircle 负责把真实偏移填进来；误差目标 <50ms。

**代码状态**：✅ 三条消息已发；`sample_rate/channels/is_last/char_start` 已带；🟡 真实偏移待接 TTS。

---

## 4. G4 降级协议

```jsonc
// C→S
{"type":"transport_degraded","from":"webrtc","to":"ws_pcm","reason":"ice_failed",
 "stats":{"rtt_ms":180,"loss_pct":12.4,"jitter_ms":95,"packets_received_stalled_ms":1600}}
// S→C
{"type":"transport_switched","to":"ws_pcm","audio":{"uplink":"pcm16/16000","downlink":"pcm16/24000"}}
{"type":"transport_failed","reason":"ws_unavailable","fallback":"text_only"}
```

**判据冻结**（前端照此实现）：

- `iceConnectionState` 为 `failed`；或 `disconnected` 持续 ≥3s；
- 或 `packetsReceived` 停滞 ≥1500ms；
- 或 ICE 首帧 2s 未达。
- **每会话最多降级一次，30s 内不回切。**

**代码状态**：✅ 消息与 `Degraded` 事件；🟡 实际切换/回切逻辑属 W2 前端+媒体层。

---

## 5. G5 挂断

```jsonc
// C→S（推荐 WS；HTTP /end 兜底）
{"type":"hangup","reason":"user_click","client_mono_ms":88123.4}
// S→C
{"type":"session_ended","reason":"user_click","duration_ms":88200,"turns":3}
```

**代码状态**：✅ WS `hangup` → `end_session` → `session_ended`；HTTP `/sessions/{id}/end` 仍在。

---

## 6. G6 WS 鉴权

- 浏览器连接：`ws://HOST:PORT/ws/session/{session_id}?token=<JWT>`。
- 服务端：`DEV_NO_AUTH=1` 时跳过；否则校验失败 `close(4401)`。
- 续期：`POST /sessions/{id}/token` 返回新 token；TTL 30min。
- 前端收到 `4401` 只提示“会话过期，请重新开始”，**不要无限重连**。
- 非浏览器客户端允许 `Authorization: Bearer` header。

**代码状态**：✅ query 校验（受 `DEV_NO_AUTH` 控制）、`/token` 续期；🟡 JWT 签名校验仍用开发 token，W2 接正式签发。

---

## 7. G7 状态枚举 + 六阶段映射

```text
turn_state: IDLE | CONNECTING | GREETING | LISTENING | USER_SPEAKING |
            ENDPOINTING | THINKING | SPEAKING | WRAP_UP | CLOSING |
            ENDED | DEGRADED | FAILED

flow_state: OPEN | IDENTITY_CONFIRM | NEED_CONFIRM | OFFER |
            RESULT_CONFIRM | WRITEBACK | CLOSE | TRANSFER | END | FALLBACK
```

| 赛题六阶段 | flow_state | 进度条 |
|---|---|---|
| 开场自报身份 | `OPEN` | ① 身份披露 |
| 身份确认 | `IDENTITY_CONFIRM` | ② 身份确认 |
| 需求确认 | `NEED_CONFIRM` | ③ 需求确认 |
| 方案/权益沟通 | `OFFER` | ④ 方案沟通 |
| 结果确认 | `RESULT_CONFIRM` | ⑤ 结果确认 |
| 结构化回写 | `WRITEBACK → CLOSE` | ⑥ 回写完成 |

`slots` 最小字段：`order_id`（string）、`new_time`（ISO8601）、`consent`（bool）。

**代码状态**：✅ `state` 消息已带 `turn_state/flow_state/slots`；🟡 枚举校验/状态机严格化待 pseudonym 的 FlowEngine 接入。

---

## 8. G8 打断 b3

```jsonc
{"type":"local_muted","turn_id":"t_0004","reply_id":"r_0004_1",
 "client_mono_ms":13001.0,"reason":"barge_in",
 "played_ms":3120,"sentence_id":2,"char_idx":7,
 "last_frame_played_mono_ms":13001.2,
 "stop_latency_ms":38.0}
```

两个指标分开：

| 指标 | 计算 | 目标 |
|---|---|---|
| 用户可感停止时延 | `b3 - b0`（前端算） | ≤100ms |
| 服务端静默时延 | `barge_in_confirmed - b0`（服务端算） | ≤300ms |

**代码状态**：✅ 字段透传、`BargeInConfirmed` 事件、`TurnLatency.barge_in_stop_ms`；🟡 前端真实 b3 计算待 WebRTC/播放器接入。

---

## 9. G9 交付物：已解决（请拉最新）

最新提交：tag **`v0.2-contract-patch`**（基线 `w2-llm-agent`）。仓库实际存在：

```text
contracts/schemas/{event,agent_chunk,tool_request,tool_response,manifest}.v1.json
contracts/fixtures/{session_normal,session_bargein,session_tool_fail,session_compliance_block}.jsonl
contracts/validate.py
app/  tests/  web/  scripts/  README.md
```

验证命令：

```bash
git log --oneline -3
python contracts/validate.py contracts/fixtures
python -m pytest tests/test_contract.py -q
python scripts/replay.py --session s_...
```

**可直接 curl 的地址清单**：

| 名称 | 值 |
|---|---|
| `APP_BASE` | `http://127.0.0.1:8081`（本机当前） |
| `WEB_BASE` | 同上，`/` |
| `WS_PATH` | `/ws/session/{session_id}?token={token}` |
| `RTC_TOKEN` | `POST /sessions/{id}/rtc` |
| `EVENTS` | `GET /sessions/{id}/events?since_seq=0` |
| `EVAL` | `POST /eval/run` |
| `REGISTRY` | `/registry/production` 等 |
| `LIVEKIT_URL` | 未配置，当前 `/rtc` 返回 503 |

cx 的 D1/D2/D3 建议：**同机 localhost + 文件回灌兜底 + LiveKit 只做媒体 + 双口径时延主显保守值**。

---

## 10. P1/P2 处置

| # | 处置 | 说明 |
|---|---|---|
| G10 sample_rate | ✅ | `tts_started` 带 `sample_rate/channels` |
| G11 BargeInCancelled | 🟡 | 采用“客户端只 duck、保留 ≤400ms 缓冲”，契约已写；代码待前端 |
| G12 client_event | ✅ | WS `client_event` 通用入口 + 白名单事件名 |
| G13 合规下发 | ✅ | WS `compliance` 消息（before/after/rule_ids） |
| G14 工具进度 | 🟡 | 契约定义 `tool_progress`；Gateway 回调 W2 |
| G15 回写结果 | ✅ | WS `turn_result`（与 `turn_end` 并存） |
| G16 t0 | 🟡 | `speech_ended.last_voiced_mono_ms` + `TurnLatency.t0_client/t0_vad`；前端待接 |
| G17 pong | ✅ | 回显 `client_mono_ms/server_rx/server_tx` |
| G18 错误→UI | 🟡 | 契约补映射表；代码待集中映射 |
| G19 自动播放 | ✅ | 进入通话页先点“开始通话”，再触发 GREETING |
| G20 AEC 能力 | 🟡 | `hello.capabilities` 结构化草案；服务端暂忽略 |
| G21 超时 | ✅ | `session_ready.timeouts` 下发 |
| G22–G26 | ⏳ | 进 backlog；G25 结论：不允许中途切 mode，失败走 G4 |

### G18 错误码 → UI 动作（新增列）

| code | UI 动作 |
|---|---|
| `ASR_EMPTY` / `ASR_TIMEOUT` | 提示“没听清，请再说一遍”，不阻断 |
| `LLM_TIMEOUT` / `LLM_RATE_LIMIT` | 文本气泡“稍慢”，继续 |
| `TOOL_TIMEOUT` / `TOOL_5XX` | 显示“查询失败”，允许转人工 |
| `TTS_TIMEOUT` | 只显示文本，不阻断对话 |
| `COMPLIANCE_BLOCK` | 红色拦截标记 + 安全话术 |
| `MEDIA_DISCONNECTED` | 触发 G4 降级 |
| `SESSION_TIMEOUT` | 结束语 + 挂断 |
| `BUDGET_EXCEEDED` | 切模板/转人工 |

---

## 11. 会签建议

**建议 cx 按“有条件同意”会签，但条件改为：**

> v0.2 已补齐 G1–G8 的协议与代码入口；G3 真实 TTS 时间轴、G11 缓冲恢复、G14 工具进度、G20 AEC 能力仍属 W2 实现项，不阻塞 W1 前端开工。G9 已解决，请以 tag `v0.2-contract-patch` 为准。

**W1 前端可以立即开工**：fixtures、WS 地址、`tts_*`、`compliance`、`turn_result`、`hangup`、`transport_*` 都已具备。
