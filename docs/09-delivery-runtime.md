# 09 · 交付运行时：在主办方设备上跑 Docker

> 约束：现场用**交付的 Docker** 在**主办方设备**上运行；设备型号、GPU、网络、浏览器位置暂不确定。
> 结论：**交付物必须按「CPU-only、x86_64、8C/16GB、Docker Compose、可离线启动」设计。6×3090 只用于开发/评测，不进交付路径。**
> 这是对 06/07 的修正：本地语音不再假设 3090，而是 CPU 可跑的轻量模型；GPU 只作为可选加速。

---

## 0. 一句话结论

现场不是「我们的机器 + 我们的 GPU」，而是「评委的机器 + 我们的镜像」。所以：

1. **不能依赖 6×3090、不能依赖隧道、不能依赖运行时下载模型。**
2. ASR/TTS 必须能在 **CPU** 上跑；LLM 仍走云 API。
3. 镜像要自带模型、健康检查、一键启动、失败降级；启动到可演示 ≤10 分钟。
4. 浏览器麦克风需要安全上下文：**`http://localhost` 可以，`http://192.168.x.x` 不行**；这决定演示怎么打开页面。

---

## 1. 交付边界

| 在交付物内 | 不在交付物内 |
|---|---|
| Dockerfile / docker-compose / 预构建镜像 | 6×3090 远程集群 |
| CPU ASR/TTS/VAD 模型（内置或校验下载） | 训练/微调 |
| web + app + LiveKit + coturn + SQLite | frp/ngrok/SSH 隧道 |
| `.env.example`、README、健康检查、冒烟脚本 | 真实号码、真实 PII |
| 评测/RSI 的报告与日志样本 | 大规模并发外呼 |

> 赛题书七(三)：「现场严禁私自搭建外网穿透、代理」。交付镜像里**不要**放隧道工具；演示只走主办方网络与正常端口。

---

## 2. 目标环境假设（最坏情况）

| 项 | 假设 | 影响 |
|---|---|---|
| CPU | x86_64，8 核 | ASR/TTS 用 ONNX Runtime CPU；避免重模型 |
| 内存 | 16GB | 全栈峰值 <6GB，留余量 |
| 磁盘 | ≥20GB 可用 | 镜像 + 模型 + 日志 |
| GPU | **无**（有则可选加速） | 默认 CPU；`gpu` profile 可选 |
| OS | Linux（Ubuntu 22.04 优先） | Docker Desktop 也可，但网络/端口差异要测 |
| Docker | Docker Engine + Compose v2 | 一键启动 |
| 网络 | 可出站 HTTPS（不确定） | LLM 云；断网要能降级 |
| 浏览器 | 与 Docker 同机，或局域网另一台 | 决定 `localhost` vs HTTPS |
| 架构 | amd64；若 arm64 需多架构镜像 | 构建时确认 |

**第一件事**：向主办方确认 CPU/内存/OS/架构/Docker 版本/是否允许出站/浏览器在哪台机器。确认前按最坏情况做。

---

## 3. 运行时组件（CPU profile）

| 服务 | 作用 | 端口 | 内存预算 | 备注 |
|---|---|---|---|---|
| `web` | 静态前端（由 app 托管） | 8081（宿主） | <100MB | 容器内 app 8080，compose 映射 8081:8080 |
| `app` | FastAPI + Orchestrator/Agent/Tool/Compliance | 8081（宿主） | 300–500MB | 单进程 asyncio |
| `livekit` | WebRTC 媒体 | 7880/7881/7882 udp | 200–400MB | 自带 TURN 可选 |
| `coturn` | TURN 兜底 | 3478/5349 | <100MB | 网络严格时必需 |
| `asr` | sherpa-onnx 流式 ASR | 10095 | 500MB–1GB | CPU |
| `tts` | sherpa-onnx VITS TTS | 9880 | 500MB–1GB | CPU |
| `llm` | 云 API（无容器） | — | — | 唯一外部依赖 |

总内存 <4GB；8 核足够。演示时只开必要服务。

---

## 4. 语音栈：CPU 版

### 4.1 ASR：sherpa-onnx 流式

