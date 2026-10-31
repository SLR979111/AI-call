# 13 · 项目现状与文件地图

> 更新时间：2026-10-31
> 仓库：`git@github.com:SLR979111/AI-call.git` ｜ 分支 `master`
> 最新提交：`5c2fd95`（Docker 验证）｜ 测试：**21 passed** ｜ 契约：**47/47**
> 结论：**Fake 全链路 + Docker 交付已跑通；真实 ASR/TTS/LLM/LiveKit 待接外部资源。**

---

## 0. 一页结论

| 项 | 状态 |
|---|---|
| 浏览器 WebRTC 页面 | ✅ 静态 Demo UI（Fake 语音，文本输入 + 浏览器 TTS） |
| Fake 全链路 | ✅ 3 轮、打断、挂断、事件回放 |
| 契约 / 事件 | ✅ 5 Schema、47 fixture 事件、`TurnLatency t0..t9` |
| Tool Gateway | ✅ 参数校验、幂等、超时、重试、审计 |
| 合规 | ✅ REWRITE / BLOCK / 事件 / 质检数据 |
| Registry / RSI | ✅ candidate→production→previous；bootstrap 门禁 |
| LLM | ✅ Router + LLMAgent（Mock 验证）；🟡 真实 Key 未接 |
| ASR/TTS | 🟡 适配器边界；真实 CPU 模型未接 |
| LiveKit | 🟡 Token/WS 消息；媒体服务未接 |
| Docker | ✅ 镜像 261MB，compose healthy，E2E PASS |
| 技术方案书 | 🟡 初稿（`docs/12`），待 W3 真机数据 |

---

## 1. 仓库结构

```text
aicall/
├── app/                     FastAPI 应用
│   ├── main.py              HTTP/WS 路由、lifespan
│   ├── orchestrator.py      Session/Turn/取消/超时/下行队列
│   ├── events.py            EventBus（JSONL + SQLite）
│   ├── compliance.py        合规守卫
│   ├── registry.py          版本注册中心
│   ├── reporting.py         自测报告
│   ├── agent/
│   │   ├── fake.py          规则 Agent（默认）
│   │   └── llm_agent.py     LLM JSON 决策 Agent
│   ├── llm/router.py        OpenAI 兼容主/备 Router
│   ├── tools/gateway.py     Tool Gateway + Mock 业务
│   ├── rag/knowledge.py     本地知识检索
│   ├── flow/pack.py         Flow Pack 加载/状态转移
│   ├── eval/runner.py       评测套件
│   ├── rsi/gate.py          RSI 对比与回归门禁
│   ├── media/livekit.py     LiveKit JWT
│   └── voice/factory.py     语音 Provider 状态
├── web/                     Demo UI（HTML/CSS/JS，无构建）
├── contracts/
│   ├── schemas/             5 个 JSON Schema
│   ├── fixtures/            事件 + WS 报文 golden fixture
│   └── validate.py
├── tests/                   21 个测试 + conftest async hook
├── scripts/                 smoke / e2e / mock_llm / report / rsi / replay
├── eval/cases/              评测用例
├── flowpacks/               业务流程配置
├── knowledge/               本地 FAQ
├── reports/                 自测报告样例
├── docs/                    架构、契约、提交物、方案书
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
├── THIRD_PARTY.md
├── config/app.example
└── .github/workflows/ci.yml
```

### 1.1 文档地图

| 文档 | 用途 |
|---|---|
| `docs/01` | 需求—指标—模块—负责人矩阵 |
| `docs/02` | 总体架构 v0.1 |
| `docs/03` | 接口契约 v0.1 |
| `docs/04` | 事件 Schema v0.1 |
| `docs/05` | W1 集成主线 |
| `docs/06` | 本地 GPU 备选方案 |
| `docs/07` | LLM 云 + 本地语音混合方案 |
| `docs/08` | 提交物清单、视频/答辩脚本 |
| `docs/09` | 交付 Docker 运行时 |
| `docs/10` | 总汇总（先看这个） |
| `docs/11` | v0.2 契约补丁（回应 cx） |
| `docs/12` | 技术方案书初稿 |
| `docs/13` | 本文（现状与文件地图） |
| `docs/reference/` | 赛题书 PDF/TXT、三周分工 |
| `docs/reviews/` | cx 契约评审、给 cx 的回复 |
| `docs/contracts-signoff.md` | 会签表 / v0.2.1 复签 / P1–P5 |
| `README.md` | 代码级运行、测试、Docker |
| `THIRD_PARTY.md` | 开源组件/模型清单 |

---

## 2. 已实现能力

### 2.1 实时语音骨架

- WS 音频帧接收（二进制占位）、`speech_started/ended`、`local_muted`、`playback_first_frame`。
- 客户端本地静音 + 服务端异步取消；`turn_id` 过滤幽灵音频。
- `tts_started/tts_sentence/tts_completed` 元数据；`tts_alignment=false` 标注估算。
- `turn_aborted` 复位消息；`transport_degraded/switched/failed` 降级协议。

