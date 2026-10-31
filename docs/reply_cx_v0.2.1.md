# 给 cx 的回复（v0.2.1）

> 对象：cx · 前端 WebRTC
> 回复：`04/11` 契约评审 C1–C6
> 版本：v0.2.1 · 最新提交 `cbd31b3`（tag `e2e-ws`）· 契约点 `v0.2.1-cx-conditions`
> 日期：2026-10-31

---

cx，C1–C6 已处理，逐条答复如下。

## C1：`/rtc` 503 静默降级

接受。

- 服务端保留 `MEDIA_NOT_CONFIGURED(503)`。
- W1 **不要等 LiveKit**，直接走 `ws_pcm`。
- 前端收到 503 不弹错、不阻断会话，只在 UI 标“媒体未配置”。
- W1 的音频通路不依赖 LiveKit。

## C2：钉死 `b0`

接受，定义按你的写法：

```text
b0 = 本地 VAD 连续 3 帧（60ms）越限的第一帧 client_mono_ms
b3 = last_frame_played_mono_ms
stop_latency_ms = max(0, b3 - b0)
```

约束：

- `speech_started.client_mono_ms` 与 `local_muted` 必须同一 `performance.now()` 时间基。
- 代码已把 `b0_mono_ms` 透传进 `local_muted` 事件。
- 真实 VAD 首帧由你在 W2 接麦克风后实现；当前手动打断按钮只用于联调，不作为官方 `b3-b0` 指标。

## C3：误打断本地恢复

接受，不新增消息。

- `duck` 后 300ms 内本地 VAD 无持续语音，则本地 `unduck()` 并从缓冲续播。
- 同时上报：

```json
{"type":"client_event","name":"BargeInCancelledLocal","payload":{...},"client_mono_ms":12345.6}
```

- 若随后收到服务端 `BargeInConfirmed`，以服务端为准：保持静音、作废旧 `reply_id`。

## C4：`client_event` 白名单

已进代码，白名单：

```text
PlaybackCancelled | PlaybackUnderrun | NetStats | AecState |
ClockSync | BargeInCancelledLocal | VadMetrics
```

非白名单返回 `CLIENT_EVENT_NOT_ALLOWED`，不会静默丢，也不会让 04 的 schema CI 失败。

## C5：`APP_BASE=8081` + 完整 `ws_url`

接受。

- `POST /sessions` 现在直接返回完整 `ws_url`：`/ws/session/{id}?token=...`，**你直接用，不要再自己拼 token**。
- 宿主端口统一 **8081**；容器内仍是 8080，compose 映射 `8081:8080`。
- Makefile、README、02 §11、09 §3 已统一。
- `.env.example` 我这边受工作区凭据保护不能直接改，先放了同样内容的 [`config/app.example`](../config/app.example)，你合并进 `.env` 即可。

## C6：`tts_started` 游标语义

接受，已按你的约定：

- `tts_started.cursor = null`
- `tts_started.estimated = true`
- `tts_started.tts_alignment = false`
- `tts_sentence.estimated = true`
- `tts_completed.estimated = true`
- `TTSStarted` 事件带 `tts_alignment=false`

UI 标“估算”；D 侧用 `tts_alignment=false` 过滤游标误差，不把它归因成话术截断。

## 额外补的一条

服务端打断后现在会发 `turn_aborted`：

```json
{"type":"turn_aborted","turn_id":"t_0004","reason":"BARGE_IN"}
```

前端可以据此复位状态，不用只靠 `cancel` 推断。

## 验证与版本

- **20 tests passed**。
- WS 实测：`tts_started → tts_sentence → tts_completed → compliance → turn_result → session_ended`。
- `make e2e`：2 轮 + 打断 + 挂断 **PASS**。
- 最新提交 `cbd31b3`（tag `e2e-ws`）。
- C1–C6 契约点：`v0.2.1-cx-conditions`；之后只加了 `turn_aborted` 和 E2E 脚本。

```bash
git fetch --tags
git checkout cbd31b3

# 或只取 C1–C6 契约点
git checkout v0.2.1-cx-conditions
```

如果这 6 条无异议，请按 **v0.2.1** 复签，W1 前端可以开工。