- 候选：sherpa-onnx 中文流式 Zipformer / Paraformer（ONNX，CPU）。
- 能力：16k、流式 partial、端点、热词（视模型支持）。
- 目标：final ≤250ms（CPU）；CER 以实际测试为准。
- 备选：FunASR CPU（更重）；云端 ASR（仅应急）。
- 适配器：`AsrStream` 不变，新增 `SherpaCpuAsr` 实现；3090 上的 FunASR 只用于开发/对照。

### 4.2 TTS：sherpa-onnx VITS

- 候选：sherpa-onnx 中文 VITS 模型（ONNX，CPU）。
- 目标：首包 ≤300ms（短句）；可懂度优先。
- 现实：自然度不如 CosyVoice/云 TTS；**这是交付形态的硬取舍**。
- 缓解：开场白/高频话术预合成；选最自然的中文音色；句短；语速适中。
- 备选：云 TTS（仅当允许第二个 API 且网络可用）；3090 CosyVoice 只用于开发/对照。

### 4.3 VAD

- Silero VAD ONNX（CPU）或 WebRTC VAD；端点参数可配。
- 目标：端点 P50 ≤300ms；噪声误触发 ≤1/min。

### 4.4 质量取舍（需要拍板）

| 方案 | 自然度 | 延迟 | 依赖 | 工作量 |
|---|---|---|---|---|
| CPU VITS（默认） | 中 | 低 | 无 | 低 |
| 云 TTS | 高 | 中 | 网络+费用 | 低 |
| 3090 CosyVoice | 高 | 低 | GPU（现场没有） | 中 |

> 按你「只有 LLM 用接口」的约束，默认 CPU VITS；如果评委对自然度权重高，建议把 TTS 也放云作为可选 profile。这个取舍需要你拍板。

---

## 5. 模型打包

| 方式 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| 烘进镜像 | 一键、无下载失败 | 镜像大（+1–2GB） | **默认** |
| 启动时下载 | 镜像小 | 现场网络/超时风险 | 不推荐 |
| 挂载 `models/` | 灵活 | 评委要多一步 | 备选 |

要求：

- 模型文件带 `sha256`，构建时校验。
- 许可证逐项登记到 `THIRD_PARTY.md`（sherpa-onnx、模型、onnxruntime、LiveKit、coturn 等）。
- 构建多阶段：编译/下载在 builder，运行镜像只留必要文件。
- 镜像 tag 固定，不用 `latest`；提交镜像 tar + Dockerfile + compose。

---

## 6. Docker Compose 设计

```bash
# 默认：CPU + 云 LLM
docker compose --profile cpu up -d

# 可选：有 GPU 时
docker compose --profile gpu up -d

# 开发：Fake 全链路
docker compose --profile dev up -d
```

要求：

- `depends_on: condition: service_healthy`；每个服务有 healthcheck。
- `app` 启动时检查：ASR/TTS 模型存在、LLM key 已配置、LiveKit 可达；失败则进入 `DEGRADED`，但 Web 仍可打开。
- 数据卷：`./data`（SQLite/JSONL）、`./models`（可选）、`./logs`。
- 端口可配置：`WEB_PORT`、`APP_PORT`、`LIVEKIT_PORT`。
- 时区 `Asia/Shanghai`；日志 UTF-8。
- 不在镜像里放 `.env`/密钥；用 `--env-file .env`。

---

## 7. 网络、HTTPS 与麦克风（最容易翻车）

浏览器 `getUserMedia` 要求安全上下文：

| 访问方式 | 麦克风 | 结论 |
|---|---|---|
| `http://localhost:8081` | ✅ | 浏览器与 Docker 同机时首选 |
| `http://127.0.0.1:8081` | ✅ | 同上 |
| `http://192.168.x.x:8081` | ❌ | 局域网另一台会被拒 |
| `https://...`（自签） | ✅（接受警告后） | 评委设备访问时用 |

**建议**：