### 2.2 编排与 Agent

- 双状态机：M3 运行时、M4 Flow Pack 业务。
- FakeAgent（规则）默认；`AGENT_PROVIDER=llm` 走 LLMAgent。
- LLM Router：主/备、超时、熔断、日预算；Mock 本地可跑。
- RAG：本地 FAQ 检索 + citation。
- Flow Pack：6 阶段、槽位、状态转移。

### 2.3 工具与合规

- Tool Gateway：JSON Schema 校验、幂等键、24h TTL、1 次重试、审计事件。
- 合规：禁语 REWRITE、隐私 BLOCK、转人工；事件 + WS `compliance` 消息。

### 2.4 数据与评测

- EventBus：JSONL append-only + SQLite 索引；`seq` 连续。
- `TurnLatency t0..t9`、`e2e_ms`、`e2e_official_proxy_ms`。
- `/eval/run` 套件、`/eval/sessions` headless；失败标签。
- `scripts/report.py` 生成自测报告；`reports/self-test-sample.md` 样例。

### 2.5 RSI 与版本

- Registry：candidate → production → previous；回滚。
- RSI Gate：bootstrap 95% CI、P50/P95 回退、合规不降。
- `/rsi/compare`、`/rsi/promote`；`scripts/rsi_compare.py`。
- 9 个 tag 可回滚（见 §4）。

### 2.6 交付

- Dockerfile：CPU-only、模型内置路径、pip 镜像参数。
- Compose：宿主 8081 → 容器 8080；健康检查；数据卷；无隧道。
- CI：pytest + 契约校验 + WS E2E。
- 一键命令：`docker-compose up -d --build` / `make e2e` / `make report`。

---

## 3. 验证证据

| 项 | 结果 |
|---|---|
| 单元/契约测试 | 21 passed |
| 事件 schema | 4 fixture / 47 事件通过 |
| WS E2E | 2 轮 + 打断 + 挂断 PASS |
| Docker | `aicall-app:local` 261MB，`Up (healthy)` |
| 容器评测 | 5/5，P50 719ms，P95 937ms（Fake） |
| 契约 fixture | `contracts/fixtures/ws_session_bargein.jsonl` 43 帧 |
| 自测报告 | `reports/self-test-sample.md` |

> 时延为 Fake Agent + 浏览器 TTS 的链路验证数据，**不是真实语音指标**；真实数据待 W3。

---

## 4. 版本与回滚点

| tag | 内容 |
|---|---|
| `p0-fake-skeleton` | Fake 全链路 |
| `w2-modules` | Registry/LLM/RAG/Flow/Eval |
| `w2-llm-agent` | LLMAgent + 媒体/语音边界 |
| `v0.2-contract-patch` | cx G1–G9 补丁 |
| `v0.2.1-cx-conditions` | cx C1–C6 |
| `v0.2.2-p5` | P5 WS golden fixture |
| `rsi-report` | RSI 门禁 + 报告 |
| `e2e-ws` | Headless E2E |
| `docker-verified` | Docker 构建/运行验证 |

当前 HEAD：`5c2fd95`（`master`，已推送 origin）。

---

## 5. 仍未完成（外部资源阻塞）

| 缺口 | 需要 |
|---|---|
| 真实 LLM | OpenAI 兼容 Key/Base/Model（用于 TTFT、FC、成本） |
| 真实 CPU ASR/TTS | sherpa-onnx + 中文流式/ VITS 模型，或允许安装 |
| LiveKit 媒体 | LiveKit Server URL/API Key/Secret |
| 前端真实麦克风/VAD | cx W2 接入 |
| 真机时延/成本 | W3 实测后替换方案书占位 |

---

## 6. 下一步

1. **给 LLM Key**：`AGENT_PROVIDER=llm` 接真实云，测 TTFT / Function Calling / 主备 / 成本。
2. **接 CPU ASR/TTS**：sherpa-onnx 模型进镜像，替换浏览器 `speechSynthesis`。
3. **接 LiveKit**：`/sessions/{id}/rtc` 返回真实 room/token，前端入房。
4. **W3**：真机 P50/P95、RSI 一组 A/B、技术方案书结果分析、提交包。

---

## 7. 本地开发备注

- 本机 `python` 依赖被 Windows 权限挡住，当前用 `.deps`（uv 缓存拼装）跑测试；`.deps` 已 gitignore，不进仓库。
- 本机 `make` 有 MSYS token 问题；直接跑等价命令，或用 Docker。
- Docker 构建时 Docker Hub 可能被代理拦截；用 README 的 DaoCloud 基础镜像 + 阿里云 pip 参数。
- `.env.example` 受当前工作区凭据保护，`config/app.example` 是可读副本。
