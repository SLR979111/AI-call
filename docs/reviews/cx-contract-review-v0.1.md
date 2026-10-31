# cx · 对 Interface Contract v0.1 / Event Schema v0.1 的评审与会签意见

**评审人** cx（前端 WebRTC） ｜ **对象** `03-interface-contract-v0.1.md`、`04-event-schema-v0.1.md`、`05` §3 媒体路径
**结论**：**有条件同意会签**。骨架（ID/时间/音频/错误/取消/事件 Envelope/两个状态机）没有问题，可以作为冻结基线；但必须在 v0.2 补齐下面 **9 个 P0**，否则前端在 W1 D3（真实语音接入）和 W2（WebRTC 切换）会直接卡死，且卡的不是"实现难"，是"契约里没有这个通道"。

> 会签条件可以一句话表述：**把 §4.2 的消息表从"骨架够用"补到"WebRTC 够用"**。现在这张表描述的是 WS+base64 骨架，而 05 §3 决定 W2 换 WebRTC —— 换轨时 `audio_chunk` 消失，附着在它上面的所有元数据（`reply_id / sentence_id / seq / is_last`）也随之消失，契约里没有任何替代通道。

---

## 0. 总表（先看这张）

| # | 级别 | 缺口 | 影响 | 建议归属 |
|---|---|---|---|---|
| G1 | **P0** | 上行音频帧消息缺失（C→S 消息表里没有音频） | WS 骨架期与降级层都传不了音频 | 天命 + cx |
| G2 | **P0** | WebRTC 信令消息缺失（offer/answer/candidate/ICE restart） | W2 切 WebRTC 无契约可依 | 天命 |
| G3 | **P0** | 换轨后 `reply_id/sentence_id/时间偏移` 无下发通道 | 游标、打断定位、幽灵音过滤全部失效 | 天命 + SinCircle + cx |
| G4 | **P0** | 降级/恢复协议未定义（`DEGRADED` 只有事件，没有消息） | 现场弱网时前端不知道该切谁 | 天命 |
| G5 | **P0** | 客户端挂断/结束接口缺失 | UI 的"挂断"按钮无接口可调 | 天命 |
| G6 | **P0** | WS 鉴权方式不可实现（浏览器不能给 WebSocket 设 header） | 握手必失败或被迫裸奔 | 天命 + cx |
| G7 | **P0** | `state` 的枚举值未冻结（`turn_state` / `flow_state`） | 进度条、状态灯写不出来 | 天命 + pseudonym + cx |
| G8 | **P0** | 打断的 `b3`（最后一个 Agent 音频帧播出的时刻）无上报点 | 官方"打断停止时延"算不出来 | cx + 天命 |
| G9 | **P0** | `contracts/` 目录、fixtures、code skeleton 实际不在交付物里 | 05 D1 要求前端"用 fixtures 渲染"，没东西可渲染 | 天命 |
| G10 | P1 | 下行 `audio_chunk` 无 `sample_rate`（03 §2.3 写死 24k，09 说 CPU VITS 采样率不定） | 播放变速/变调 | SinCircle |
| G11 | P1 | `BargeInCancelled(resume_from_sentence)` 语义不可实现 | 误打断后无法"从下一句继续" | 天命 + cx |
| G12 | P1 | 客户端事件上传通道不完整（只有 4 条 WS 消息，事件字典里客户端事件远多于 4 条） | `TurnLatency` 缺段、网质量/欠载无法统计 | cx + 天命 |
| G13 | P1 | 合规命中/改写无误下发消息 | cx 无法"展示合规命中与改写前后对照"（03 §1 给 cx 派了这个活） | 天命 + pseudonym |
| G14 | P1 | 工具调用进度无误下发消息 | UI 无法显示"正在查询订单" | 天命 |
| G15 | P1 | 结构化回写结果无误下发消息 | 结果卡（六阶段闭环的唯一物证）无法渲染 | 天命 + pseudonym |
| G16 | P1 | `t0`（用户最后一个有声帧）未定义、无上报点 | 保守口径 `t9-t0` 算不出来 | cx + 天命 + 湖中日月 |
| G17 | P1 | `audio_ctx_time` 的用途未定义；ping/pong 未回显客户端单调时间 | 客户端无法自算偏移，`clock_suspect` 判定无依据 | cx |
| G18 | P1 | 错误码到 UI 动作的映射未定义（哪些重连、哪些阻断、哪些只提示） | 兜底逻辑只能猜 | 天命 + cx |
| G19 | P1 | 自动播放策略与开场白预合成冲突（无用户手势不能出声） | `GREETING` 在 Chrome 上静默失败 | cx |
| G20 | P1 | AEC/双讲状态未纳入协商；外放场景无半双工兜底 | R02"无自激"只能靠运气 | cx + 天命 |
| G21 | P1 | 静音超时阈值未下发（03 §6 有 10s/30s/45s 多套数字，前端不知道用哪个） | 无法做倒计时提示 | 天命 |
| G22 | P2 | `hello.client_version` 无对应 `server_version` / 版本兼容判定 | 协议漂移无法早失败 | 天命 |
| G23 | P2 | `latency_ms` 要求配 `payload.latency_from`，但该字段无语义 | 事件校验会误报 | 天命 |
| G24 | P2 | 心跳超时策略未定义（谁判、多久判死） | 断线后各自猜 | 天命 |
| G25 | P2 | `text` 模式能否中途切换未说明 | 语音失败时前端不知道能否原地降级 | 天命 |
| G26 | P2 | 服务地址/端口/WS 路径/环境变量清单未冻结 | 前端 `.env` 只能猜 | 天命 + cx |

