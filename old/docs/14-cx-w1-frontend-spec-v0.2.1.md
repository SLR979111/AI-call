# 12 · cx W1 前端实现基线（对齐契约 v0.2）

> **状态**：契约 `v0.2` 已签（`contracts-signoff.md`），W1 前端开工基线。
> **上位文档**：`03-interface-contract-v0.1.md` + `11-contract-v0.2-patch.md`（两者冲突时以 11 为准）
> **本文替代**：`E_前端WebRTC_实现规格.md` 的**命名与消息部分**（E 仍可作模块级算法参考，但字段名一律以本文为准）
> **一句话**：前端不做业务判断，只做四件事——**把音频送上去、把音频放出来、把状态显示对、把时间戳打准**。

---

## §1 冻结的配置（C5）

```bash
# .env.local —— W1 联调（当前 app 实跑在 8081）
APP_BASE=http://127.0.0.1:8081
WEB_BASE=http://127.0.0.1:8081
# 会话建立后直接用 POST /sessions 返回的 ws_url（已含 token），不要再自己拼
DEV_NO_AUTH=1
# 传输偏好：livekit 未配置时自动回落 ws_pcm（见 §10）
TRANSPORT_PREF=webrtc          # webrtc | ws_pcm | auto
WS_FALLBACK=1
# 音频
UPLINK_RATE=16000
DOWNLINK_RATE=24000            # 以 tts_started.sample_rate 为准，此值仅兜底
```

> **注意**：`107.0.0.1` 属浏览器"安全上下文"，麦克风可用；换成 `192.168.x.x` 会被 `getUserMedia` 直接拒。这条决定了演示时**评委必须在跑 Docker 那台机器上开浏览器**（09 §7 已确认）。

---

## §2 消息处理全表（收到什么 → 前端做什么）

### §2.1 C→S（前端发送）

| 消息 | 发送时机 | 必带字段 | 备注 |
|---|---|---|---|
| `hello` | WS `onopen` 后立刻 | `session_id, client_version:"web@0.2", capabilities` | `capabilities` 用结构化草案（G20）：`{"local_vad":true,"aec":"browser","duplex":"full"}` |
| 二进制 `audio_frame` | 每个 20ms 采集帧（`transport=ws_pcm` 时） | 帧头 16B + 640B PCM | WebRTC 期**停止发送** |
| `speech_started` | 本地 VAD 命中，**在 `local_muted` 之后** | `client_ts_ms, client_mono_ms, energy_db, source:"local_vad"` | `client_mono_ms` 必须与 `b0` 同源（C2） |
| `local_muted` | 本地 duck 完成（打断时） | `turn_id, reply_id, client_mono_ms, reason:"barge_in", played_ms, sentence_id, char_idx, last_frame_played_mono_ms, stop_latency_ms` | 见 §6 |
| `speech_ended` | 本地 VAD 判结束 | `client_mono_ms, last_voiced_mono_ms, reason` | `last_voiced_mono_ms` 供服务端算 `t0_client`（G16） |
| `playback_first_frame` | **首个采样真正播出** | `turn_id, reply_id, client_mono_ms, audio_ctx_time` | 见 §5.3，官方时延终点 |
| `playback_ended` | 一句/一轮播完 | `turn_id, reply_id, client_mono_ms, played_ms` | |
| `client_event` | 见 §8 白名单 | `name, payload, client_mono_ms` | 服务端重分配 `seq` |
| `media_request` | 进入 `GREETING` 前 | — | 失败按 C1 静默回落 |
| `transport_degraded` | 满足 §10 判据 | `from, to, reason, stats` | 每会话最多一次 |
| `hangup` | 用户点挂断 | `reason:"user_click", client_mono_ms` | |
| `ping` | 每 10s | `t, client_mono_ms` | `pong` 回来算 RTT/offset |

### §2.2 S→C（前端响应）

