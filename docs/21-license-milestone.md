# 21 · License Compliance Milestone

> 目标：把许可证缺口收敛到可交付状态，避免“代码能跑、资产不能交”。
> 完整清单：[THIRD_PARTY.md](../THIRD_PARTY.md)
> 行动清单：[docs/20-license-checklist.md](20-license-checklist.md)
> 许可证原文：[licenses/](../licenses/)

## 对外版 milestone 描述（给所有人看）

```text
Title: M1 · 本地语音 Demo 到可用 AI 客服
State: open

已完成：
本地真实语音链路已经跑通。用户可以通过浏览器麦克风说话，系统完成语音识别、生成回复并语音播报；页面可以同时看到转写、最新回复和后端运行状态。

当前阶段：
正在把“规则回复”升级为“本地 AI 回复”，并修复确认、拒绝、听不清等常见对话。当前版本已经能完成基本对话，但仍有三个明显问题：不主动开场、响应偏慢、语音识别在嘈杂/短句场景不稳定。

下一阶段：
提升语音识别和对话质量，接入正式实时媒体通道，完成真机三轮验收、评测指标、许可证和交付包。

完成标准：
本地 AI 客服可以稳定完成三轮真实语音对话；常见确认/拒绝不再复读；识别、延迟和许可证达到可交付标准。
```

## 阶段状态（内部版）

### 已完成：M0 本地语音 Demo 基线

- `8091` 比赛入口：`scripts/serve_pop_contest.py`，真实 sherpa ASR + Melo TTS + POP 六阶段规则 Agent。
- `8081` 开发入口：`uvicorn app.main`，同一套真实语音链路。
- `/rtc` 通话台：实时字幕、最新自主回复、后端事件面板。
- 已完成真实麦克风、ASR、TTS、打断和三/六阶段联调。
- 已完成 Qwen3-4B 本地端点验收：JSON、tool_calls、中文对话可用。

### 当前：M1 本地 LLM 接入与稳定性

- `8084`：Qwen3-4B + `LLMAgent`，每个用户回合调用 LLM。
- 已修复“可以”被误判为 unclear、流程停在 `IDENTITY_CONFIRM` 的问题。
- 当前瓶颈：4B 单轮约 5–14 秒、thinking 开销、并发排队；只适合离线/降级。
- 许可证 P0 并行收敛：ASR 权重、Melo 数据授权、Freeze AUP、NVIDIA/LGPL/GCC。

### 下一阶段：M2 真双工与 WebRTC

- WebRTC/LiveKit + AEC/TURN，替换 `ws_pcm`。
- 真双工模型通道：16–24GB 或云端 Realtime；8GB 本地不作为主链路。
- 官方评测/时延口径、3 轮真机验收、交付包和许可证闭环。

## Milestone 创建文案（复制到 GitHub）

```text
Title: M1 · 本地语音 Demo 到 LLM 接入
State: open
Description:
已完成 M0：8091/8081 真实语音 Demo、POP 六阶段规则 Agent、/rtc 后端事件面板、Qwen3-4B 本地端点验收。
当前 M1：8084 LLM 接入与稳定性；已修复“可以”误判导致的状态复读；4B 延迟/并发仍是瓶颈；许可证 P0 并行收敛。
下一阶段 M2：WebRTC/LiveKit + AEC、真双工模型通道、官方评测/时延与交付包。
Exit: M1 的 LLM 路径可稳定演示；M2 入口和真双工通道准备完成；P0 许可证有结论。
```

## Milestone

```text
Title: License Compliance & Submission Readiness
State: open
Scope: ASR/模型权重、Docker/系统库、LLM 条款、评测数据
Exit: 所有 P0 有结论；交付包带 LICENSE/NOTICE；ASR 权重不再分发未知许可内容
```

## Issues

| # | Issue | Owner | 验收 |
|---|---|---|---|
| L1 | 确认 sherpa ASR 权重许可或替换 | SinCircle | 有书面许可，或替换为可再分发权重 |
| L2 | 补 Melo 训练数据/说话人授权 | SinCircle | 有数据/音色授权说明 |
| L3 | 审 Freeze-Omni 代码许可 + 腾讯 AUP | SinCircle + 天命接口 | 有结论；不满足则不交付 |
| L4 | Moshi 权重 CC-BY 署名/修改说明 | SinCircle | 若启用，NOTICE 完整 |
| L5 | Docker 不烘入未授权 ASR 权重 | 天命 + SinCircle | 镜像检查通过 |
| L6 | NVIDIA CUDA EULA 再分发确认 | 天命 | 有赛事/法务结论 |
| L7 | libsndfile LGPL / GCC libgomp1 随包 | 天命 | `licenses/` 完整，Docker 附文本 |
| L8 | LiveKit Server/coturn 版本+许可 | 天命 | 锁版本，LICENSE/NOTICE |
| L9 | LiveKit 客户端 SDK/aiortc | cx | 锁版本，安装说明 |
| L10 | LLM/云端 Realtime 服务条款与数据边界 | 天命 + pseudonym | Provider 台账完整 |
| L11 | 评测/Judge/RSI 数据许可 | 湖中日月 | 数据来源与合成声明 |
| L12 | 依赖锁版本与 LICENSE/NOTICE 清单 | 天命 | `pip freeze`/锁文件、NOTICE 随包 |

## GitHub 创建（需登录）

本机当前没有 `gh`，也没有 `GITHUB_TOKEN`，所以尚未创建 GitHub Milestone。登录后执行：

```bash
gh auth login
gh api repos/SLR979111/AI-call/milestones \
  -f title='License Compliance & Submission Readiness' \
  -f state=open \
  -f description='ASR/模型权重、Docker/系统库、LLM 条款、评测数据'
```

创建 issue 时使用 `--milestone 'License Compliance & Submission Readiness'`。

## 交付门槛

```text
P0 全部有结论
ASR 权重许可或替换完成
Docker 不含未授权权重
licenses/ + THIRD_PARTY.md + docs/20 随包
```