1. 默认让评委在**运行 Docker 的同一台机器**上打开 `http://localhost:8081`。
2. 若必须用另一台设备：提供 Caddy/Nginx 自签 HTTPS + 一页说明；提前测浏览器是否接受。
3. WebRTC：同机用 host 候选；跨机用 TURN；`LIVEKIT_URL` 按 `ws://`/`wss://` 配置。
4. Docker NAT：Linux 可用 host network 简化；Docker Desktop 需端口映射 + TURN。
5. 不做隧道；所有访问走主办方网络。

---

## 8. LLM 配置与离线降级

`.env.example`：

```bash
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
LLM_TIMEOUT_MS=3000
LLM_DAILY_BUDGET_CNY=100
```

- 密钥只在服务端；不进镜像、不进前端、不进日志。
- 无 key / 无网络：`app` 进入 `DEGRADED`，用 **脚本化 Flow Pack + 预置话术/音频** 完成演示，不白屏。
- 演示前跑 `scripts/healthcheck.sh` 和 `scripts/smoke.sh`。
- 预算熔断：超限切降级，不静默烧钱。

---

## 9. 启动与验收

```bash
docker load -i aicall-images.tar
cp .env.example .env && vi .env
docker compose --profile cpu up -d
./scripts/healthcheck.sh
./scripts/smoke.sh          # 文本 1 轮 + 语音 1 轮（可选）
open http://localhost:8081
```

| 验收 | 标准 |
|---|---|
| 干净机启动 | ≤10 分钟（含加载镜像） |
| 健康检查 | 全部 `healthy` |
| 文本闭环 | 4 轮，工具调用 + 回写 |
| 语音闭环 | 3 轮，首字 P50 有数 |
| 断网降级 | LLM 不可用时仍能播预置话术 |
| 重启 | `docker compose restart` 后 2 分钟内恢复 |
| 日志 | `data/sessions/.../events.jsonl` 可读 |

**每周在干净 VM（8C/16GB，无 GPU）演练一次**，不要在自己的开发机上验收。

---

## 10. 失败模式

| 失败 | 检测 | 降级 |
|---|---|---|
| 镜像加载失败/架构不符 | `docker compose up` 报错 | 提供 amd64/arm64 两版；README 写清 |
| 模型缺失 | healthcheck | 镜像内置；启动脚本校验 sha256 |
| LLM 无 key/断网 | 启动检查/首 token 超时 | 脚本化 Agent + 预置音频 |
| LiveKit UDP 被封 | ICE 失败 | TURN/TCP 443；同机 localhost |
| 浏览器麦克风被拒 | 前端提示 | 用 localhost/HTTPS；检查权限 |
| 端口冲突 | compose 报错 | `.env` 改端口 |
| CPU 太慢 | 首字 >3s | 降模型/短句/关闭非必要服务；至少保证能演示 |
| 磁盘满 | 日志增长 | 日志轮转/上限 |

---

## 11. W1–W3 任务

| 时间 | 任务 | Owner | 完成标准 |
|---|---|---|---|
| W1 | 确认主办方环境假设；搭 CPU compose 骨架 | 天命 | `docker compose --profile dev up` 可跑 |
| W1 | sherpa-onnx ASR/TTS 各跑通 1 条 wav | SinCircle | CPU 上 final/首包有数 |
| W2 | 模型烘进镜像；干净 VM 一键启动 | 天命 + SinCircle | 8C/16GB 无 GPU 可跑 |
| W2 | localhost/HTTPS + LiveKit/TURN 验证 | 天命 + cx | 同机/跨机各一次 |
| W2 | 断网/无 key 降级演练 | 天命 + 湖中日月 | 不白屏，能完成演示 |
| W3 | 镜像冻结、tar + Dockerfile + README | 天命 | 评委视角复现 ≤10min |

---

## 12. 待确认

1. 主办方设备的 CPU/内存/OS/架构/Docker 版本？是否有 GPU？
2. 浏览器跑在**同一台机器**还是另一台？这决定 `localhost` vs HTTPS。
3. 是否允许出站 HTTPS？LLM key 由谁配置、能否现场配置？
4. 交付形式：源码 + Dockerfile，还是预构建镜像 tar？有无大小限制？
5. 是否有测试 VM 可提前演练？