| 消息 | 前端动作 |
|---|---|
| `session_ready` | 存 `session_id/version_set/timeouts`；UI 显示 `release_id`；起心跳；解锁 `AudioContext`（已由手势解锁则直接 `resume`） |
| `state` | **唯一的状态来源**：`turn_state` 驱动顶部状态灯，`flow_state` 驱动六阶段进度条，`slots` 渲染槽位面板 |
| `transcript` | `final:false` → 覆盖当前用户句；`final:true` → 定稿入字幕列表 |
| `agent_text` | 按 `reply_id/sentence_id` 累加；`final:true` 标该句完成 |
| `tts_started` | 建 utterance：`reply_id`、`sample_rate`、`tts_alignment`、`sentences[]`（可能为空） |
| `tts_sentence` | 追加/覆盖句级时间轴；**在收到首条前游标为 `null`**（C6） |
| `tts_completed` | 结束 utterance；`interrupted:true` 时 UI 给该 reply 打删除线 |
| `audio_chunk` | （仅 `ws_pcm`）解 base64 → `AudioBuffer(sample_rate)` → 预调度 120ms 后播 |
| `media_ready` | （WebRTC）LiveKit 入房；**不允许**用其 DataChannel 收 `state/transcript/agent_text/cancel` |
| `cancel` | 立即本地 duck + 作废旧 `reply_id`（服务端已决定取消） |
| `compliance` | 合规面板：`decision/rule_ids/before/after` 双列展示（before 划线、after 高亮） |
| `tool_progress` | 工具卡：`calling → ok/failed` + `latency_ms` |
| `turn_result` | 结果卡：`outcome/slots/result`（含 `ticket_id`），六阶段最后一步打勾 |
| `error` | 按 §9 表格的动作分支；`fatal` 直接 `ended` |
| `transport_switched` | 切音频实现（`rtc-track ↔ buffers`），**不重建会话、不清字幕** |
| `pong` | 记录 `client_mono_ms/server_rx/server_tx` → 算 `offset/rtt`，3 次取中位数，偏差 >50ms 标 `clock_suspect` |
| `session_ended` | 停采集、停播放、上传剩余 `client_event`、跳结算页 |

---

## §3 启动时序（含自动播放解锁，G19）

```
用户点「开始通话」（唯一手势入口，必须有）
  → getUserMedia + new AudioContext + await ctx.resume()        // 手势内同步链
  → POST /sessions {mode:"voice", scenario_id?, release_id?} → {session_id, token, ws_url, version_set}
  → new WebSocket(ws_url) → onopen → hello
  → session_ready → media_request
      ├─ media_ready      → transport=webrtc（LiveKit 入房）
      └─ error 503/MEDIA_NOT_CONFIGURED → 静默 transport=ws_pcm（C1，UI 角标"媒体未配置"）
  → 起采集 worklet → 发首帧 → state:GREETING → 开场白播报
```

**禁止**：`new AudioContext()` 出现在手势链之外；页面加载即建 WS（会产生无 `session_id` 的孤儿会话）。

---

## §4 目录与依赖方向

```
web/src/
├─ protocol/         types.ts(对齐 03/04) · codec.ts(WS 收发) · schema.ts(dev 断言)
├─ session/          sessionClient.ts(唯一 WS 出口) · hello.ts · clock.ts(ping/pong)
├─ audio/            engine.ts(单例 ctx/设备) · capture.worklet.ts · resample.ts
│                    vad.local.ts · playback/ws.pcm.ts · playback/rtc.track.ts
├─ ui/               CallPage · Transcript · StageBar(六阶段) · CompliancePanel
│                    ToolCard · ResultCard · NetBadge · DebugDrawer
└─ store/            callStore.ts(zustand：字幕/电平/状态/指标)
```

**依赖不变量**（code review 红线）：
1. `ui/` 不得 import `audio/` 与 `session/` 的具体实现，只读 store。
2. `store` 只被 `session/` 与 `audio/` 写，`ui/` 只读。
3. 全项目只有一个 `new AudioContext()`（在 `engine.ts`）；只有一个 `new WebSocket()`（在 `sessionClient.ts`）。
4. 任何地方不得出现 `if (tts.mode === 'cloud')`——云端/本地 TTS 的差异只由 `sample_rate` 与 `tts_alignment` 体现。

