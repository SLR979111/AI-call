# 开发与运行指南

> 面向需要本地跑代码、调接口、做联调的人。项目总览看根目录 [README](../../README.md)。

## 1. 本地运行

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8081
```

默认值与 [.env.example](../../.env.example) 对齐，本地不需要 `.env`；Docker Compose 会自动读取 `.env`。

启动后：

| 地址 | 用途 |
|---|---|
| <http://localhost:8081/> | 旧版最小 Demo（文本输入 + 浏览器 TTS） |
| <http://localhost:8081/rtc/> | cx 新通话台（麦克风/文件回灌，零依赖 ESM） |
| <http://localhost:8081/healthz> | 健康检查 |
| <http://localhost:8081/docs> | OpenAPI |

`/rtc/` 接真后端时默认同源，直接开即可；页面与后端不同源时才加 `?app=http://HOST:PORT`。无麦克风用页面上的“无麦回放”。

## 2. 常用 Make 目标

| 目标 | 作用 |
|---|---|
| `make run` | 在 `0.0.0.0:8081` 启动（reload） |
| `make test` | 全量 pytest |
| `make contract-test` | Schema 校验 + 契约测试 |
| `make smoke` | 健康检查 + 一轮文本（服务需已启动） |
| `make e2e` | 2 轮 + 打断 + 挂断的 WS E2E |
| `make metric` | 全量比赛指标自检 |
| `make metric-closure/latency/task/compliance/barge` | 单段指标自检 |
| `make report` | 从 `data/sessions` 生成自测报告 |
| `make docker-build/up/down` | Docker 构建/启动/停止 |

## 3. 配置项

| 变量 | 示例 | 含义 |
|---|---|---|
| `APP_ENV` | `dev` | 运行环境 |
| `APP_BASE` | `http://127.0.0.1:8081` | 开发基地址 |
| `RELEASE_ID` | `rel_dev_001` | 版本固定值 |
| `DATA_DIR` | `./data` | SQLite / JSONL / 产物 |
| `DEV_NO_AUTH` | `1` | 开发期跳过鉴权 |
| `AGENT_PROVIDER` | `fake` | `fake` 或 `llm` |
| `LLM_PRIMARY_*` | `https://api.deepseek.com/v1` 等 | 主 LLM |
| `LLM_SECONDARY_*` | 同上 | 备用 LLM |
| `LLM_TIMEOUT_MS` | `3000` | 单供应商超时 |
| `LLM_DAILY_BUDGET_CNY` | `100` | 日预算熔断 |
| `VOICE_PROVIDER` | `fake` | `fake` / `sherpa` / `funasr` / `cosyvoice` |
| `LIVEKIT_*` | URL/Key/Secret | 启用 `/sessions/{id}/rtc` |

没有 LLM Key 时，`/internal/llm/complete` 返回 `LLM_NOT_CONFIGURED`，Fake Agent 继续用规则回复。

## 4. 接口速查

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz`、`/metrics` | 健康 / Prometheus |
| POST | `/sessions` | 建会话（text/voice） |
| GET | `/sessions/{id}/state` | 当前状态 |
| POST | `/sessions/{id}/text` | 一轮文本 |
| POST | `/sessions/{id}/cancel`、`/end` | 取消 / 结束 |
| GET | `/sessions/{id}/events?since_seq=0` | NDJSON 事件流 |
| WS | `/ws/session/{id}?token=...` | 控制/音频通道 |
| POST | `/eval/sessions`、`/eval/run` | 3 轮会话 / 评测套件 |
| POST | `/internal/tools/{tool}/invoke` | 直调 Tool Gateway |
| GET/POST | `/registry/*`、`/rsi/*` | 版本与 RSI |
| GET | `/voice/status`、`/media/status` | 语音/媒体状态 |
| POST | `/sessions/{id}/rtc` | LiveKit token（未配置 503） |
| GET/POST | `/knowledge/search`、`/flowpacks/*` | RAG / 流程 |

字段级契约以 [03](../03-interface-contract-v0.1.md)、[11](../11-contract-v0.2-patch.md)、[04](../04-event-schema-v0.1.md) 为准。

## 5. Docker 与网络

```bash
docker-compose up -d --build
curl -fsS http://localhost:8081/healthz
docker-compose logs -f app
docker-compose down
```

`docker compose`（v2 插件）也可用；Makefile 默认 `docker-compose`，可覆盖：

```bash
make docker-up COMPOSE="docker compose"
```

如果 Docker Hub 被限速/拦截，用镜像加速器 + 本地基础镜像：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

DOCKER_BUILDKIT=0 docker build \
  --build-arg PIP_INDEX_URL=http://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_TRUSTED_HOST=mirrors.aliyun.com \
  -t aicall-app:local .
```

原因：部分 Windows Docker Desktop 会继承残留代理，BuildKit 拉 `docker/dockerfile:1` 失败；classic builder + 本地基础镜像可绕开。

## 6. 排障

- **WebSocket 404 / No supported library**：`websockets` 钉在 `<16`；或 `uvicorn ... --ws websockets-sansio`。
- **pytest 找不到 app**：在仓库根目录执行；`pytest.ini` 已配 `pythonpath=.`，根目录 `conftest.py` 兜底。
- **Docker 连不上**：确认 Docker Desktop 已启动；若 CLI 报命名管道错误，检查 Desktop 运行状态与管道权限。
- **端口冲突**：宿主统一 8081，容器内 8080；改 `PORT` 或 compose 映射。
