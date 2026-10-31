# 07 · LLM 云 API + 本地语音混合方案

> Owner：天命（LLM Router/成本/降级）· SinCircle（本地 ASR/TTS）· pseudonym（LLM/Prompt）· 湖中日月（Judge/评测）
> 决策：**LLM 走云 API；ASR/TTS 本地（6×3090）；Embedding/Rerank/Judge 本地。**
> 一句话：**把外部依赖压缩到只剩 LLM 一个；语音链路留在本地，换低延迟、数据不出机、断网可播。**

---

## 0. 结论

这是当前约束下的最优组合：

| 维度 | 本方案 | 全云 | 全本地 |
|---|---|---|---|
| 语音延迟 | 低（本地 1 跳） | 中（多 2 跳公网） | 低 |
| LLM 质量 | 高（云） | 高 | 中（14B） |
| 外部依赖 | **1 个（LLM）** | 3 个 | 0 |
| 运维负担 | 中（GPU 语音） | 低 | 高（全部模型） |
| 数据边界 | 音频不出机，文本出云 | 全部出云 | 全部不出机 |
| 断网演示 | 语音可播，LLM 降级 | 基本不可用 | 可用 |
| 3 周可行性 | **高** | 高 | 中 |

**核心判断**：开发期用 3090 跑 FunASR/CosyVoice 做质量上限与对照；**交付镜像必须 CPU-only**（sherpa-onnx），因为现场跑在主办方设备上，见 [09](09-delivery-runtime.md)。LLM 走云，保持唯一公网依赖。

---

## 1. 组件分工

| 组件 | 主路径 | 降级 | 说明 |
|---|---|---|---|
| LLM | **云 API**（OpenAI 兼容） | 本地 14B vLLM → 模板话术 | 质量、FC、上下文；唯一公网依赖 |
| ASR | **本地 CPU（交付）/ FunASR（开发）** | 云 ASR（可选）→ 请用户重说 | 低延迟、热词、音频不出机 |
| TTS | **本地 CPU（交付）/ CosyVoice2（开发）** | 云 TTS（可选）→ 预置音频 | 自然度、首包、断网可播 |
| Embedding / Rerank | **本地 CPU（交付）/ GPU（开发）** | 云 | 便宜、低延迟 |
| Judge | **本地（离线，不进交付镜像）** | 云低价模型 | 离线评测，避免自偏好 |
| 开场白/高频话术 | **本地预合成** | — | 首字时延与断网兜底 |

> 交付镜像不含 3090；CPU VITS 的自然度低于 CosyVoice，需用预合成开场白、音色选择、短句来缓解。取舍见 09 第 4 节。

> 云 ASR/TTS 只作为「GPU 主机整体不可用」的应急适配器，**不是主路径**；没有预算就不接，靠预置音频和重试兜底。

---

## 2. 为什么语音留本地

1. **延迟**：ASR/TTS 各少一次公网往返，按 30–80ms/次算，直接省 60–160ms；对流式首包更关键。
2. **稳定**：赛场网络只影响 LLM 一个调用，不影响「能不能听见」。
3. **数据**：音频与转写留在本地，降低隐私与供应商条款风险。
4. **成本**：语音按分钟计费会随评测/RSI 放大；本地边际成本低。
5. **可控**：热词、音色、VAD 参数可针对赛场噪声调。

代价：GPU 主机从「可选」变成「关键路径」。见第 7 节。

---

## 3. LLM Provider Router

```text
Agent (pseudonym)
     │  OpenAI-compatible
     ▼
┌──────────────────────────┐
│ LLM Router (天命)        │
│ primary / secondary      │
│ timeout / retry / breaker│
│ cost meter / budget      │
└──────────────────────────┘
     │              │
     ▼              ▼
  LLM-1          LLM-2 / local 14B
```

### 3.1 配置

`config/providers.yaml`：