---

## §5 音频实现

### §5.1 采集（`capture.worklet.ts`）

```
每 320 样本（20ms@16k）输出一帧：
  int16[i] = clamp(s, -1, 1) * (s < 0 ? 0x8000 : 0x7fff)
  postMessage({ type:'frame', frameSeq, clientMonoMs, energyDb, pcm: buffer }, [buffer])
每 100ms 输出电平：{ type:'meter', rmsDb, peakDb }        // 只驱动 UI，不做 VAD
```
- 读 `audioCtx.sampleRate`（**不假设 16k**）：若 ≠16000，worklet 内先 2 阶低通（截止 0.45×16000）再线性插值降采样。Safari 必须走这条。
- 帧头：`0xA1, ver, rate(u16BE), 320(u16BE), frameSeq(u32BE), clientMonoMs(u32BE), flags(u16BE)` = 16B + 640B。
- **禁止** `ScriptProcessorNode`；**禁止**每 128 帧 postMessage。

### §5.2 本地 VAD（`vad.local.ts`）

```
noiseDb 跟踪：静音段 noiseDb = noiseDb*0.95 + db*0.05，clamp[-80,-35]
门限：thr = max(noiseDb+9, -50)；播放期 gate = max(noiseDb+9, downDb+6)
speech start：连续 3 帧（60ms）> gate          ← 这一帧的 clientMonoMs 即 b0（C2）
speech end  ：连续 20 帧（400ms）< gate
barge-in    ：播放中，连续 8 帧（160ms）> gate
```
门限与窗口在 DebugDrawer 里可调并列实时值——评委问"VAD 怎么调的"要能现场演。

### §5.3 播放与 `playback_first_frame`

| | `ws_pcm` | `rtc`（LiveKit 轨） |
|---|---|---|
| 承载 | `audio_chunk.pcm_b64` | 远端 `MediaStreamTrack` |
| 实现 | `AudioBuffer(1, len, chunk.sample_rate)` → `BufferSource` 预调度 120ms | `<audio>` 元素 + 旁路 `AnalyserNode`(gain=0) 计时 |
| `played_ms` | `(ctx.currentTime - uttStartCtx)*1000`，underrun 时后推 | 旁路 worklet 累加已渲染 128 帧样本 |
| duck | 共享 `GainNode.gain = 0` | `el.volume = 0`（**不用 `muted`**，会让 AEC 参考消失） |
| `playback_first_frame` | 首个 `BufferSource` 真正开始输出的那一刻：`client_mono_ms = performance.now()`，`audio_ctx_time = ctx.currentTime` | 旁路 worklet 首次回调 |

**`playback_first_frame` 只在每轮首个采样时发一次**，不许每句都发（会把 P50 算成"每句首字"）。

---

## §6 打断（C2 + C3，逐毫秒）

```
b0  本地 VAD 首次连续 3 帧越门限的**第一帧** client_mono_ms
    ★ 与 speech_started.client_mono_ms 同源，差值必须 = 0
+0~80ms   gain/volume → 0（本地静音，不等服务端）
         记 b3 = 本地最后一个残留音频帧播出的 client_mono_ms
         记 played_ms（本 reply 已播毫秒）、sentence_id、char_idx（靠 §7 游标）
         发 local_muted{turn_id, reply_id, played_ms, sentence_id, char_idx,
                        last_frame_played_mono_ms: b3, stop_latency_ms: b3-b0}
         作废当前 reply_id（后续该 reply 的 audio_chunk 一律丢弃并计数）
+80ms    发 speech_started{client_mono_ms: b0, ...}
+300ms   误打断判定（仅本地）：
           若此时本地 VAD 未保持连续语音 → unduck() 从缓冲续播
           → client_event{name:"BargeInCancelledLocal"}
           若收到服务端 BargeInConfirmed → 保持静音，等新 turn
```

