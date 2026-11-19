# 28 · 当前架构与分支状态

> 更新时间：2026-11-18
> master：`cbbdd80`（预测指标、状态时序/插话、音频预算已合并）
> 当前工作分支：`feature/text-audio-decoupling`（文本发布/音频准备解耦，待合）
> 说明：本文只描述“现在仓库里实际有什么”，不把目标态写成已上线。

## 1. 先看结论

当前 master **不是一条单链路**，而是两套实现并存：

```text
A. 旧正式链路：app/ + web/rtc
B. 新独立核心：new/
C. UI mock：web/rtc/mock（仅演示/联调）
```

新前端 Decision 流已经合入 master，但后端 `/ws/decision` 尚未实现；页面会自动探测并回落到旧 `/ws/session` 契约。

## 2. 分支

```text
本地分支：10
远端分支：15
远端未合并到 master：0
```

主要分支：

| 分支 | 状态 | 说明 |
|---|---|---|
| `master` | 当前 | `ee41ae3` |
| `origin/newfrontend` | 已合并 | 新前端 Decision 流 |
| `origin/codex/new-ai-call-g2` | 已合并 | `new/` 独立核心（PR #13） |
| `origin/feature/ui-enhancement` | 已合并 | UI 增强 + 多层缓存 + 延迟测试（PR #14） |
| 其他 feature/backup | 已合并或历史 | 不再作为当前主线 |

本地 `preview/new-ai-call-g2` 是独立 worktree 分支，落后远端一版，不影响 master。

## 3. 链路 A：旧正式链路 `app/` + `web/rtc`

```text
浏览器 /rtc
→ boot.js 探测 /ws/decision
   ├─ 可用：Decision 流
   └─ 不可用：回落 legacy
→ WS /ws/session + ws_pcm
→ Orchestrator（Session/Turn/取消/超时）
→ Agent / predictive v1
→ Compliance / Tool Gateway
→ TTS
→ PCM 播放
```

特点：

```text
- 后端入口在 app/main.py
- LiveKit 可选，失败回落 ws_pcm
- /predictive/v1 契约属于这条链路
- 当前 /ws/decision 未实现，所以实际会回落 legacy
```

## 4. 链路 B：新独立核心 `new/`

```text
浏览器 new/web
→ AudioWorklet 16k + AEC3/NS
→ WS /ws
→ Hearing：VAD + 预识别 + ASR
→ Brain：chat/completions + 预测候选
→ TTS /v1/tools/tts
→ 24k PCM
→ Player
```

接口：

```text
POST {base}/chat/completions
POST {base}/v1/tools/asr
POST {base}/v1/tools/tts
```

预测逻辑：

```text
当前回复开始播放
→ 复制任务/状态/对话快照
→ 5 个分支并行生成候选，每支最多 2 条
→ 候选文字一到立即发布（不等待音频）
→ 后台准备候选音频（可选 PREDICTION_AUDIO_LIMIT 限制前 N 条）
→ 下一轮规则优先选择，规则不确定时由模型选择
→ 有预生成音频就用音频，没有就回退普通语音合成
```

文本和音频已经解耦。语音模式下不再出现“音频没准备好就不发布文字”的等待；代价是选中未准备音频的候选会走普通语音合成。

合规与话术依据见 `docs/30-jd-compliance-and-call-script.md`。当前工单里的两段授权链、名称不一致、截图模糊是案例假设，不是京东对所有商家的统一规则。

特点：

```text
- 独立 FastAPI + WebSocket，不依赖旧 Orchestrator
- 默认端口 8088
- 需要 new/.env 的 DEEPSEEK_API_KEY
- 是独立原型，不是 8081 正式镜像
```

## 5. 端口

| 端口 | 用途 | 当前定位 |
|---|---|---|
| 8081 | 正式旧容器 | 旧镜像，待正式切换 |
| 8087 | 旧预测式实验 | 需手动启动 |
| 8088 | `new/` 独立核心 | 新原型入口 |
| 8091 | 比赛入口 | 未启动 |
| 8099 | 新前端 mock | 仅 UI/协议联调 |

## 6. 最新提交

```text
cbbdd80 预测音频预算实验数据
2f037fa 合并音频预算
00550ae 合并状态时序/插话保候选
cb49693 指标埋点与 /api/prediction/stats
e27ae05 状态时序 + 插话保候选
10effb4 可配置预测音频预算
d46938f Merge PR #20（缓存命中与扇出）
```

当前未提交分支 `feature/text-audio-decoupling` 在此基础上做文本发布/音频准备解耦。

## 6.1 开发解耦（仅 dev）

生产仍建议同源；开发时允许前后端分开：

```powershell
$env:DEV_CORS_ORIGINS = "http://127.0.0.1:5173"
# 启动后端 8087
.\scripts\dev-web-rtc.ps1 -Port 5173 -Backend http://127.0.0.1:8087
```

```text
前端：http://127.0.0.1:5173/?app=http://127.0.0.1:8087
后端：http://127.0.0.1:8087
```

约束：

```text
- DEV_CORS_ORIGINS 未设置 → 不开 CORS
- 生产同源默认不变
- WebSocket 跨域由浏览器直连；dev 暂不做 Origin 白名单
- 正式解耦仍需 token/Origin 校验/CORS 白名单
```

## 7. 现在最关键的缺口

```text
1. /ws/decision 后端未实现，新前端只能回落 legacy
2. new/ 尚未接正式 8081/Docker
3. 两条链路没有统一入口和统一延迟口径
4. AEC/降噪只在新核心与浏览器侧验证，正式链路仍未闭环
5. 正式 8081 仍是旧镜像
```

## 8. 建议下一步

```text
1. 决定主推 A（旧 app）还是 B（new/）
2. 若主推新前端：实现 /ws/decision 后端
3. 若主推 new/：补 .env、Docker、正式入口
4. 统一一条延迟链路：voice_to_playback / first_text / first_packet
5. 分支收敛，避免两套同时演进
```
