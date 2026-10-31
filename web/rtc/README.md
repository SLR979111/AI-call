# web · 前端通话台（cx / 契约 v0.2.1）

零依赖、免构建：纯 ESM + 浏览器原生 API。`node` 起一个 mock 就能看到完整链路。
（本轮开工时 npm registry 不可达，所以不引 React/Vite；等网络恢复再包一层构建，源码不用改。）

---

## 一、跑起来（3 条命令）

```bash
# 1) 起 mock（自带静态托管 + 契约 Mock，零依赖）
node web/rtc/mock/mock-server.mjs          # 默认 8099

# 2) 浏览器打开（必须 127.0.0.1，局域网 IP 会被拒麦克风）
#    http://127.0.0.1:8099/

# 3) 点「开始通话」→ 说一句 / 或点「模拟打断」/ 或底部输入框打字（text 消息）
```

**入口不会和 mock 绑死**：`config.js` 的 `APP_BASE` 默认 **同源**（`location.origin`）。

| 页面由谁托管 | 打开地址 | 连的是 |
|---|---|---|
| `mock-server.mjs` | `http://127.0.0.1:8099/` | mock（假 Agent） |
| 真后端（uvicorn `:8081`） | `http://127.0.0.1:8081/rtc/` | **真后端**（`app/main.py` 已把 `web/` 挂到 `/`） |
| 其他静态服务 + 别处后端 | `http://HOST:PORT/rtc/?app=http://HOST:8081` | `?app` 指定的后端 |

也就是说：**接入真后端不需要改任何代码、也不需要改 URL**。`?app=` 只在页面与后端不同源时才用；调试面板会打印 `APP_BASE=…（same-origin|query）`，一眼看出当前连谁。

## 一点五、自检（不用浏览器，全部零依赖）

```bash
# ① 语法检查
node --check web/rtc/src/main.js        # 或 cd web/rtc && npm run check

# ② 契约冒烟：21 项断言，约 6 秒（自带 mock，覆盖 C1/打断/回写）
FAST=1 node web/rtc/mock/mock-server.mjs     # Windows: $env:FAST="1"; node web/rtc/mock/mock-server.mjs
node web/rtc/mock/smoke.mjs

# ③ 真实抓包回放：16 项断言，用天命的 ws_session_bargein.jsonl 验前端解析
node web/rtc/test/golden-replay.mjs

# ④ 下行 PCM 路径：19 项断言（stub AudioContext，钉住 W2 真 TTS 的第一天风险）
node web/rtc/test/pcm-path.mjs

# ⑤ 无麦回放音源：10 项断言（帧切分/实时性/mute）
node web/rtc/test/uplink-file.mjs

# ③④⑤ 一次跑完：cd web/rtc && npm test
```

冒烟覆盖：`POST /sessions` → WS 握手带 token → `hello/session_ready` → 开场白 `agent_text` →
`tts_started/tts_sentence/tts_completed`（校验 `tts_alignment=false`、`estimated`、`cursor:null`、`char_start/end`）→
`media_request → MEDIA_NOT_CONFIGURED`（C1）→ 两轮 turn（`compliance` / `tool_progress` / `flow_state`）→
`local_muted → cancel + turn_aborted(BARGE_IN)` → `client_event` 白名单 → `ping/pong` → `hangup → session_ended`。

### 协议外消息（抓包发现，待并入 03/11）

真实 e2e 抓包里出现 3 条 `03/11` 未定义的消息，前端已兼容（不兼容会静默丢协议）：

| 方向 | 消息 | 真实 payload | 前端处理 |
|---|---|---|---|
| S→C | `hello_ack` | `{session_id, protocol_version, server_version, transport}` | 采用其 `transport`，并记日志 |
| S→C | `turn_end` | 与 `turn_result` 同 payload | 与 `turn_result` 合并处理 |
| C→S | `text` | `{text}` | 底部输入框发送（无麦演示 / text 模式） |

另外 `turn_result` 的真实形状是 `{turn_id, result:{kind:'end', outcome, slots, result:{new_time, ticket}}}`（**嵌套**），
不是扁平 `{turn_id, outcome}`；`session.onTurnResult` 两种都归一化成 `{outcome, ticket, newTime}` 再进 store。