```yaml
llm:
  primary:
    name: deepseek
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
    timeout_ms: 3000
    max_retries: 1
  secondary:
    name: qwen
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen-plus
    api_key_env: DASHSCOPE_API_KEY
    timeout_ms: 3000
    max_retries: 1
budget:
  daily_cny: 100
  per_call_cny: 3
```

### 3.2 行为

| 场景 | 行为 |
|---|---|
| 429 限流 | 退避 1 次 → secondary |
| 5xx | 重试 1 次 → secondary |
| 首 token 超时 | 取消 → secondary；仍超时 → 模板/转人工 |
| 连续 5 次失败/30s | 熔断 60s，直接 secondary |
| 日预算超限 | 停用云 LLM，走本地 14B/模板，发 `BudgetExceeded` |
| 内容过滤 | 记录 `ProviderBlocked`，走本地合规改写/转人工 |

### 3.3 事件（并入 04）

`ProviderCalled`、`ProviderFirstToken`、`ProviderReturned`、`ProviderFailed`、`ProviderFailover`、`BudgetExceeded`。

> 没有这些事件，就无法区分「模型慢、网络慢、限流、内容过滤」。

---

## 4. LLM 选择：一天 bake-off

| 标准 | 权重 | 验证 |
|---|---|---|
| 首 token P95（同区域） | 高 | 20 次调用取 P50/P95 |
| Function Calling / JSON 稳定性 | 高 | 20 条工具用例 |
| 中文口语与话术质量 | 高 | 盲评 10 条 |
| 限流/配额 | 高 | 能否提额、并发上限 |
| 价格 | 中 | 单次通话成本 |
| 内容过滤误杀 | 中 | 禁语/正常话术各 10 条 |

候选：DeepSeek、阿里百炼（Qwen）、火山方舟、智谱 GLM、腾讯混元。优先 OpenAI 兼容，Router 不绑 SDK。

**不要在没有数据前锁死一家**；至少保留两家，主备切换。

---

## 5. 延迟预算（混合）

| 阶段 | P50 | P95 | 说明 |
|---|---|---|---|
| 上行 + 本地 ASR final | 120 | 250 | 流式；收尾只做 final |
| LLM TTFT（云，同区域） | 350 | 750 | 流式；短 prompt |
| 首句成型 | 100 | 200 | 首句 ≤20 字 |
| 合规首句 | 25 | 50 | 正则快路径 |
| 本地 TTS 首包 | 180 | 350 | 流式、连接预热 |
| 下行 + 播放 | 70 | 140 | 抖动缓冲 |
| **合计** | **845** | **1740** | 比全云 P95 低约 300ms |

**结论**：P50 有约 1.1s 余量，P95 有约 260ms 余量。仍需：

1. LLM 应用部署在**离供应商最近的地域**；不要跨地域。
2. 首句 ≤20 字；开场白预合成；高频话术缓存。
3. LLM/TTS 长连接、预热；避免冷启动。
4. 供应商按 **P95 TTFT** 选，不只看中位数。
5. 失败快速切换，不在重试上耗时间。

---

## 6. GPU 分配（LLM 上云后）

| GPU | 用途 | 显存 | 说明 |
|---|---|---|---|
| 0 | FunASR 流式 ASR | 4–6GB | 主路径 |
| 1 | CosyVoice2 流式 TTS | 8–12GB | 主路径 |
| 2 | 可选：14B AWQ 降级 / 第二 ASR | 14–18GB | LLM 断网兜底；不做则用于评测 |
| 3 | Judge 7B + bge-m3 + reranker | 10–14GB | 离线评测 |
| 4–5 | 批量评测 / RSI 回归 / 故障切换 | — | 与演示隔离 |

- 演示只开 GPU0/1 + app；GPU2 可选。
- 如果 W1 没时间搭 14B，GPU2 先空着或跑第二路 ASR，**不要把降级方案变成负担**。
- 3090 默认无 NVLink，不做实时 TP；14B 单卡即可。

---

## 7. 远程可达性：现在是 P0

语音本地后，**GPU 主机不可达 = 没有声音**。这比「LLM 挂了」严重得多。