**两个指标分开报，不许合并**：
- 用户可感停止 `b3 - b0`（前端算，目标 ≤100ms）→ 进 `local_muted.stop_latency_ms`
- 服务端静默 `barge_in_confirmed - b0`（服务端算，目标 ≤300ms）→ 服务端事件

**禁止**：等 `cancel` 消息才静音；`duck` 时把已收音频缓冲丢掉（G11 要求保留 ≤400ms 以便续播）。

---

## §7 已播游标（两条路径）

```
tts_alignment = true :
  played_ms → 在 sentences[] 上二分（start_ms ≤ played_ms < end_ms）
  ratio = (played_ms - s.start_ms) / (s.end_ms - s.start_ms)
  char_idx = s.char_start + round(ratio * (s.char_end - s.char_start))

tts_alignment = false（当前 Fake TTS 就属此类）:
  total = tts_completed.total_ms 或 sentences 里的估算 end_ms
  按各句字符数占比分配时间 → 得到估算 start/end → 同上二分
  ★ UI 必须在字幕旁标「估算」，并把 {alignment:false} 写进 client_event（C6），
    否则 RSI 阶段会把游标误差误判成"话术被截断"
```

`tts_started` 已到但首条 `tts_sentence` 未到时，`cursor()` 返回 `null`，**不得插值猜测**。

---

## §8 埋点（`client_event` 白名单，C4）

| name | 时机 | payload |
|---|---|---|
| `PlaybackCancelled` | 本地 duck 生效 | `reply_id, played_ms, sentence_id, char_idx` |
| `PlaybackUnderrun` | 缓冲空 ≥40ms | `reply_id, gap_ms, at_ms` |
| `NetStats` | 每 2s | `rtt_ms, loss_pct, jitter_ms, bitrate_kbps, jb_delay_ms` |
| `AecState` | 会话建立时一次 | `aec:"browser|off", duplex:"full|half", earphone:true|false` |
| `ClockSync` | 每次 pong | `offset_ms, rtt_ms, suspect` |
| `BargeInCancelledLocal` | 本地误打断恢复 | `reply_id, resume_ms` |
| `VadMetrics` | 每轮结束 | `start_db, floor_db, b0_delta_ms` |

上报失败不得阻塞播放：本地队列 + 重连后补发；页面卸载用 `sendBeacon` 兜底到 `client_event` 的 HTTP 等价入口（若无，则只靠 WS 补发）。

---

## §9 错误码 → UI 动作（对齐 11 §10）

| code | UI |
|---|---|
| `ASR_EMPTY/ASR_TIMEOUT` | 提示"没听清，请再说一遍"，不阻断、不换状态 |
| `LLM_TIMEOUT/LLM_RATE_LIMIT` | 文本气泡"稍慢"，继续等 |
| `TOOL_TIMEOUT/TOOL_5XX` | 工具卡转红 + "可转人工" |
| `TTS_TIMEOUT` | 只留文本气泡，不停会话 |
| `COMPLIANCE_BLOCK` | 红标 + 安全话术气泡（展示 before/after） |
| `MEDIA_DISCONNECTED` | 触发 §10 降级 |
| `MEDIA_NOT_CONFIGURED` | **静默回落 `ws_pcm`**（C1），仅打角标 |
| `SESSION_TIMEOUT` | 倒计时 15s 提示 → 结束语 → 挂断 |
| `LLM_NOT_CONFIGURED` | 演示降级：脚本化 Flow Pack（服务端已实现） |
| WS close `4401` | 提示"会话过期，请重新开始"，**停止重连** |

---

## §10 降级（判据冻结，11 §4）

```
触发（任一）：
  iceConnectionState === 'failed'
  iceConnectionState === 'disconnected' 持续 ≥3s
  packetsReceived 停滞 ≥1500ms
  ICE 首帧 2s 未达
  media_request 返回 MEDIA_NOT_CONFIGURED / 503        ← C1，W1 必然命中

动作：
  发 transport_degraded{from,to:"ws_pcm",reason,stats}
  → 收 transport_switched → 切到 ws_pcm 播放/采集
  → 保留 session_id / turn_id / 字幕 / 进度条（**不重建会话、不清 UI**）
  → 角标："兼容模式"
  每会话最多一次；30s 内不回切
```