---

## 1. P0 逐条展开（会签前必须定）

### G1 上行音频帧：消息表里根本没有音频

03 §4.2「客户端 → 服务端」只有 6 条：`hello / speech_started / speech_ended / playback_first_frame / playback_ended / local_muted`。**没有任何一条携带上行音频**。而 05 §3 决定 W1 用 WS 传 PCM，D3 Gate 要求"真实说一句话 2 秒内听到回答"。

同时 03 §2.3 写"传输：骨架期 WS base64"，而 05 §3 写"WS 二进制帧"。两处不一致。

**建议**（三选一，我推荐 A）：

```jsonc
// A. 二进制帧 + 定长帧头（推荐：无 base64 膨胀、无 JSON 解析开销）
// C→S, /ws/session/{id}
// 偏移 长度 字段
// 0    1   magic 0xA1
// 1    1   version = 1
// 2    2   sample_rate  u16 BE = 16000
// 4    2   samples_per_frame u16 BE = 320
// 6    4   frame_seq    u32 BE（从 0 递增，丢帧靠它发现）
// 10   4   client_mono_ms u32 BE（距会话开始的毫秒，单调）
// 14   2   flags        u16 BE（bit0=静音帧 bit1=DTX bit2=语音段首帧）
// 16   640 PCM16LE × 320
// 说明：Turn 归属不在帧里，由服务端 VAD 归属到当前 turn_id。

// B. JSON + base64（仅调试期可用，正式期禁用）
{"type":"audio_frame","turn_id":null,"seq":12,"sample_rate":16000,
 "pcm_b64":"...","client_mono_ms":12345.6}

// C. 二进制帧 + 单独 JSON 头（每 40 帧发一次头再发音频）——不推荐，状态机复杂
```

**必须同时冻结**：帧长 20ms / 320 样本 / 640 B、上行 16k 单声道 PCM16LE。这三个数字如果变，ASR 端点、VAD 参数、打断阈值全部要重调。

**WebRTC 期**：此消息停用，音频走 RTP。契约里要写一句"`audio_frame` 仅 `transport=ws` 时使用"，避免两端实现打架。

---

### G2 WebRTC 信令：W2 换轨时没有可依的契约

05 §3 决定了 W2 用 LiveKit（优先）或 aiortc+coturn，但 03 只给了 `session_ready.ice_servers`。**信令消息一条都没定义**。

**建议**：区分两种媒体服务形态，二选一写进契约：

```jsonc
// 形态 1：LiveKit（05 推荐）—— 前端只需 token/room，不需要自己交换 SDP
// S→C
{"type":"media_ready","transport":"livekit",
 "livekit_url":"ws://localhost:7880","room":"room_s_01J...",
 "token":"<JWT aud=room>","identity":"cx-7f3a","ttl_s":1800}

// 形态 2：aiortc 1:1 —— 需要完整 O/A，且必须支持 trickle ICE 与 ICE restart
// C→S
{"type":"rtc_offer","sdp":"v=0...","sdp_type":"offer"}
{"type":"rtc_candidate","candidate":"candidate:...","sdp_mid":"0","sdp_mline_index":0}
{"type":"rtc_ice_restart","reason":"ice_disconnected_3s"}
// S→C
{"type":"rtc_answer","sdp":"v=0..."}
{"type":"rtc_candidate","candidate":"..."}
{"type":"rtc_failed","reason":"ice_failed","retry_after_ms":0}
```