| 方案 | 拓扑 | 结论 |
|---|---|---|
| A | app + LiveKit + 模型同机，公网只开一个入口 | **推荐** |
| B | GPU 在校园 LAN，app 同 LAN，反向代理 | 备选 |
| C | GPU 远程、app 在别处 | **不推荐**（音频跨公网） |

> ⚠️ **合规红线**：赛题书七(三)明确「现场严禁私自搭建外网穿透、代理」。演示环境不要用 frp/ngrok/SSH 隧道临时打通；优先公网 HTTPS 入口或本地备机，并赛前向组委会书面确认。

**本周必须做**：

1. 从赛场同类网络访问 GPU 主机：`ping`、WebSocket、UDP/TCP 443。
2. 让 cx 从校外网络跑一次真实通话（哪怕 Fake LLM），验证 NAT/防火墙。
3. 如果赛场连不上：考虑把演示迁移到本地机器/云主机，或准备云 ASR/TTS 应急适配器。
4. 预置「网络不稳定，先帮您转人工」音频在本地，任何情况下都能播。

---

## 8. 成本

只剩 LLM 一项：

- 单轮 system+history+output 约 2–4k tokens；5 轮约 10–20k；单次通话几毛到几元。
- 评测 100 条 × 5 轮：几十到几百元；RSI 按候选数线性增长。
- 设 `daily_cny` 与 `per_call_cny`；演示/评测 Key 分离；余额告警。
- 语音本地后，成本不再随音频分钟数增长。

---

## 9. 失败模式与降级

| 失败 | 影响 | 降级 |
|---|---|---|
| LLM 429/5xx/超时 | 回答慢/无回答 | secondary → 本地 14B → 模板 → 转人工 |
| LLM 内容过滤 | 回答被拒 | 本地合规改写 → 安全话术 → 转人工 |
| GPU 主机不可达 | **无 ASR/TTS** | 云 ASR/TTS（若接）→ 预置音频 → 转人工 |
| ASR 服务崩溃 | 听不见 | 重启 → 第二实例 → 请用户重说 |
| TTS 服务崩溃 | 说不出 | 重启 → 预置音频 → 文字+转人工 |
| 赛场断网 | LLM 不可用 | 本地 14B（若就绪）/模板；语音继续 |
| GPU 被占用/OOM | 语音异常 | 预留卡 + 监控 + 降级第二实例 |

**最低可用形态**：本地能录音、能播预置音频、能转人工；LLM 可有可无。

---

## 10. W1 任务

| 时间 | 任务 | Owner | 完成标准 |
|---|---|---|---|
| D0 | 确认云 LLM 账号/Key/配额/预算；至少 2 家 | 天命 | `providers.yaml` 可填 |
| D0 | 确认 GPU 主机位置/权限/Docker；从赛场网络测通 | 天命 + cx | 一张连通性结论 |
| D1 | LLM 主/备 + Router 骨架；20 次 TTFT | 天命 + pseudonym | P95 有数，切换可演示 |
| D1 | FunASR + CosyVoice2 起服务 | SinCircle | 各 1 条 wav 跑通 |
| D2 | LLM 接入文本 Agent；20 条工具用例 | pseudonym | JSON/FC ≥95% |
| D3 | 本地 ASR/TTS 接 WS 骨架 | SinCircle + 天命 + cx | 真实 3 轮有声音 |
| D4 | 故障演练：LLM 429/超时/断网 | 天命 + 湖中日月 | 每种有降级动作 |
| D5 | 成本报表 + 预算熔断 | 天命 | 单次/单日可查 |

> 本地 14B 是 P2：W2 有空再搭；没搭也不影响主路径，模板话术可兜底。

---

## 11. 待确认

1. 云 LLM 账号/Key/配额/预算？至少两家？
2. GPU 主机在哪、能否从赛场网络访问、是否独占、有 root/Docker？
3. 是否需要云 ASR/TTS 作为「GPU 整体不可用」的应急适配器？
4. 赛场网络是否允许出站 HTTPS/WSS？有无 4G/5G 备份？
