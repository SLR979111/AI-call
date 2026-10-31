# 06 · 本地推理与 GPU 方案（6×RTX 3090）

> **决策更新（D0 三次）：现场交付 Docker 跑在主办方设备（按 CPU-only 假设），ASR/TTS 交付用 CPU 轻量模型，不是 3090。本文档的 3090 方案只用于开发/评测/可选 GPU 加速；交付形态见 [09 交付运行时](09-delivery-runtime.md)。**
>
> Owner：天命（基础设施/vLLM）· SinCircle（ASR/TTS 服务）· 湖中日月（Judge/Embedding）
> 触发：团队确认有 6 张远程 RTX 3090（24GB）
> 结论：**算力足够支撑本地语音；LLM 上云后，GPU 主路径是 ASR/TTS + Judge/Embedding，远程可达性仍是最大风险。**

---

## 0. 一句话结论

6×3090 = 144GB 显存，足够支撑本地 ASR + TTS + Judge/Embedding + 评测/RSI；LLM 走云后，GPU 不再需要背 14B 主模型（除非做降级）。

但「远程」两个字带来两个新问题：**赛场能不能连上**、**应用和模型是否在同一网络**。这两个问题不解决，本地推理的收益会被网络延迟和演示故障吃掉。

因此：

1. **W1 仍以契约 + Fake 骨架为主**，GPU 作为并行轨道，D3 接第一个本地语音服务。
2. **应用与模型必须同机/同 LAN 部署**；只让浏览器到媒体网关走公网。
3. **LLM 走云、语音本地**；本地 14B 只作断网降级，不承诺 W1 可用。

---

## 1. 资产与约束

| 项 | 假设 | 影响 |
|---|---|---|
| GPU | 6×RTX 3090，24GB，Ampere | 单卡 14B AWQ 可行；32B 需 2 卡 TP，PCIe 下不划算 |
| NVLink | 默认无 | 不做跨卡 TP 的实时推理；32B 只用于离线评测 |
| 精度 | FP16 / AWQ 为主 | 不要假设 BF16 有加速；vLLM 显式 `--dtype float16` |
| 位置 | 远程（校内外未知） | 必须测赛场到 GPU 主机的 RTT/可达性 |
| 权限 | 假设有 root + Docker + NVIDIA Toolkit | 若没有，W1 就要提权，否则无法容器化 |
| 共享 | 未知，可能多人共用 | 需要 GPU 配额/排队，避免演示时被占满 |
| 存储 | 未知 | 模型权重约 30–60GB；需预留 ≥100GB |
| 电力/散热 | 3090 功耗高 | 不要 6 卡满负荷跑；演示时只开 3–4 卡 |

**第一件事不是装模型，是确认**：

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
ping <GPU_HOST>
curl -w '%{time_connect} %{time_total}\n' https://<GPU_HOST>/healthz
```

把结果写进 `docs/reference/gpu-baseline.md`，作为后续延迟对比的基线。

---

## 2. GPU 分配（推荐）

| GPU | 用途 | 模型/服务 | 显存预算 | 备注 |
|---|---|---|---|---|
| 0 | ASR | FunASR streaming（Paraformer + fsmn-vad + punc） | 4–6 GB | 流式；热词；16k |
| 1 | TTS | CosyVoice2 streaming | 8–12 GB | 24k；首包优先 |
| 2 | LLM 降级（可选） | Qwen2.5/3 14B 级 AWQ，vLLM | 14–18 GB | 主 LLM 在云；本卡用于断网/评测 |
| 3 | Judge + RAG | Qwen2.5-7B Judge + bge-m3 + bge-reranker | 10–14 GB | 离线评测用，不上关键路径 |
| 4 | 备用/评测 | 第二路 LLM 或批量 eval | — | RSI 回归、并发评测 |
| 5 | 备用/实验 | 32B 实验、故障切换 | — | 不承诺演示可用 |

**为什么不把 ASR/TTS 放一张卡**：省一张卡，但 OOM/抢占会让语音链路整体挂掉；6 张卡没必要省。
**为什么 Judge 单独一张**：用主模型自评会有自偏好，RSI 的对比会失真；7B Judge + 人工抽检更可信。
**为什么本地 LLM 用 14B 单卡（仅降级）**：3090 无 NVLink，32B TP=2 的跨卡通信会拉高 TTFT；14B AWQ 单卡足够做断网兜底。主路径 LLM 在云，见 07。

> 如果 W2 评测证明 14B 在意图/话术上明显不够，再考虑 32B AWQ TP=2，但只用于离线候选评估，不进实时主路径。

---

## 3. 模型与服务选型

### 3.1 ASR：FunASR 流式

- 组成：Paraformer-streaming + fsmn-vad + ct-punc；领域热词表。
- 服务：FunASR runtime（WebSocket）或薄 FastAPI 包装；端口 `10095`。
- 关键指标：`final` 延迟 ≤150ms；CER ≤8%；实体 ≥95%。
- 注意：电话窄带/强噪下退化，必须用赛场噪声源做测试；必要时前端降噪 + 热词补偿。

### 3.2 TTS：CosyVoice2 流式

- 服务：CosyVoice FastAPI/gRPC；端口 `9880`；24kHz PCM。
- 关键指标：首包 ≤200ms；MOS ≥4.0；可懂度 ≥98%。
- 策略：开场白、高频话术**预合成缓存**；TTS 连接预热；按句合成，不等整段。

### 3.3 LLM：vLLM + 14B AWQ（仅降级）

```bash
vllm serve <model> \
  --host 0.0.0.0 --port 8000 \
  --quantization awq --dtype float16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.85 \
  --enable-prefix-caching \
  --max-num-seqs 8
