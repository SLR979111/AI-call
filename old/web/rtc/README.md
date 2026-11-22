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
| `transport` | `auto` | `auto` \| `livekit` \| `webrtc`(=livekit) \| `ws_pcm` \| `rtc_pcm`；`auto` 先要 LiveKit token，拿不到 / 入房失败 / 超时静默回落 `ws_pcm`（C1 + G4） |
| `downlink` | `auto` | `speech`（W1 Fake）\| `pcm`（W2 真 TTS）\| `auto`（400ms 内见 `audio_chunk` 用 pcm） |
| `lkSdk` | `./vendor/livekit-client.esm.mjs` | 覆盖 LiveKit SDK 地址（测试 / 灰度用） |
| `mic` | 开 | `mic=0` 无麦模式（只用按钮联调与字幕） |
| `uplink` | `auto` | `auto` \| `mic` \| `file`；`auto` 在无麦克风时自动切回放音源 |
| `wav` | `./fixtures/demo_16k.wav` | 回放音源路径（4s：二段有声+二段静音，够 VAD/打断跑两轮） |
| `scenario` | `service_outbound` | 透传给 `POST /sessions` |
| `theme` | 开 | `theme=off` 关掉 `theme-inject.js` 的运行时换肤 |

## 二点五、LiveKit 媒体面（入房 / 播放 / AEC）

主通话台已接 LiveKit，`ws_pcm` 作为永久回退保留。两者是**同一层的两种传输**，
业务代码不按传输分支：字幕与控制消息始终走 WebSocket，只有音频走 WebRTC。

```text
session_ready → media_request ──► media_ready{provider:livekit, url, room, token}
                                     │
                     ┌───────────────┴────────────────┐
                入房成功                          任一步失败
                     │                                │
        transport=livekit                    transport_degraded → ws_pcm
        · 发布麦克风（复用采集轨）              · 立刻可用，通话不中断
        · 订阅远端音频 → <audio>
        · AEC/NS/AGC 来自 getUserMedia 约束
```

**四条实现要点**

1. **SDK 懒加载**：`vendor/livekit-client.esm.mjs` 有 1.4MB，`ws_pcm` 用户不该为它付下载
   代价；用动态 `import()` 才能把「SDK 缺失 / 加载失败」变成可捕获异常 → 回退。
   静态 import 失败会让整条模块图崩掉，页面直接白屏。
2. **麦克风只采一次**：把 `audio-engine` 已拿到的 `MediaStreamTrack` 直接
   `new LocalAudioTrack(track, undefined, true)` 发布，不二次 `getUserMedia`。
   `userProvidedTrack=true` 让 SDK 不 stop/重建它，VAD 那条采集图不受影响；
   **AEC/NS/AGC 是 `getUserMedia` 时的约束**，共用同一条 track 才保证
   「VAD 判定的声音」和「发给服务端的声音」是同一份。
3. **打断只静音不销毁**：`duck()` 把远端 `<audio>` 置 `muted`，流继续走、解码不丢；
   `unduck()` 立刻恢复，与 `playback.js` 的 G11「误打断要能续播」同语义。
   打断门控同时认 `playback.isPlaying()` **和** `livekit.isPlaying()`，
   否则 LiveKit 模式下 VAD 永远以为「对方没说话」。
4. **失败必须可追溯**：`transport_degraded.reason` 直接用错误码前缀
   （`LIVEKIT_TOKEN_MISSING` / `LIVEKIT_SDK_LOAD_FAILED` / `LIVEKIT_CONNECT_TIMEOUT` /
   `LIVEKIT_CONNECT_FAILED` / `LIVEKIT_PUBLISH_FAILED`），服务端日志能一眼分清是哪种失败。

右侧「媒体 / LiveKit」卡片实时显示入房状态、房间名、远端音频条数，以及
`getSettings()` 报回来的**实际生效**的 AEC/NS/AGC 与采样率 —— 请求约束不代表浏览器采纳了，
只有 getSettings 才是能验收的口径。