**禁止**：降级时重置 `flow_state` 或清空 `completedSteps`（否则闭环率会被算丢）。

---

## §11 六阶段进度条（G7）

`flow_state` → 进度条（与 D 的闭环率口径必须一致，不许前端自造阶段）：

| flow_state | 进度条 |
|---|---|
| `OPEN` | ① 身份披露 |
| `IDENTITY_CONFIRM` | ② 身份确认 |
| `NEED_CONFIRM` | ③ 需求确认 |
| `OFFER` | ④ 方案沟通 |
| `RESULT_CONFIRM` | ⑤ 结果确认 |
| `WRITEBACK` / `CLOSE` | ⑥ 回写完成 |
| `TRANSFER` | 提前打灰 → "已转人工" |
| `FALLBACK` | 打黄标（不判定失败，等 `turn_result`） |
| `END` | 按 `turn_result.outcome` 决定末态 |

进度条只允许"前进"，`flow_state` 回退时保留最高水位（进度条倒退会让评委以为系统出错了）。

---

## §12 里程碑与验收（W1）

| # | 交付 | 验收（本地命令/动作） |
|---|---|---|
| M0 | 骨架：Vite+TS+路由 + `protocol/types.ts` + `sessionClient` + 连上 `/ws/session/{id}?token=` 发 `hello` 收 `session_ready` | 页面显示 `session_id/release_id`，`GET /sessions/{id}/state` 与页面状态一致 |
| M1 | 点"开始通话"→ 建会话 → 收开场白文本（`agent_text`）+ `tts_started/sentence/completed` 字幕渲染 | 3 轮文本可见，`events.jsonl` 里能看到对应 WS 消息 |
| M2 | 采集 + `ws_pcm` 推流 + 播放 `audio_chunk`（`playback_first_frame` 上报） | 说一句话能听到回答；`TurnLatency` 第一次出现完整 `t0..t9` |
| M3 | 本地 VAD + 打断（`local_muted` 全字段）+ `compliance`/`tool_progress`/`turn_result` 三块面板 | 打断后 ≤100ms 静音且无残留；合规 before/after 同屏；结果卡显示 `ticket_id` |

> M3 通过 = D3/D4 Gate 的前端部分就绪。之后 W2 接 LiveKit（`media_ready`）只需替换 `playback/rtc.track.ts`，其余不动。

---

## §13 反模式（写代码时贴在手边）

1. `ScriptProcessorNode` / 每 128 帧 postMessage
2. 假设采集就是 16k（不读 `actualRate`）
3. `element.muted = true` 当打断（AEC 参考会消失 → 自激）
4. 同时 `<audio>` 出声 + `gain=1` 的 WebAudio 出声
5. 等 `cancel` 消息才静音
6. `playback_first_frame` 每句都发
7. 用 `Date.now()` 算时延
8. 用"收到 `audio_chunk` 的时间"当播出时间
9. 前端自己造阶段/状态（不跟 `state` 消息）
10. `tts_alignment=false` 时不标"估算"
11. 降级时重建会话 / 清字幕
12. 在 `ui/` 里出现 `getUserMedia` / `RTCPeerConnection` / `ws.send`

---

## §14 待同步事项（阻塞开工的两件）

1. **仓库同步**：本地工作区目前只有 `docs/`，`contracts/`、`app/`、`tests/`、`web/` 未到位。需要 pull tag `v0.2-contract-patch`（或把 `web/`、`app/` 拷进来），我才能对着真实 `POST /sessions` 与 WS 帧跑 M0，并补 §5.2 的机器级确认。
2. **C1–C5 回写**：C1/C2/C3 已影响我方实现分支，请在 `03` 或 `11` 里加一行确认（C4 白名单、C5 地址同步进 `.env.example`）。C6 建议项待湖中日月确认"估算游标"是否要在报告里单列。
