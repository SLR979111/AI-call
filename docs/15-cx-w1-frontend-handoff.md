# 15 · cx W1 前端交付说明（对齐契约 v0.2.1）

> 分支：`feat/web-rtc-cx` ｜ 日期：2026-10-31 ｜ 依据：`03/11` + `docs/reviews/reply-cx-v0.2.1.md`
> 位置：全部在 `web/rtc/`，**未改动 `web/` 下已有的 `index.html / app.js / style.css`**（避免和 D1 最小页面撞车，见文末"合并方式"）

---

## 1. 这次交付了什么

| 交付 | 位置 | 说明 |
|---|---|---|
| 通话台（可跑） | `web/rtc/index.html` + `src/` | 零依赖 ESM，浏览器直接开，无构建步骤 |
| 契约编码层 | `web/rtc/src/protocol.js` | 消息名/枚举/上行帧头（`0xA1`）/base64→Int16 的唯一实现 |
| 会话客户端 | `web/rtc/src/session.js` | 唯一 `new WebSocket()`；建会话→hello→分发→上行；4401 停重连 |
| 音频引擎 | `web/rtc/src/audio-engine.js` + `capture-worklet.js` | 唯一 `new AudioContext()`；16k/20ms/320 样本；Safari 精确降采样 |
| 本地 VAD | `web/rtc/src/vad-local.js` | b0=首帧（C2）、双讲门控、打断预触发 |
| 下行收敛层 | `web/rtc/src/playback.js` | `speech`（W1 Fake）/`pcm`（W2 真 TTS）同接口；duck 不丢缓冲（G11） |
| Mock + 冒烟 | `web/rtc/mock/` | 零依赖 WS Mock（手写帧编解码）+ 21 项断言 |
| golden 回放 | `web/rtc/test/golden-replay.mjs` | 用天命的真实抓包验前端解析（16 项断言） |
| W1 实现基线 | `docs/14-cx-w1-frontend-spec-v0.2.1.md` | 逐条消息"收到后做什么"的表 |
| 契约工具修复 | `contracts/validate.py` + `contracts/schemas/ws_transcript.v1.json` | 见 §3 |

---

## 2. 怎么验（30 秒）

```bash
# 1) 契约 schema
python contracts/validate.py contracts/fixtures

# 2) 前端 ↔ Mock 全链路（21 项）
FAST=1 node web/rtc/mock/mock-server.mjs      # 另一个终端；Windows: $env:FAST="1"; ...
node web/rtc/mock/smoke.mjs

# 3) 前端 ↔ 真实抓包（16 项，不需要后端）
node web/rtc/test/golden-replay.mjs

# 4) 人眼看 UI
#    http://127.0.0.1:8099/?app=http://127.0.0.1:8099   （mock 自带静态托管）
#    接真后端：.../?app=http://127.0.0.1:8081
```

---

## 3. 机器级确认结果（就是我此前承诺的 §5.2）

> **分支切分说明**：为避免动别人的地盘，下面这条契约工具修复单独放在 **`fix/contract-validate-cx`**（仅 `contracts/validate.py` + `contracts/schemas/ws_transcript.v1.json` + `docs/contracts-signoff.md §7`），便于天命单独 review / 回退。
> 本分支 `feat/web-rtc-cx` 是**纯新增**（`web/rtc/**` + `docs/14` + `docs/15`），不改任何既有文件，可 fast-forward 合并。

| 检查 | 结果 | 备注 |
|---|---|---|
| `python contracts/validate.py contracts/fixtures` | ❌→✅ **修复后 PASS：5 file(s), 90 document(s)** | 见下方说明 |
| `python -m pytest tests/test_contract.py -q` | ⛔ **未执行** | 本机沙箱装不上 pytest（`pip install` 写 site-packages/临时目录被拒，PyPI 可通但落盘 Permission denied）。**请天命或任意有环境的人在 main 上补跑一次**，我不把"21 passed"当自己的结论 |
| `python contracts/validate.py` 的 `ws_*.jsonl` | 修复前 **FAIL 43 条**（`missing required property 'schema_version'`） | 见下 |

### 修复说明（`ws_session_bargein.jsonl` 让 `make contract-test` 变红）

`ws_*.jsonl` 是**WS 收发实录**（`{"dir","frame"}` 逐帧），不是事件 envelope，但 `validate.py` 把所有 `*.jsonl` 都按 `event.v1.json` 校验，于是每行都报缺 `schema_version`。

- 新增 `contracts/schemas/ws_transcript.v1.json`：约束 `dir ∈ {c2s,s2c}`、`frame.type` 必填，允许扩展字段；
- `validate.py`：文件名以 `ws_` 开头时改用该 schema、跳过事件 `seq` 单调检查，并**要求至少有一条 `s2c session_ready`**（防止抓到半截）；
- 结果：`PASS: 5 file(s), 90 document(s) valid`。