**关键决策请求**（今天就要拍）：**LiveKit 只做媒体，控制面仍然走我们自己的 `/ws/session/{id}`。**
理由：① 前端不引 `livekit-client` 就会退化成"自己写 Room 协议"，引了则两套事件语义并存；② 03 全篇的控制帧语义（`state / transcript / agent_text / cancel`）都在自有 WS 上；③ 降级到 WS+base64 时，控制面不能跟着换。如果 LiveKit 的数据通道也要用，请明确"哪些消息允许走 DataChannel"，否则 W2 会出现同一事件两条路径、`seq` 冲突。

---

### G3 换轨后元数据无处安放（最严重的语义缺口）

现状：`audio_chunk` 一条消息同时承载"音频 + 归属（`turn_id/reply_id/sentence_id/seq/is_last`）"。03 §4.2 规则写着"WebRTC 后音频走轨，控制面不再传 PCM"——**但没说元数据去哪**。

一旦音频上轨，前端会丢掉：

| 丢失的东西 | 导致 |
|---|---|
| `reply_id` / `sentence_id` | 无法算"已播到第几句第几个字"→ 打断定位、RSI 分析"被截断话术"、必说项覆盖统计全部失效 |
| 每句的音频时间偏移 | 无法做游标；`BargeInConfirmed` 只能说"停了"，不能回答"停在哪" |
| `is_last` | 不知道一句/一轮什么时候结束 |
| `seq` | 无法发现丢块、无法补静音 |

**建议新增 3 条控制消息（放在 `tts_started` / `tts_completed` 两侧，媒体无关）**：

```jsonc
// S→C：一轮播报开始时发一次，携带全部句子与时间轴（RTC / WS 通用）
{"type":"tts_started","turn_id":"t_0004","reply_id":"r_0004_1",
 "sample_rate":24000,"channels":1,"total_ms":4200,
 "sentences":[
   {"sentence_id":1,"text":"可以的，我先帮您看一下订单。","char_start":0,"char_end":15,"start_ms":0,"end_ms":1800},
   {"sentence_id":2,"text":"订单目前显示明天上午送达。","char_start":15,"char_end":29,"start_ms":1980,"end_ms":4100}
 ]}

// S→C：单句开始/结束时也可增量补（云 TTS 拿不到总时长时用这条）
{"type":"tts_sentence","turn_id":"t_0004","reply_id":"r_0004_1","sentence_id":2,
 "start_ms":1980,"end_ms":4100}

// S→C：播报结束（正常或被取消）
{"type":"tts_completed","turn_id":"t_0004","reply_id":"r_0004_1",
 "total_ms":4100,"interrupted":false}
```

**对 SinCircle 的硬要求**（这决定 G3 是否可实现）：`start_ms` 必须是**真实音频偏移**（含句间静音），不是估算；句级误差 < 50ms。云端 TTS 给不出时间戳时，按句合成后自行计算。做不到就把 `capabilities.tts_alignment=false` 明确报出来，前端会退化为"按 `total_ms` 均分估算"，同时 D 要知道游标是估的。

---

### G4 降级协议：只有事件，没有消息

03 §2.5 有 `MEDIA_DISCONNECTED`，04 §2.1 有 `Degraded` 事件，05 §3 有"双栈 + 配置开关切"。但**客户端与服务端之间没有一条"切换传输"的消息**。W2 演示时（尤其评委现场断网/丢包）前端必须知道：是走 ICE restart、切 TURN、切 aiortc、还是退回 WS+PCM、还是只能文本。

**建议新增一组（含触发条件，别让双方各自猜）**：