```

- OpenAI 兼容接口，Agent 侧用标准 client；不要绑死 vLLM SDK。
- **主路径不启动本服务**；仅在云 LLM 不可用、且需要断网演示时启用。
- 关键指标：TTFT P50 ≤500ms、P95 ≤900ms（本地热态）；tokens/s 记录。
- 结构化输出：JSON Schema / guided decoding；失败降级纯文本。
- 温度 0.2–0.4；system prompt 精简；prefix caching 对多轮有效。

### 3.4 Embedding / Rerank / Judge

| 能力 | 模型 | 部署 | 用途 |
|---|---|---|---|
| Embedding | bge-m3 | GPU3 或 CPU | 知识库检索 |
| Rerank | bge-reranker-v2-m3 | GPU3 | Top-K 重排 |
| Judge | Qwen2.5-7B-Instruct | GPU3 | 离线评分，不上实时链路 |
| 参考转写 | Whisper large-v3 | GPU4（离线） | 评测集人工参考的辅助，不做线上 ASR |

> Judge 只用于评测/RSI，**不进入对话关键路径**；Judge 与主模型不同源，降低自偏好。

---

## 4. 服务拓扑与端口

```text
┌──────────────── GPU 主机 / 同 LAN ────────────────┐
│  app       FastAPI + Orchestrator        :8080    │
│  livekit   媒体服务器                    :7880    │
│  vllm      14B AWQ                       :8000    │
│  funasr    流式 ASR                      :10095   │
│  cosyvoice TTS                           :9880    │
│  embed/rerank/judge                      :8081+   │
│  web       静态前端                      :3000    │
└───────────────────────────────────────────────────┘
        ▲ HTTPS/WSS（仅这一个入口对公网）
        │
   浏览器 / 赛场网络