```text
http://127.0.0.1:8091/rtc/?transport=auto&downlink=pcm   有 LiveKit Server 时
http://127.0.0.1:8091/rtc/?transport=ws_pcm              确定性兼容路径
http://127.0.0.1:8091/rtc/?theme=off                     关掉换肤
```

**已知边界（需要服务端 / 桥接配合，见 [docs/23-livekit.md](../../docs/23-livekit.md) 的责任划分）**

- `scripts/livekit_bridge.py` 目前只把 TTS 音频推回房间，**不回传
  `transcript/agent_text/tts_*` 事件**，所以 LiveKit 模式下字幕区为空、`#latestReply`
  停在占位文案。客户端已把空态文案按 `data-lk` 区分，但真正的字幕要桥接侧下发
  （DataChannel 或事件回传接口）。
- 桥接侧 `send_json` 还是打印桩，收不到 `local_muted` / 取消指令，所以 LiveKit 模式下的
  打断目前只做到**本地静音**（停止时延仍可测），服务端 TTS 不会因此停。
- 自动播放策略：入房时若被挂起，客户端提示一句并挂一次性 `pointerdown` 解锁，不自动重试。


## 二点六、新前端逻辑（Decision 流）

> 后端正在把"预测式对话（Decision）"接进来。前端已按新逻辑实现，**只有两个接口**。
> 后端契约尚未合并，所以目前默认走能力探测：探测通过走新逻辑，否则自动回落旧契约。

### 两个接口

```js
// ① 把用户输入推给后端 Decision（音频帧 / 文本 / 本地信号，一个方法）
decision.push({ audio: { pcm, monoMs, muted } });
decision.push({ text: '你好', final: true });
decision.push({ signal: { name: 'barge_in', b0_ms } }, heard);

// ② 挂一个 hook 等音频（流式）；返回取消订阅函数
const off = decision.onAudio(({ meta, pcm, ended, reason }) => { /* 播 */ });
```

### 打断不是协议

用户一开口，前端只做两件本地事：**立刻停播** + **继续推音频**。没有
`local_muted / cancel / turn_aborted` 这套握手。

新的 `epoch` 一到，旧音频与旧字幕全部作废 —— `epoch` 就是取消：

```text
服务端                   前端
audio_segment(epoch=5) → 开始播第 5 轮
audio_chunk(epoch=5)   → 继续播
（用户打断，随便推什么）
audio_segment(epoch=6) → hardStop()：增益归零、清空队列、丢弃所有 epoch<6 的块
```

### 音频块自带元数据

```json
{"type":"audio_segment","utt":"u_1","epoch":5,"kind":"filler","reply_id":"r_0005_filler",
 "sample_rate":24000,"format":"pcm16le","channels":1,"stage":"REASON","is_last":false,
 "sentences":[{"sentence_id":1,"text":"好的，我看一下。","start_ms":0,"end_ms":480,
               "char_start":0,"char_end":8}]}
{"type":"audio_chunk","utt":"u_1","epoch":5,"seq":0,"pcm_b64":"..."}
{"type":"audio_end","utt":"u_1","epoch":5}
```

- `kind`：`filler`（先垫一句的套话）| `answer`（正式回答）。字幕会标「套话」，
  「最新自主回复」卡只显示 `answer`。
- `sample_rate` **每块可不同**：预渲染套话与云端 TTS 采样率常常不一样，
  前端按块的 rate 建 buffer，交给浏览器重采样，所以不会变调。
- 块可以整块带元数据，也可以只带 `{utt,epoch,seq,pcm_b64}` 继承段的元数据 ——
  两种都收（判据是 `kind`/`sample_rate` 是否存在，`epoch` 不算，因为每个块都带）。
- `is_last` 或 `audio_end` 二者之一必须给，否则 `playing` 永远为真、段收不了尾。

### 上行