```jsonc
// C→S：前端判定媒体不可用（判据写进契约，别口头约定）
{"type":"transport_degraded","from":"webrtc","to":"ws_pcm","reason":"ice_failed",
 "stats":{"rtt_ms":180,"loss_pct":12.4,"jitter_ms":95,"packets_received_stalled_ms":1600}}
// S→C：确认切换，并给出新的媒体参数
{"type":"transport_switched","to":"ws_pcm","audio":{"uplink":"pcm16/16000","downlink":"pcm16/24000"}}
// S→C：无法降级（例如 WS 也失败）
{"type":"transport_failed","reason":"ws_unavailable","fallback":"text_only"}
```

**判据建议冻结**（前端照此实现，不自定义）：`iceConnectionState ∈ {failed}` 或 `disconnected` 持续 ≥3s；或 `packetsReceived` 停滞 ≥1500ms；或 ICE 首帧 2s 未达。**每会话最多降级一次，30s 内不回切**，避免抖动。

---

### G5 挂断接口缺失

03 §4.1 有 `POST /sessions/{id}/cancel`，但注明"仅调试/管理"。用户点"挂断"时前端调什么？没有。→ 无法触发 `SessionEnded`、无法写 `result.json`、演示时只能关标签页。

```jsonc
// C→S（推荐走 WS，保证顺序；HTTP 作为兜底）
{"type":"hangup","reason":"user_click","client_mono_ms":88123.4}
// S→C
{"type":"session_ended","reason":"user_click","duration_ms":88200,"turns":3,"outcome":"rescheduled"}
```

---

### G6 WS 鉴权方式不可实现

03 §4.1：`token` 是 JWT，`aud=session_id`，"WebSocket 握手校验"。但**浏览器 `new WebSocket()` 不能设置自定义 header**。现在契约没写怎么传，很容易变成"两端各写一套：一个用 header，一个用 query"，联调当场失败。

**建议冻结**：`ws://HOST:PORT/ws/session/{session_id}?token=<JWT>`，且：
- 服务端必须接受 query 参数（header 方式仅用于非浏览器客户端）；
- token 校验失败回 `4401` 关闭码，前端据此提示"会话过期，请重新开始"，**不要无限重连**;
- TTL 30min 的续期方式：重连时用 `POST /sessions/{id}/token` 换新（或把 TTL 提到 2h，演示够用）。

---

### G7 `state` 枚举未冻结

`{"type":"state","turn_state":"LISTENING","flow_state":"NEED_CONFIRM","slots":{...}}` —— 两个枚举只在 02 §6 各出现过一次示意。前端要做状态灯和六阶段进度条，必须拿到**闭合枚举**。

```jsonc
// 请冻结为（与 02 §6 一致，补上异常态）
"turn_state": "IDLE|CONNECTING|GREETING|LISTENING|USER_SPEAKING|ENDPOINTING|THINKING|SPEAKING|WRAP_UP|CLOSING|ENDED|DEGRADED|FAILED"
"flow_state": "OPEN|IDENTITY_CONFIRM|NEED_CONFIRM|OFFER|RESULT_CONFIRM|WRITEBACK|CLOSE|TRANSFER|END|FALLBACK"
```

并补一张**六阶段 ↔ flow_state 映射表**（赛题门槛 2 要求"六阶段 + 回写"，前端进度条必须与 D 的闭环率口径一致）：

| 赛题六阶段 | flow_state | 进度条 |
|---|---|---|
| 开场自报身份 | `OPEN` | ① 身份披露 |
| 身份确认 | `IDENTITY_CONFIRM` | ② 身份确认 |
| 需求确认 | `NEED_CONFIRM` | ③ 需求确认 |
| 方案/权益沟通 | `OFFER` | ④ 方案沟通 |
| 结果确认 | `RESULT_CONFIRM` | ⑤ 结果确认 |
| 结构化回写 | `WRITEBACK` → `CLOSE` | ⑥ 回写完成 |

`slots` 也要给字段清单（至少 `order_id`、`new_time`、`consent`），否则前端只能 `JSON.stringify` 一把梭。

---

### G8 打断的 `b3` 无上报点

01 §3.2 定义打断停止时延 = `b3 - b0`，其中 **b3 = 客户端最后一个 Agent 音频帧播出的时刻**。03 §4.2 只有 `local_muted`（b0 之后的本地动作），**没有 b3**。服务端算的 `BargeInConfirmed.stop_latency_ms` 是"取消完成"时延，跟用户耳朵听到的停止时刻不是一回事（差一个网络 RTT + 缓冲）。