```

**硬规则**：

- `app` 与模型服务**同机或同 LAN**；禁止 app 在公网、模型在另一公网。
- 只暴露 `web`/`app`/`livekit` 的必要端口；vLLM/FunASR/CosyVoice 不直接对公网。
- 开发用 WireGuard/SSH 隧道（仅开发环境）；演示用公网 HTTPS + token，**禁止在现场临时搭隧道**。
- Docker Compose 用 profile 区分：`dev`（Fake）、`cloud`（云 API）、`gpu`（全本地）。

---

## 5. 远程可达性：本周必须验证的三条路

| 方案 | 拓扑 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| A | GPU 主机 = 应用主机，公网入口 | 内部 RTT 最低；只维护一套 | 需要公网 IP/域名/TLS；赛场网络要能出站 | **推荐** |
| B | GPU 主机在校园 LAN，应用同 LAN，公网只做反向代理 | 安全；延迟低 | 依赖学校网络策略；赛场可能连不上 | 备选 |
| C | GPU 主机远程，app 在本地/云 | 灵活 | app→模型跨公网，TTFT 抖动；2s 门槛风险 | **不推荐** |

> ⚠️ **合规红线**：赛题书七(三)明确「现场严禁私自搭建外网穿透、代理」。用 frp/ngrok/SSH 隧道把远程 GPU 暴露到赛场，可能被判定违规。优先方案 A（公网 HTTPS 入口）或本地备机；赛前向组委会书面确认。

**本周验证**：

1. 从「赛场同类型网络」（校外 4G/5G、酒店 Wi-Fi、有线）访问 GPU 主机：`ping`、`curl` TTFT、WebSocket 握手。
2. 记录 RTT：<30ms 可接受；30–80ms 需评估；>80ms 考虑把 app 迁到 GPU 主机或换云。
3. 测试 UDP 是否被封：WebRTC 需要 TURN/TCP 443 兜底。
4. 让 cx 用浏览器从校外网络跑一次真实通话（哪怕 Fake Agent），验证 NAT/防火墙。

> 这是当前仅次于契约的第二大风险：**你无法把 6 张 3090 搬到赛场**。如果赛场网络不可控，必须有「本地小模型/云 API/预置话术」三级降级。

---

## 6. 容量与并发

| 场景 | 并发 | 资源 | 说明 |
|---|---|---|---|
| 现场演示 | 1 路通话 | GPU0/1/2 | 只开 3 卡，降低故障面 |
| 联调 | 2–3 路 | GPU0/1/2 + GPU4 | vLLM continuous batching |
| 批量评测 | 4–8 路文本 | GPU2 + GPU4 | 不开 TTS/ASR，省资源 |
| RSI 回归 | 夜间批量 | GPU4/5 | 与演示隔离，避免抢占 |

- 14B AWQ + 8k context 单卡可支撑个位数并发；竞赛不需要高并发。
- 评测时把 `max-num-seqs` 调低，保证单请求延迟；不要用吞吐换 TTFT。
- 演示前 `nvidia-smi` 检查显存；禁止其他任务占用 GPU0/1/2。

---

## 7. 延迟预期与验证

| 阶段 | 本地预期 P50 | 验证方式 |
|---|---|---|
| ASR final | 100–200ms | `ASRFinal.latency_ms` |
| LLM TTFT | 250–600ms（热态） | `AgentFirstToken.latency_ms` |
| 首句成型 | 100–200ms | `TurnLatency.t4→t5` |
| TTS 首包 | 150–300ms | `TTSFirstChunk.latency_ms` |
| 端到端 | 1.0–1.6s | `TurnLatency.e2e_ms` |

**冷启动是最大陷阱**：模型首次加载/首次请求可能 5–30s。演示前必须：

- 预加载模型 + 跑 3 轮 warmup；
- 预合成开场白；
- 保持 vLLM/TTS 长连接；
- 演示脚本第一句用预置音频，不依赖冷启动。

---

## 8. 降级与故障

| 故障 | 检测 | 降级 |
|---|---|---|
| GPU 主机不可达 | 健康检查 3 次失败 | 切云 API（LLM/TTS）或预置话术 |
| vLLM OOM | 启动/请求失败 | 降 `max-model-len`/`gpu-memory-utilization`；切 7B |
| ASR 服务挂 | 3s 无 partial | 切云 ASR / 提示重说 |
| TTS 服务挂 | 首包 >1.5s | 切云 TTS / 预置句 |
| GPU 被他人占用 | `nvidia-smi` 显存不足 | 预留卡 + 预约；演示前锁定 |
| 网络抖动 | RTT/丢包阈值 | TURN/TCP；降码率；重连 |

云 API 适配器不是「备用方案」的临时补丁，而是**必须从一开始就保留的接口实现**；否则断网时无法降级。

---

## 9. W1–W2 任务（并行轨道，不阻塞骨架）

| 时间 | 任务 | Owner | 完成标准 |
|---|---|---|---|
| D1 | 确认 GPU 主机位置/权限/驱动/Docker；跑 baseline | 天命 | `nvidia-smi` + 容器 GPU 透传成功 |
| D1 | 从校外网络测 RTT/端口/UDP | 天命 + cx | 三条路结论写进 `gpu-baseline.md` |
| D2 | vLLM 14B 起服务；OpenAI API smoke；TTFT | 天命 | 单请求 TTFT <800ms（热态） |
| D3 | FunASR + CosyVoice2 起服务；适配器 | SinCircle | 各 1 条 wav 跑通；首包有数 |
| D4 | 预热脚本、健康检查、compose `gpu` profile | 天命 | `docker compose --profile gpu up` 一键起 |
| D5 | 本地语音链路联调（可与 WS 骨架并行） | 天命 + SinCircle + cx | 真实说一句→本地模型→听到回答 |
| W2 | 本地为主、云为降级；同测试集 bake-off | 全员 | CER/TTFT/首包/MOS 四项达标 |

**注意**：D5 的「本地语音链路」是并行验证，不替代 W1 的 Fake 骨架 Gate。两条线都要绿。

---

## 10. 风险登记

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | 赛场连不上远程 GPU | 中 | 致命 | 本周实测；准备云 API/本地小模型/预置话术三级降级 |
| 2 | GPU 主机被共享/抢占 | 中 | 高 | 预约 + 预留卡 + 演示前锁定；监控显存 |
| 3 | 3090 无 BF16/NVLink | 高 | 中 | FP16/AWQ；14B 单卡；不做实时 TP |
| 4 | 模型权重下载慢/磁盘不足 | 中 | 中 | 提前下载；≥100GB；本地缓存 |
| 5 | CosyVoice 首包不达标 | 中 | 中 | 流式调参；预合成；云 TTS 备选 |
| 6 | 噪声下 ASR 退化 | 高 | 高 | 赛场噪声回放；热词；前端降噪；云 ASR 对照 |
| 7 | Judge 自偏好导致 RSI 失真 | 中 | 中 | 7B 独立 Judge + 人工抽检 + 双盲 |
| 8 | 电力/散热导致降频 | 低 | 中 | 只开必要卡；监控温度；不要 6 卡满载 |

---

## 11. 待确认

1. GPU 主机在哪：校园 LAN、实验室、还是公有云？能否从赛场公网访问？
2. 是否独占？是否有 root / Docker / NVIDIA Toolkit？磁盘多大？
3. 能否把 `app` + LiveKit 部署到 GPU 主机上（方案 A）？
4. 模型许可证是否允许竞赛使用（Qwen/FunASR/CosyVoice 逐项登记）？
5. 是否有第二台本地机器（哪怕 1 张 3090/4090）作为赛场降级？

> 这 5 个答案决定 W2 是「全本地」还是「本地 + 云混合」。在答案明确前，代码层面只依赖适配器接口，不依赖具体部署。