## 二、URL 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `app` | **同源**（`location.origin`） | 后端基地址；只在页面与后端不同源时覆盖，如 `?app=http://192.168.1.20:8081` |
| `transport` | `auto` | `webrtc` \| `ws_pcm` \| `auto`；`auto` 遇 `MEDIA_NOT_CONFIGURED` 静默回落（C1） |
| `downlink` | `auto` | `speech`（W1 Fake）\| `pcm`（W2 真 TTS）\| `auto`（400ms 内见 `audio_chunk` 用 pcm） |
| `mic` | 开 | `mic=0` 无麦模式（只用按钮联调与字幕） |
| `uplink` | `auto` | `auto` \| `mic` \| `file`；`auto` 在无麦克风时自动切回放音源 |
| `wav` | `./fixtures/demo_16k.wav` | 回放音源路径（4s：二段有声+二段静音，够 VAD/打断跑两轮） |
| `scenario` | `service_outbound` | 透传给 `POST /sessions` |

## 三、目录

```
web/
├─ index.html                 # 通话台（单页，无框架）
├─ config.js                  # 运行期配置（C5：APP_BASE=8081）
├─ fixtures/demo_16k.wav      # 无麦回放音源（4s，两段有声两段静音）
├─ mock/mock-server.mjs       # 零依赖 Mock：静态 + /sessions + 手写 WS
├─ mock/smoke.mjs             # 契约冒烟（21 项）
├─ test/golden-replay.mjs     # 真实抓包回放（16 项）
├─ test/pcm-path.mjs          # 下行 PCM 路径（19 项，stub AudioContext）
├─ test/uplink-file.mjs       # 无麦回放泵（10 项）
└─ src/
   ├─ protocol.js             # 契约 v0.2.1 唯一编码层（消息名/枚举/音频帧头/base64）
   ├─ session.js              # 唯一 WebSocket 出口：建会话→hello→分发→上行
   ├─ audio-engine.js         # 单例 AudioContext + 采集图（唯一 new AudioContext）
   ├─ capture-worklet.js      # 16k/20ms/320 样本 + 精确降采样（Safari 兜底）
   ├─ uplink-file.js          # 无麦回放源（与麦克风同形的采集帧）
   ├─ vad-local.js            # 前端 VAD：b0 / 打断预触发（端点仍以服务端为准）
   ├─ playback.js             # 下行收敛点：speech / pcm 两形态，同一接口
   ├─ store.js                # 唯一可变状态（只有 session/audio 能写）
   ├─ ui.js                   # 纯渲染，只读 store
   └─ main.js                 # 组装层：唯一写全局时序的地方
```

## 四、已实现的契约点

| 契约 | 落地位置 |
|---|---|
| G1 上行音频帧 `0xA1`+16B 头+640B | `protocol.buildAudioFrame` / `main.onFrame` |
| G2 `media_request → media_ready`，未配置走 503 | `session.requestMedia`（3s 超时回落） |
| G3 `tts_started / tts_sentence / tts_completed` | `session.onTts*` → `store` → `playback` |
| G4 `transport_degraded / switched / failed` | `session.degrade` / `onTransportSwitched` |
| G5 `hangup → session_ended` | `session.hangup` / `ui` 挂断按钮 |
| G6 `ws_url?token=` + `4401` 停止重连 | `session.resolveWsUrl` / `onClose` |
| G7 `turn_state / flow_state` 驱动 UI | `store` + `ui.render`（进度条只前进） |
| G8 `local_muted` 三时间字段 + `stop_latency_ms` | `vad-local`(b0) + `main.triggerBarge` + `session.localMuted` |
| C1 `MEDIA_NOT_CONFIGURED` 静默回落 | `session.onError` |
| C2 `b0` = 首帧，与 `speech_started` 同源 | `vad-local.b0Mono` |
| C3 误打断本地恢复（300ms） | `main.triggerBarge` 定时器 + `playback.unduck` |
| C4 `client_event` 白名单 | `protocol.CLIENT_EVENTS` + `session.clientEvent` |
| C6 `tts_alignment=false` 标「估算」 | `store.replies[].estimated` + UI 徽标 |

## 五、三条实现纪律（写代码前先看）

1. **只有一处 `new AudioContext()`**（`audio-engine.js`）、**一处 `new WebSocket()`**（`session.js`）。
2. **UI 不 import audio/session**，只读 `store`；业务状态一律来自服务端 `state`。
3. **打断先本地静音再发消息**，且 `duck()` 不丢缓冲（否则 G11 误打断无法续播）。

## 六、下一步（W1 余下）

- [ ] `playback_first_frame` 接到真实首采样（现在 speech 形态用 `onstart`，pcm 形态用调度时刻）
- [ ] 无麦演示模式：`FileUplink`（把 wav 灌进采集管线），现场保险 + E2E 基础
- [ ] 用 `contracts/fixtures/ws_session_*.jsonl` 做 golden 回放（P5，等天命落 fixture）
- [ ] 网络恢复后加 Vite/TS 构建层（`.ts` 化 + 类型检查），业务代码不动