```jsonc
// C→S：本地 duck 完成（b2'）与最后一个残留音频帧的播出时刻（b3）
{"type":"local_muted","turn_id":"t_0004","reply_id":"r_0004_1",
 "client_mono_ms":13001.0,"reason":"barge_in",
 "played_ms":3120,                    // 本 reply 已播出多少毫秒
 "sentence_id":2,"char_idx":7,        // 播到第几句第几字（靠 G3 的 tts_started 计算）
 "last_frame_played_mono_ms":13001.2} // ← b3，官方"打断停止"只认这个
```

**同时请把两个指标分开命名**，避免答辩时被问倒：
- **用户可感停止时延** = `b3 - b0`（前端算，目标 ≤100ms）
- **服务端静默时延** = `t_barge_in_confirmed - b0`（服务端算，目标 ≤300ms）

---

### G9 交付物缺口：`contracts/` 与 code skeleton 不在仓库里

`contracts-signoff.md` §3 写着"已确认的事实：4 个 fixture / 47 条事件通过 schema；8 个测试通过"，`README(1).md` §2 索引里写着"`../README.md`：代码骨架 ✅ Fake 骨架可跑"，`04` §6 写着 `contracts/schemas/event.v1.json` 等。**但当前仓库里只有这份 `docs/`，没有 `contracts/`、没有 `app/`、没有 `README.md`。**

05 D1 给我（cx）的活是"最小页面 + 用 fixtures 渲染"，没有 fixture 文件我无法开工。**请提供**：

```
contracts/schemas/event.v1.json
contracts/schemas/agent_chunk.v1.json
contracts/fixtures/session_normal.jsonl
contracts/fixtures/session_bargein.jsonl
contracts/fixtures/session_tool_fail.jsonl
contracts/fixtures/session_compliance_block.jsonl
contracts/validate.py
```

另请给我一份**可直接 curl 的地址清单**（G26）：`APP_BASE`、`WEB_BASE`、`WS_PATH`、`LIVEKIT_URL`、是否需要 token 前缀。否则我的 `.env` 只能猜，猜错就是"连不上"，而这类问题每次联调要吃掉半小时。

---

## 2. P1 简述（不阻塞会签，但 W2 前必须有）

| # | 缺口 | 一句建议 |
|---|---|---|
| G10 | `audio_chunk` 无 `sample_rate`/`channels` | 每条都带；或会话级在 `session_ready` 里给 `downlink_sample_rate`，并要求 TTS 若换采样率必须提前一条 `tts_started`（G3 已含 `sample_rate`） |
| G11 | `BargeInCancelled` 后"从下一句继续"不可实现 | 二选一：①客户端 duck 时不丢已收音频（只静音，保留 ≤400ms 缓冲），恢复即时；②服务端从 `sentence_id+1` 重发。**推荐①**，但要写明"客户端必须保留缓冲，不得 stop" |
| G12 | 客户端事件不完整 | 允许一条通用 `client_event{name, payload, client_mono_ms}`，白名单 = `PlaybackCancelled / PlaybackUnderrun / NetStats / VadCorrection / AecState / ClockSuspect`；服务端包 envelope 并重新分配 `seq`（04 §1 已规定客户端事件由服务端重分配 `seq`，但没有入口消息） |
| G13 | 合规展示无下发 | `{"type":"compliance","scope":"sentence","decision":"REWRITE","rule_ids":["false_promise"],"before":"保证明天送到","after":"以实际结果为准","severity":"warn"}`。03 §1 把"展示合规命中与改写前后对照"派给了 cx，但没有数据来源 |
| G14 | 工具进度无下发 | `{"type":"tool_progress","call_id":"c_...","tool":"order.query","state":"calling|ok|failed","latency_ms":123}` |
| G15 | 回写结果无下发 | `{"type":"turn_result","turn_id":"t_0012","outcome":"rescheduled","slots":{...},"result":{"new_time":"...","ticket_id":"WO..."}}`（与 `AgentChunk.end.result` 同结构，直接转发即可） |
| G16 | `t0` 未定义 | 建议**双报**：客户端在 `speech_ended` 里带上"最后一个有声帧的 client_mono_ms"，服务端 VAD 也给出自己的 `t0`，`TurnLatency` 同时存 `t0_client` 与 `t0_vad` → 官方口径一公布，选一列即可，不用重测 |
| G17 | `audio_ctx_time` 用途不明；ping 不回显客户端单调时间 | 明确 `audio_ctx_time = AudioContext.currentTime`，用途是"验证播出时刻的精度"；`pong` 必须回显 `client_mono_ms` 与 `server_rx_mono/server_tx_mono`，让客户端也能自算 `clock_suspect` |
| G18 | 错误码→UI 动作 | 补一列"前端动作"：`ASR_EMPTY`→提示不阻断；`TTS_TIMEOUT`→文本气泡；`MEDIA_DISCONNECTED`→触发 G4 降级；`COMPLIANCE_BLOCK`→红色拦截标记；`SESSION_TIMEOUT`→结束语+挂断。现在只有 `user_message` 与 `fallback`，颗粒度不够 |
| G19 | 自动播放 | 契约层加一句：**`GREETING` 必须在用户手势之后触发**（进入通话页先点"开始通话"）。否则预合成开场白在 Chrome 上会被 autoplay 策略静音，现场表现为"接通了但没声音"，而且极难 debug |
| G20 | AEC/双讲状态未协商 | `hello.capabilities` 从字符串数组改为结构化：`{"local_vad":true,"aec":"browser|off","duplex":"full|half","echo_cancel_verified":true}`；服务端在 `aec="off"` 或 `duplex="half"` 时启用半双工兜底（播放期不上行或提高 VAD 门限）。这是 R02"无自激"的可执行保证 |
| G21 | 静音超时阈值不统一 | 02 §6 有 10s/30s/45s 三个数字，03 `SESSION_TIMEOUT` 只说"总时长/静音超时"。请在 `session_ready` 下发 `timeouts:{listening_silence_ms, session_max_ms, wrapup_ms}`，前端据此做倒计时提示 |