| 消息 | 说明 |
|---|---|
| 二进制 `0xA1` 帧 | 沿用旧编码（16B 头 + 640B PCM），服务端那侧已有解析器 |
| `{"type":"input","text","final"}` | 文本输入（无麦演示 / 文本模式） |
| `{"type":"input","signal":{...},"heard":{...}}` | `speech_started` / `speech_end` / `barge_in` / `first_frame`；`heard` 带"用户听到第几句、第几字"，供后端做针对性回答 |

`signal` 只是**数据**，不是控制指令：后端不需要为它回 ack，打断也不依赖它。

### 跑起来

```powershell
node web/rtc/mock/mock-server.mjs          # Mock 已实现新契约（含 epoch 切换演示）
# 新逻辑（强制）  http://127.0.0.1:8099/?mode=stream&mic=0
# 能力探测自动选 http://127.0.0.1:8099/?mic=0
# 旧契约         http://127.0.0.1:8099/?mode=legacy
```

Mock 每次输入会先出一段 440Hz 套话（24k），900ms 后再出一段 620Hz 正式回答（22k），
两段采样率不同 —— 用来验证 per-chunk 重采样。连续输入两次即可看到 epoch 顶掉旧音频。

### 代码在哪

```text
src/boot.js        入口分发：探测 /ws/decision，可用走新逻辑，否则回落旧契约
src/decision.js    两个接口（唯一实现）+ epoch 记账 + 下行分发
src/player.js      流式播放：epoch 过滤 / per-chunk 采样率 / is_last / 打断游标
src/stream-app.js  新装配：engine + vad + ui + decision + player
src/main.js        旧装配（ws_pcm 契约），保留不删，作为回落路径
```

**后端尚未实现的部分**：`/ws/decision` 端点与上述消息契约。前端已按此实现并可用
Mock 全链路验证；后端就绪后把 `MODE` 默认切到 `stream` 即可，无需改前端代码。


## 三、目录

```
web/
├─ index.html                 # 通话台（单页，无框架）
├─ config.js                  # 运行期配置（C5：APP_BASE=8081）+ 换肤挂载
├─ theme-inject.js            # 运行时换肤（注入 <style>，不改 DOM、不进 release 指纹）
├─ livekit-test.html          # LiveKit 媒体面最小测试页（只验浏览器 ↔ Server）
├─ fixtures/demo_16k.wav      # 无麦回放音源（4s，两段有声两段静音）
├─ vendor/livekit-client.esm.mjs  # LiveKit 客户端 SDK 随仓库副本（懒加载，2.23.0）
├─ mock/mock-server.mjs       # 零依赖 Mock：静态 + /sessions + 手写 WS
├─ mock/smoke.mjs             # 契约冒烟（21 项）
├─ test/golden-replay.mjs     # 真实抓包回放（16 项）
├─ test/pcm-path.mjs          # 下行 PCM 路径（19 项，stub AudioContext）
├─ test/uplink-file.mjs       # 无麦回放泵（10 项）
├─ test/livekit.mjs           # LiveKit 入房 / 播放 / 打断 / 回退（21 项，假 SDK）
└─ src/
   ├─ protocol.js             # 契约 v0.2.1 唯一编码层（消息名/枚举/音频帧头/base64）
   ├─ session.js              # 唯一 WebSocket 出口：建会话→hello→分发→上行 + 传输协商
   ├─ livekit.js              # LiveKit 媒体面：入房 / 发布麦克风 / 播放远端音频 / 打断静音
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

## 六、打断（barge-in）到底怎么走 —— 最容易被误解的一段

期望时序（契约 `03 §3.2` + 附条件 C2/C3）：

```
T+0      用户开口：本地 VAD 连续 3 帧（60ms）越限 → 记 b0 = **第一帧**的时刻
T+<80ms  本地静音：speech → speechSynthesis.cancel()；pcm → gain=0
         记 b3 / played_ms / 第几句第几字 → 发 local_muted{ b0_mono_ms, last_frame_played_mono_ms, stop_latency_ms, played_ms, sentence_id, char_idx }
         ⚠️ 客户端只说"我静音了"，**不说"取消"**——取消权在服务端
