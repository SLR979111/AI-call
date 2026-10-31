# THIRD_PARTY · 开源组件与预训练模型清单（v0.1）

> 赛题要求：逐项标注名称、版本、来源、许可证。
> 状态：当前为 Fake 骨架，**尚未接入任何预训练语音/语言模型**；接入时逐项追加。
> 许可证以各项目官方仓库为准；本表用于参赛材料整理，不构成法律意见。

## 1. 运行时依赖

| 名称 | 版本约束 | 来源 | 许可证 | 用途 |
|---|---|---|---|---|
| Python | 3.12 | https://www.python.org/ | PSF-2.0 | 运行时 |
| FastAPI | >=0.115,<1.0 | https://github.com/fastapi/fastapi | MIT | HTTP/WS 框架 |
| Starlette | 由 FastAPI 解析 | https://github.com/encode/starlette | BSD-3-Clause | ASGI 基础 |
| Uvicorn | >=0.32,<1.0 | https://github.com/encode/uvicorn | BSD-3-Clause | ASGI 服务器 |
| websockets | >=14,<16 | https://github.com/python-websockets/websockets | BSD-3-Clause | WS 协议实现 |
| Pydantic | >=2.9,<3.0 | https://github.com/pydantic/pydantic | MIT | 请求模型/校验 |
| jsonschema | >=4.23,<5.0 | https://github.com/python-jsonschema/jsonschema | MIT | 契约校验 |
| HTTPX | >=0.27,<1.0 | https://github.com/encode/httpx | BSD-3-Clause | 测试客户端 |
| pytest | >=8.3,<9.0 | https://github.com/pytest-dev/pytest | MIT | 测试（仅开发） |

## 2. 前端

| 名称 | 版本 | 来源 | 许可证 | 用途 |
|---|---|---|---|---|
| 原生 HTML/CSS/JS | — | 本项目 | 本项目 | 无第三方前端依赖 |

## 3. 计划接入（接入时核实版本/许可证）

| 名称 | 计划用途 | 许可证（待核实） |
|---|---|---|
| LiveKit Server | WebRTC 媒体面 | Apache-2.0（待核实） |
| sherpa-onnx | CPU ASR/TTS | Apache-2.0（待核实） |
| FunASR | 本地 ASR（开发/对照） | MIT（待核实） |
| CosyVoice | 本地 TTS（开发/对照） | Apache-2.0（待核实） |
| 云 LLM API | Agent 推理 | 以服务商条款为准 |

## 4. 维护规则

- 每新增一个依赖/模型，在同一 PR 里更新本表；缺版本/许可证不允许合并。
- 提交前跑一次 `pip freeze` 或 `uv lock`，把实际版本回填。
- 不使用成品化商业“整机外呼机器人”；所有核心编排/合规/评测代码自研。