---

## 3. P2（记 backlog，别现在讨论）

- **G22** `session_ready` 加 `server_version` + `protocol_version`，不兼容直接 `error` 早失败。
- **G23** `latency_ms` 的 `payload.latency_from` 语义补一句（是"从哪个事件的 ts 算起"）。
- **G24** 心跳：`ping` 间隔 10s、连续 3 次无 `pong` 判死，走 G4。
- **G25** `mode:"text"` 是否允许中途切换 → 建议**不允许**，语音失败就降级到 WS，再不行 `text_only`（复用 G4 的 `transport_failed.fallback`），不要动态改 mode。
- **G26** 冻结 `.env` 清单：`APP_BASE / WEB_BASE / WS_PATH / LIVEKIT_URL / SESSION_MODE`。

---

## 4. 命名对齐：我这边先改，不用契约迁就我

我此前按"前端自研契约"写了一套文档（`docs/00/A/B/C/D/E`），与 03/04 的命名不一致。**以 03/04 为准，我改**。映射关系如下（也是我接下来改文档的清单）：

| 我文档里的名字 | 03/04 的正式名字 | 语义变化 | 影响 |
|---|---|---|---|
| `callId` | `session_id` | 相同 | 全局替换 |
| `turnId` | `turn_id` | 相同 | 全局替换 |
| `ttsSeq`（一次播报） | `reply_id`（一次播报） + `sentence_id`（句） | **契约更细：播报被拆成"回复+句子"两级** | 打断定位、游标改成 `(reply_id, sentence_id, char_idx)` 三元组 |
| `uttSeq`（用户语音段） | 无独立 ID（`turn_id` 承担） | 简化 | 删除 |
| `toolCallId` | `call_id` | 注意：`call_id` 在 03 里指**工具调用**，不是通话 | 我原文档里"callId=通话"的用法必须全部改掉，否则歧义极大 |
| `StepId`（5 枚举） | `flow_state`（7+3 状态） | 契约更全 | 进度条按 flow_state 映射（见 G7 表） |
| `TransportEvent` 联合类型 | WS 消息 + 事件总线双轨 | 我的"单一事件流"设想不成立 | 前端要同时消费 WS 消息（控制）与事件（回放/指标） |
| `/api/events` 批量上报 | WS `client_event` + EventBus 重分配 `seq` | 同上 | 改 |
| `capabilities`（结构化 tts.delivery 等） | `hello.capabilities` 字符串数组 + `session_ready.version_set` | 契约偏简 | 需要扩展（G20、G10） |
| `DownlinkStream`（云/本地 TTS 收敛点） | 仍在客户端内部，不是契约 | 无 | 保留，但 `form` 判据改为"当前 `transport` 是 rtc 还是 ws"，不再由 `tts.mode` 推 |