> 如果天命更喜欢别的做法（比如把抓包挪到 `contracts/replays/`），删掉这两处改动即可，逻辑不依赖它。

---

## 4. 抓包暴露的 3 条"协议外消息"（请并入 03/11）

| 方向 | 消息 | 真实 payload | 影响 |
|---|---|---|---|
| S→C | `hello_ack` | `{session_id, protocol_version:"0.2", server_version:"0.2.0", transport:"ws_pcm"}` | 前端据它定 `transport`；契约里没有 → 不实现会静默丢 |
| S→C | `turn_end` | 与 `turn_result` 同 payload | 若只实现 `turn_result`，重复到达的 `turn_end` 会被当未知消息 |
| C→S | `text` | `{text}` | text 模式/无麦输入的唯一通道；前端已接到底部输入框 |

**另有一处形状偏差（重要）**：`turn_result` 真实是**嵌套**的

```json
{"turn_id":"t_0001","result":{"kind":"end","reply_id":"r_0001_1","outcome":"rescheduled",
  "slots":{"order_id":"A12345678"},"result":{"new_time":"2026-11-01T15:00","ticket":"pending"}}}
```

而 `03 §4.2` 的示意是扁平 `{outcome, slots, result}`。前端现在两种都归一化（`{outcome, ticket, newTime}`），但**建议在 03 里把真实形状写死**，别让下一个人再猜一次。

另外抓包里 `agent_text` 是**按句**下发（`sentence_id` 1..n），不是 token 增量——前端按"同 reply 逐句拼接、同句幂等"实现；这一点 03 的语义是清楚的，但示例容易误读。

---

## 5. 与现有 `web/` 的合并方式（等你/天命点头）

现在 `web/rtc/` 是独立可跑的一页，不动你们已有的 `web/index.html`（D1 最小页面）。
两种合并选一个：

1. **并存（推荐 W1）**：`/` 仍是现有 Demo，`/rtc/` 是通话台。`app` 静态托管无需改动（或加一条路由）。
2. **替换**：把 `web/rtc/index.html` 挪成 `web/index.html`，旧的 `app.js/style.css` 归档。等 W2 真 TTS + LiveKit 通了再换更安全。

我倾向 1：现在两边的"谁驱动 TTS"不同（现有页面走服务端 Fake，我的走 `speech`/`pcm` 双形态），并存能让评委/队友各取所需，也不阻塞别人。

---

## 6. 已知缺口（W1 余下 / 已补状态）

| # | 缺口 | 状态 | 说明 |
|---|---|---|---|
| 1 | 无麦克风时的**文件回灌**（`FileUplink`） | ✅ 已补（`a1bfaaf`） | wav → `OfflineAudioContext` 重采样 16k → 与麦克风**同形**的 20ms 帧泵；无麦自动回落，也可 `?uplink=file` 手动触发；`fixtures/demo_16k.wav` 自带 |
| 2 | `playback_first_frame` 在 speech 形态用 `onstart`（近似） | 🟡 保持 | speech 形态误差 ±30ms；W2 真 TTS 走 `pcm` 形态即为**实际调度时刻**（已被 `pcm-path.mjs` 断言钉住） |
| 3 | `pcm` 形态未与真后端跑过 | 🟡 半补 | 契约路径已用 stub AudioContext 单测（19 断言：解码/调度/首帧一次/clock/游标/duck 不丢缓冲）；仍需 SinCircle 一条真实 24k `audio_chunk` 样例做端到端 |
| 4 | E2E（浏览器内断言）未接 | 🟡 半补 | 协议层用 `golden-replay` + 自带 mock 兜住；`FileUplink` 就位后接 Playwright 只差网络恢复 |
| 5 | `pytest` 未在本机跑通 | ⛔ 环境限制 | 见 §3，请有环境的人补跑 |

### W1 收尾时的自检口径（三条命令全绿）

```bash
node web/rtc/test/golden-replay.mjs   # 16 ✅  真实抓包
node web/rtc/test/pcm-path.mjs        # 19 ✅  下行 PCM（W2 风险前置）
node web/rtc/test/uplink-file.mjs     # 10 ✅  无麦回放泵
node web/rtc/mock/smoke.mjs           # 21 ✅  （先 FAST=1 起 mock）
```

---

## 7. 承诺仍然有效

命名一律对齐 `03/04`（`session_id / turn_id / reply_id / sentence_id / call_id(工具) / release_id`），不私自定义"对方能接受但契约没有"的字段；`web/rtc/` 内不出现业务判断，阶段/状态只跟服务端 `state` 走。
