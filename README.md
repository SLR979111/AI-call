# 智语外呼 · 语音外呼数字员工（Fake 骨架）

这是“智语外呼”竞赛项目的 **Fake 全链路骨架**，对应 W1 集成计划：暴露 HTTP 控制面、提供静态 Demo 页面、记录 append-only 事件日志，ASR / Agent / TTS 先用确定性 Fake 实现。

当前不需要模型权重、不需要 GPU、不需要外部网络。真实 CPU ASR/TTS、云 LLM、WebRTC/LiveKit 是后续里程碑；架构、契约与计划见 [docs/](docs/)（建议先看 [docs/05-week1-integration-plan.md](docs/05-week1-integration-plan.md) 和 [docs/09-delivery-runtime.md](docs/09-delivery-runtime.md)）。

## 环境要求

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | 3.12 | 本地运行与测试 |
| Docker Engine + Compose | Compose v2.24+ | 容器运行 |
| make | 任意 POSIX make | 可选快捷命令 |

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8081
```

`app/config.py` 的默认值与 [.env.example](.env.example) 对齐，本地不需要 `.env`；需要覆盖时在 shell 里导出变量即可。Docker Compose 会自动读取 `.env`。

也可以用 Makefile：

```bash
make install
make run
```

启动后打开：

- Demo 页面：<http://localhost:8081/>
- 健康检查：<http://localhost:8081/healthz>
- OpenAPI：<http://localhost:8081/docs>

### 常见问题：WebSocket 404

如果 uvicorn 日志出现 `No supported WebSocket library detected`（`websockets` 16.x 常见），先安装 `requirements.txt`（已把版本钉在 `<16`），或这样启动：

```bash
uvicorn app.main:app --ws websockets-sansio --port 8081
```

### 用 curl 跑一轮文本对话

```bash
curl -s http://localhost:8081/healthz