> 一句提醒：`ttsSeq → (reply_id, sentence_id)` 这个拆分是**契约比我的方案更好**的地方，我接受。但它让 G3 变成真正的 P0 —— 因为拆细之后，元数据必须逐句下发，而换轨后只有控制面能承担这件事。

---

## 5. 我需要的"不是契约但决定你能不能验收我"的三件事

| # | 事项 | 要谁拍 | 为什么现在要 |
|---|---|---|---|
| D1 | **演示访问方式**：评委在跑 Docker 那台机器上开 `http://localhost:3000`，还是另一台机器走自签 HTTPS？ | 天命（09 §7 已识别，但没定） | 决定我是否要做"无麦克风演示模式"（File 音源回灌）。**这一条我建议无论怎么定都做**，它同时是 E2E 自动化的基础，也是现场保险 |
| D2 | **LiveKit 是否会真的落地**（05 说 W2，09 说交付含 livekit 容器） | 天命 | 若落地，我这周就要按形态 1 写 adapter；若不落地，直接写 aiortc 形态 2。**两条路的代码量差 2 倍**，不能等 W2 再决定 |
| D3 | **首字时延的验收口径**：`t9-t0`（保守）还是 `t9-t1`（官方代理） | 天命 + 湖中日月 | 决定我在 UI 上把哪个数字放大显示、D 用哪列算 P50。建议**两个都显示，主显保守口径**，并在方案书里写明"官方口径公布后只换列不改埋点" |

---

## 6. 建议的契约会议程（60 分钟，按这个顺序过）

1. **(10min) G1/G6/G26**：把"怎么连、怎么传音频、怎么鉴权"三项定死 —— 这是 D1 前端能不能开工的唯一前提。
2. **(15min) G3/G8**：元数据与打断锚点。请 SinCircle 当场回答"`start_ms` 能否做到 50ms 内真实偏移"，答不了就立刻把 `tts_alignment=false` 写进契约，别留到 D4 才发现。
3. **(10min) G2/G4**：媒体形态二选一（LiveKit / aiortc）+ 降级判据。**必须今天选一个**，否则我写两份 adatper。
4. **(10min) G7/G13/G14/G15**：给前端的四条展示用消息（state/合规/工具/结果）。这四条决定 demo 页能不能"一次说清系统在干什么"。
5. **(10min) G9/G26**：交付物与地址清单，谁在什么时候给。
6. **(5min) 会签**：按 `contracts-signoff.md` 原样签，但签注"以 v0.2 补齐 G1–G9 为生效条件"。

---

## 7. 我的会签答复（可直接抄进 `contracts-signoff.md`）

> **cx（前端）：有条件同意。**
> 03/04 的骨架（ID/时间/音频规格/错误模型/取消语义/事件 Envelope/双状态机/TurnLatency）可以冻结。
> **生效条件**：v0.2 补齐 G1 上行音频帧、G2 媒体信令、G3 播报元数据（`tts_started/tts_sentence/tts_completed`）、G4 降级协议、G5 挂断、G6 WS 鉴权方式、G7 枚举、G8 打断 b3、G9 fixtures 与地址清单。
> 我已核对：G10–G21 可在 W2 前补齐，不阻塞 W1 开工。
> 我方承诺：命名一律对齐 03/04（见我文档的映射表），不私自定义对方可接受但契约没有的字段。

---

## 附：v0.2 补丁建议的最小集（12 条消息）

如果时间紧，只加这 12 条就能让 W1 + W2 全程不再被契约卡住：

```
C→S：audio_frame（G1） rtc_offer/rtc_candidate（G2） transport_degraded（G4） hangup（G5） client_event（G12）
S→C：media_ready 或 rtc_answer（G2） tts_started（G3） tts_completed（G3） transport_switched（G4）
      turn_result（G15） compliance（G13） session_ended（G5）
```

其余（G7 枚举、G20 能力协商、G21 超时）是"补字段"，不是"补消息"，成本更低，可以并入同一版。
