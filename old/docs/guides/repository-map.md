# 仓库结构图

> 本文按 **2026-11-02 的实际目录树**绘制，用于快速定位代码、契约、数据与交付物。
> 图中只保留纳入版本管理的目录；`data/`、`dist/`、`artifacts/`、`.deps/`、`.tmp/`、`__pycache__/`、`.pytest_cache/`、`pytest-cache-files-*` 等生成或缓存目录已省略。

## 1. 总览

```mermaid
flowchart TB
  subgraph ACCESS["接入与前端"]
    WEB["web/<br/>旧版最小 Demo"]
    RTC["web/rtc/<br/>新通话台"]
    MAIN["app/main.py<br/>FastAPI HTTP/WS 接入"]
  end

  subgraph CORE["核心运行链路"]
    ORCH["app/orchestrator.py<br/>Session/Turn 编排"]
    AGENT["app/agent/<br/>Fake / LLM Agent"]
    TOOLS["app/tools/<br/>Tool Gateway 工具网关"]
    COMP["app/compliance.py<br/>合规守卫"]
    LLM["app/llm/<br/>主备 LLM Router"]
    RAG["app/rag/<br/>本地 FAQ 检索"]
    FLOW["app/flow/<br/>Flow Pack 执行"]
    MEDIA["app/media/<br/>LiveKit 媒体边界"]
    VOICE["app/voice/<br/>ASR/TTS 适配边界"]
  end

  subgraph SUPPORT["事件、版本与评测支撑"]
    EVENTS["app/events.py<br/>EventBus（JSONL + SQLite）"]
    REGISTRY["app/registry.py<br/>版本注册 / 回滚"]
    REPORTING["app/reporting.py<br/>指标聚合 / 报告"]
    EVAL_RUN["app/eval/<br/>评测 Runner"]
    RSI["app/rsi/<br/>bootstrap 门禁"]
  end

  subgraph DATA["契约与数据"]
    CONTRACTS["contracts/<br/>JSON Schema 与 fixtures"]
    SCHEMAS["contracts/schemas/<br/>6 份 v1 Schema"]
    EVAL["eval/cases/<br/>评测用例"]
    FLOWPACKS["flowpacks/<br/>业务流程 JSON"]
    KNOWLEDGE["knowledge/<br/>FAQ 知识库"]
  end

  subgraph TESTING["测试与工具"]
    TESTS["tests/<br/>pytest 契约测试"]
    SCRIPTS["scripts/<br/>smoke / e2e / replay / report"]
    REPORTS["reports/<br/>自测与指标报告"]
  end

  subgraph DOCS["文档"]
    DOCS_ROOT["docs/<br/>架构 · 契约 · 方案"]
    DOCS_GUIDES["docs/guides/<br/>开发与测试指南"]
    DOCS_REF["docs/reference/<br/>赛题书与三周计划"]
    DOCS_REVIEWS["docs/reviews/<br/>契约评审与回复"]
  end

  subgraph DELIVERY["交付"]
    CI[".github/workflows/ci.yml<br/>CI 流水线"]
    DOCKERFILE["Dockerfile<br/>镜像构建"]
    COMPOSE["docker-compose.yml<br/>本地编排"]
    MAKE["Makefile<br/>开发命令入口"]
    REQS["requirements.txt<br/>Python 依赖"]
    CONFIG["config/app.example<br/>配置样例"]
  end

  WEB --> MAIN
  RTC --> MAIN

  MAIN --> ORCH
  ORCH --> AGENT
  ORCH --> VOICE
  AGENT --> LLM
  AGENT --> RAG
  AGENT --> FLOW
  AGENT --> TOOLS
  AGENT --> COMP
  COMP -. "BLOCK 取消播报" .-> VOICE
  MAIN -. "媒体协商" .-> MEDIA

  RAG -. "读取 FAQ" .-> KNOWLEDGE
  FLOW -. "加载流程" .-> FLOWPACKS
  TOOLS -. "Tool Schema" .-> CONTRACTS
  CONTRACTS --> SCHEMAS
  ORCH -. "事件" .-> EVENTS
  AGENT -. "事件" .-> EVENTS
  TOOLS -. "事件" .-> EVENTS
  COMP -. "事件" .-> EVENTS
  EVENTS -. "日志" .-> EVAL_RUN
  EVENTS -. "日志" .-> REPORTING
  EVAL_RUN --> RSI
  REGISTRY -. "版本固定" .-> AGENT
  EVAL -. "用例输入" .-> SCRIPTS
  SCRIPTS -. "生成" .-> REPORTS
  TESTS -. "校验" .-> CONTRACTS

  DOCS_ROOT --> DOCS_GUIDES
  DOCS_ROOT --> DOCS_REF
  DOCS_ROOT --> DOCS_REVIEWS

  CI -. "执行测试" .-> TESTS
  MAKE -. "封装命令" .-> SCRIPTS
  COMPOSE -. "编排" .-> DOCKERFILE
  DOCKERFILE -. "启动" .-> MAIN
  REQS -. "安装依赖" .-> DOCKERFILE
  CONFIG -. "环境变量" .-> MAIN
```

> 说明：实线表示运行时调用，虚线表示读取、校验或构建依赖。`app/media/` 与 `app/voice/` 目前是边界占位，真实媒体面尚未接入。

## 2. 顶层目录职责

| 目录 | 职责 |
|---|---|
| `app/` | FastAPI 应用与核心运行链路：入口、编排、Agent、工具网关、合规、LLM、RAG、Flow、事件、Registry、评测与 RSI |
| `web/` | 前端页面；`web/rtc/` 为零依赖 ESM 的新通话台，含音频、协议与会话模块 |
| `contracts/` | 接口契约：JSON Schema、会话 fixtures 与 `validate.py` 校验脚本 |
| `docs/` | 架构、契约、方案、评审与指南；`reference/` 存赛题与计划，`guides/` 存开发/测试指南 |
| `eval/` | 评测用例，当前包含 `cases/dev.json` 与 POP 场景 `cases/pop_audit.json` |
| `flowpacks/` | 业务流程配置，当前包含 `service_outbound.json` 与 `pop_qualification_audit.json` |
| `knowledge/` | 本地知识库，当前包含 `faq.json` 与 POP 场景 `pop_audit.json` |
| `tests/` | pytest 契约与集成测试 |
| `scripts/` | 冒烟、WS E2E、回放、指标检查、报告与 RSI 对比脚本 |
| `reports/` | 自测报告与指标检查结果 |
| `config/` | 配置样例，当前为 `app.example` |
| `.github/` | CI 流水线，当前为 `workflows/ci.yml` |

## 3. 未画入的目录

以下目录或文件由运行与工具链生成，不属于仓库结构：`.git/`、`.deps/`、`.tmp/`、`.uvcache/`、`.uvdata/`、`.uvpython/`、`.pytest_cache/`、`.chrome-tmp*/`、`data/`、`dist/`、`artifacts/`、`__pycache__/`、`pytest-cache-files-*/`、`node_modules/`。新增或移动目录后，同步更新本图与目录表。
