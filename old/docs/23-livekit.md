# 23 · LiveKit 接入

> 目标：用 LiveKit 替换当前 `ws_pcm` 媒体面。
> 当前状态：Server / Token / 浏览器入房 / 服务端 Orchestrator 音频桥已完成；主通话台尚未切换。

## 1. 已启动

LiveKit Server 跑在 Docker：

```bash
docker run -d --name aicall-livekit \
  -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  livekit/livekit-server:latest \
  --dev --bind 0.0.0.0 --node-ip 127.0.0.1
```

验证：

```text
http://127.0.0.1:7880/           → 200 OK
LiveKit Server 1.13.11
API Key: devkey
API Secret: secret
```

## 2. Token 接口

现有 app 已支持：

```http
POST /sessions/{session_id}/rtc
```

需要环境：

```text
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret
```

未配置时返回 `MEDIA_NOT_CONFIGURED`，前端应回落 `ws_pcm`。

## 3. 浏览器入房测试

启动测试 app（8085，fake ASR/TTS 仅用于 token）：

```text
http://127.0.0.1:8085/rtc/livekit-test.html
```

流程：

```text
POST /sessions
→ POST /sessions/{id}/rtc
→ LiveKit Room.connect(url, token)
→ 发布麦克风
```

页面显示 Room、Local participant、Participants、Remote audio。

## 4. 服务端媒体桥

已新增：

```text
scripts/livekit_bridge.py       # 入房、订阅麦克风、sherpa ASR、Orchestrator/Agent、Melo TTS 回推
scripts/livekit_publish_wav.py  # 测试用 16k WAV 发布器
```

桥接的是现有真实链路，不另写业务：

```text
LiveKit 音频
→ AudioStream(16k)
→ sherpa ASR
→ Orchestrator
→ PopCompetitionAgent / Compliance / ToolGateway
→ Melo TTS
→ LiveKit AudioSource
```

已实测：

```text
Agent 主动开场：
"您好，我是京东商家资质审核AI客服。本次通话会录音。请问您是这家店的负责人吗？"

测试音频：
LiveKit track → ASR "对我做了介绍哈什么我想说的是呢..."
→ Orchestrator/POP Agent
→ 追问身份 "请问您是这家店的负责人吗？"
→ Melo TTS 回推
```

## 5. 还没做的

- 前端主通话台切换 LiveKit（目前仅测试页）；
- 打断/AEC/48k 重采样策略；
- 多房间/并发；
- 正式环境 TLS/TURN；
- qwen TTS 资产未就位，桥当前用 Melo。

## 6. 责任

```text
Server / Token / 媒体桥：天命
客户端 SDK / 入房 / 播放：cx
ASR/TTS 音频适配：SinCircle
```

## 7. 默认策略

```text
默认仍是 ws_pcm 级联；
LiveKit 只作接入实验；
主通话台未切换；
伪双工 8096 不是 LiveKit 替代。
```