T+~RTT   服务端：CancelRequested → B 停合成 + 清推流缓冲 → C 取消本轮 LLM → 回 cancel + turn_aborted
T+?      前端收到 cancel / turn_aborted → **封禁该 reply_id** + 硬停 → 进 LISTENING，提示"请继续说"
         然后等用户说完 → 服务端 ASR final → 新一轮 LLM → **新的 reply_id** → 才播
```

三条铁律（`test/barge.mjs` 逐条锁住）：

1. **打断后不自行恢复**。只有"误打断"才允许恢复，且必须同时满足：① 触发源是本地 VAD（手动按钮没有 VAD 证据，一律当真打断）；② 300ms 内没继续说；③ 服务端还没 ack 取消。
2. **被取消的 reply 永久封禁**（`session.abortedReplies`）：同 reply 的 `tts_started / tts_sentence / audio_chunk` 全部丢弃并计数——这条防的正是"打断之后又把原话播完"。
3. **恢复不重播半句**：`speechSynthesis.cancel()` 会丢弃当前 utterance，无法"续播半句"，所以恢复点 = 被打断那句的**下一句**（`02 §5.2` 规则 4）。

### 6.1 纯打断、然后什么都不说（"光打断不说话"）

这是最常见的一种：用户抢话但没继续讲。**兜底话术必须由服务端出，前端不硬编码**（契约 `§0` 第 4 条）——原因是"要不要说、说什么、什么时候说"都是业务，只有服务端知道这一轮有没有真的开始。

```
T+0      local_muted → 服务端 cancel + turn_aborted
T+N      N 秒内没有 ASR partial / 没有新 turn / 没有 text 消息
         → 服务端以**正常通道**下发：state(SPEAKING) → agent_text → tts_started/tts_sentence/tts_completed
         → 前端零改动（任何新 reply 都走同一条播放链路）
T+?      用户一开口/一打字 → 服务端撤掉计时器，恢复正常对话
```

实现约定（`mock-server.mjs` 里是可跑的参考实现）：

| 项 | 建议值 | 说明 |
|---|---|---|
| 静默阈值 `N` | **2000–3000ms**（mock 默认 2600，可用 `NUDGE_MS` 调） | 太短会打断用户的思考停顿；太长用户以为掉线 |
| 取消条件 | 收到 `speech_started` / `text` / 新一轮 `TurnStarted` | 只要有任何用户输入就撤 |
| 话术归属 | Flow Pack 的 `nudge` 模板（`rules.yaml` 同源），随 `configVersion` 版本化 | 方便 D 做 A/B；也便于合规检查（"请问您对刚刚的话有什么疑问吗？"属于追问话术，不是必说项） |
| 触发次数 | **每轮最多 1 次** | 防止"打扰式追问"循环 |
| 上报 | 正常 `agent_text` + `tts_*` | 不走私有消息，事件/评测链路自动覆盖 |

前端在这一步只做一件纯 UI 的事：打断后立刻在字幕区插一条灰底虚线提示 `（已打断，正在等您继续说话…）`，收到任何 `transcript` / `agent_text` 就自动消失——**它不出声、也不算业务话术**。

## 七、下一步（W1 余下）

- [x] 无麦演示模式：`FileUplink`（wav 灌进采集管线），现场保险 + E2E 基础
- [x] golden 回放：`contracts/fixtures/ws_session_bargein.jsonl`（`test/golden-replay.mjs`）
- [x] 下行 PCM 路径单测（`test/pcm-path.mjs`，W2 真 TTS 前置）
- [x] 打断语义单测（`test/barge.mjs`）
- [ ] `playback_first_frame` 在 speech 形态用 `onstart`（±30ms），接真 TTS 后走 pcm 形态即精确
- [ ] 网络恢复后加 Vite/TS 构建层（`.ts` 化 + 类型检查），业务代码不动