SESSION_ID=$(curl -s -X POST http://localhost:8081/sessions \
  -H 'Content-Type: application/json' \
  -d '{"mode":"text"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "http://localhost:8081/sessions/$SESSION_ID/text" \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，我想查询一下我的订单。"}'

curl -sN "http://localhost:8081/sessions/$SESSION_ID/events?since_seq=0"
```

### 没有云 Key 时跑 LLM 模式

先启动本地 OpenAI 兼容 Mock，再把应用指过去：

```bash
python scripts/mock_llm.py --port 9000

AGENT_PROVIDER=llm \
LLM_PRIMARY_BASE_URL=http://127.0.0.1:9000/v1 \
LLM_PRIMARY_API_KEY=mock \
LLM_PRIMARY_MODEL=mock \
uvicorn app.main:app --port 8082
```

这样可以不消耗云额度，验证 `LLMAgent → LLM Router → Tool Gateway → 结构化回写` 整条链路。

## Docker

```bash
cp .env.example .env
make docker-build
make docker-up

curl -fsS http://localhost:8081/healthz
docker-compose logs -f app
make docker-down
```

`docker compose`（v2 插件）也可以用；Makefile 默认用 `docker-compose`，因为当前工作区用的是独立二进制。可覆盖：

```bash
make docker-up COMPOSE="docker compose"
```

如果所在网络访问 Docker Hub 慢或被拦，可以先用镜像加速器拉基础镜像，并在构建时传 PyPI 镜像：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

DOCKER_BUILDKIT=0 docker build \
  --build-arg PIP_INDEX_URL=http://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_TRUSTED_HOST=mirrors.aliyun.com \
  -t aicall-app:local .
```

原因：当前工作区的 Docker Desktop 继承了残留代理 `127.0.0.1:10808`，BuildKit 拉 `docker/dockerfile:1` 会失败；用 classic builder + 本地基础镜像可以绕开。

等价的原生命令：

```bash
docker build -t aicall-app:local .
docker compose up -d
```

说明：

- 镜像按 **CPU-only**、`linux/amd64` 设计（比赛现场目标）；Fake 骨架没有 GPU profile。
- `docker-compose.yml` 目前只启动 `app`；LiveKit、CPU ASR/TTS 和 GPU override 是注释占位，留给后续里程碑。
- `./data` 绑定到 `/app/data`，存 SQLite、JSONL 事件日志和产物；已在 `.gitignore` 中忽略。
- Compose v2.24+ 下 `.env` 可选；没有 `.env` 时使用镜像默认值。
- 赛题禁止外网穿透/代理，仓库不包含任何隧道工具。

## 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | `web/` 静态 Demo 页面 |
| GET | `/healthz` | 存活检查 + 运行时元信息 |
| GET | `/metrics` | 最小 Prometheus 文本（`aicall_up`、`aicall_sessions`） |
| POST | `/sessions` | 建会话（`mode`: `text` 或 `voice`，可选 `scenario_id`） |
| GET | `/sessions/{session_id}/state` | 当前轮次/流程状态 |
| POST | `/sessions/{session_id}/text` | 跑一轮文本对话 |
| POST | `/sessions/{session_id}/cancel` | 取消当前轮（调试用） |
| POST | `/sessions/{session_id}/end` | 结束会话 |
| GET | `/sessions/{session_id}/events?since_seq=0` | NDJSON 事件流 |
| WS | `/ws/session/{session_id}` | 骨架媒体/控制通道 |
| POST | `/eval/sessions` | 跑 headless 文本评测 |
| GET | `/eval/sessions/{eval_session_id}` | 读评测记录 |
| POST | `/internal/tools/{tool}/invoke` | 经 Tool Gateway 调工具 |
| POST | `/internal/llm/complete` | 调试 LLM Router（未配置返回 503 类错误） |
| GET | `/voice/status` | 当前语音 Provider / 就绪状态 |
| GET | `/media/status` | LiveKit 配置状态 |
| POST | `/sessions/{session_id}/rtc` | 生成 LiveKit token（未配置返回 503） |
| POST | `/knowledge/search` | 本地 RAG 检索 + 引用 |
| GET | `/flowpacks` / `/flowpacks/{id}` | 列出/读取 Flow Pack |
| POST | `/flowpacks/{id}/simulate` | 按意图序列模拟流程 |
| POST | `/eval/run` | 跑评测套件（默认 `eval/cases/dev.json`） |
| GET | `/registry/production` / `/registry/releases` | 当前/历史版本 |
| POST | `/registry/candidates` / `/promote` / `/rollback` | 候选版本生命周期 |
| POST | `/rsi/compare` | 基线 vs 候选（bootstrap CI + 门禁） |
| POST | `/rsi/promote` | 门禁通过才晋升 |

字段级契约以 [docs/03-interface-contract-v0.1.md](docs/03-interface-contract-v0.1.md)、[docs/11-contract-v0.2-patch.md](docs/11-contract-v0.2-patch.md) 和 [docs/04-event-schema-v0.1.md](docs/04-event-schema-v0.1.md) 为准。Fake 骨架实现 W1/W2 子集；真实 ASR/TTS 和 LiveKit 服务是后续里程碑。

## Make 命令

| 目标 | 命令 |
|---|---|
| `make install` | `python -m pip install -r requirements.txt` |
| `make run` | 在 `0.0.0.0:8081` 启动 uvicorn（`--reload`） |
| `make test` | `pytest -q` |
| `make contract-test` | 先校验 `contracts/fixtures`，再跑 `pytest tests/test_contract.py -q` |
| `make smoke` | `sh scripts/smoke.sh`（服务需已启动） |
| `make e2e` | 2 轮 + 打断 + 挂断的 WS E2E（`scripts/e2e_ws.py`） |
| `make docker-build` | `docker build -t aicall-app:local .` |
| `make docker-up` | `docker compose up -d --build` |
| `make docker-down` | `docker compose down` |

命令行可覆盖默认值，例如 `make run PORT=9000`、`make test PYTHON=python3.12`。

## 配置

| 变量 | 示例 | 含义 |
|---|---|---|
| `APP_ENV` | `dev` | 运行环境（`dev` / `staging` / `prod`） |
| `APP_BASE` | `http://127.0.0.1:8081` | 前端/开发基地址（见 `config/app.example`） |
| `RELEASE_ID` | `rel_dev_001` | 每个 session/事件固定的版本 |
| `DATA_DIR` | `./data` | SQLite / JSONL / 产物目录 |
| `DEV_NO_AUTH` | `1` | 开发期跳过鉴权（Fake 骨架默认不强制） |
| `AGENT_PROVIDER` | `fake` | `fake` 或 `llm`（LLM Router 路径，需要 Key） |
| `LLM_PRIMARY_BASE_URL` | `https://api.deepseek.com/v1` | 主 OpenAI 兼容接口 |
| `LLM_PRIMARY_API_KEY` | `sk-...` | 主 Key（仅服务端） |
| `LLM_PRIMARY_MODEL` | `deepseek-chat` | 主模型 |
| `LLM_SECONDARY_*` | 同上 | 可选备用供应商 |
| `LLM_TIMEOUT_MS` | `3000` | 单供应商超时 |
| `LLM_DAILY_BUDGET_CNY` | `100` | 日预算熔断 |
| `VOICE_PROVIDER` | `fake` | `fake` / `sherpa` / `funasr` / `cosyvoice` |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | — | 启用 `/sessions/{id}/rtc` token |

没有 LLM Key 时，`/internal/llm/complete` 返回 `LLM_NOT_CONFIGURED`，Fake Agent 继续用规则/模板回复。

把 [.env.example](.env.example) 复制成 `.env`；不要提交 `.env`。带 `APP_BASE=http://127.0.0.1:8081` 的可读副本在 [`config/app.example`](config/app.example)。

## 测试

```bash
make test            # 全量 pytest
make contract-test   # Schema 校验 + 契约测试
make smoke           # 需要另开终端先 make run / make docker-up
make e2e             # 2 轮 + 打断 + 挂断（WS）
```

`make contract-test` 等价于：

```bash
python contracts/validate.py contracts/fixtures
pytest tests/test_contract.py -q
```

`scripts/smoke.sh` 会检查 `GET /healthz`、创建文本会话、跑一轮并拉取事件流。指向其他地址：

```bash
BASE_URL=http://127.0.0.1:8081 sh scripts/smoke.sh
```

回放一轮事件时间线、生成自测报告：

```bash
python scripts/replay.py --session s_...
python scripts/report.py --data-dir data --out reports/self-test.md
python scripts/rsi_compare.py --baseline data/baseline.json --candidate data/candidate.json
```

CI（`.github/workflows/ci.yml`）在 `ubuntu-latest` + Python 3.12 上安装 `requirements.txt`，跑 `pytest -q`，校验 `contracts/fixtures`，最后启动服务跑 WS E2E。

## 项目结构

```text
aicall/
├── app/                  FastAPI 应用（orchestrator、agent、llm、rag、flow、rsi）
├── web/                  静态 Demo 页面
├── contracts/            JSON Schema、fixtures、validate.py
├── knowledge/            本地 FAQ 知识库
├── flowpacks/            业务流程定义
├── eval/                 评测用例
├── reports/              生成的自测报告
├── tests/                pytest 测试
├── scripts/              smoke / e2e / mock_llm / report / rsi / replay
├── docs/                 架构、契约、交付文档
├── data/                 运行时 SQLite/JSONL/产物（gitignore）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── Makefile
├── .env.example
└── .github/workflows/ci.yml
```

## 当前骨架范围

- ASR / Agent / TTS 都是确定性 Fake，不需要模型文件。
- 事件链路为内存 + JSONL + SQLite，`seq` 单调递增，可用于回放和时延分析（见 [docs/04](docs/04-event-schema-v0.1.md)）。
- 暂不包含：真实 ASR/TTS、云 LLM 接入、LiveKit/WebRTC、coturn、正式鉴权、多场景 Flow Pack。
