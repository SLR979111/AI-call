# THIRD_PARTY · 开源组件与预训练模型清单（v0.1）

> 赛题要求：逐项标注名称、版本、来源、许可证。
> 状态：已接入 sherpa 流式 ASR、默认 Qwen3 GPU sidecar、可选 Melo CPU TTS；权重与参考音频不随仓库分发。
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
| NumPy | >=1.26,<3 | https://github.com/numpy/numpy | BSD-3-Clause | PCM 转换 |
| SciPy | >=1.12,<2 | https://github.com/scipy/scipy | BSD-3-Clause | Melo 重采样 |
| sherpa-onnx | 2.0.1 | [官方许可证](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE) | Apache-2.0 | CPU Zipformer ASR / OfflineTts Melo |
| Qwen3-TTS | Qwen3-TTS-12Hz-1.7B-Base | [官方仓库/许可证](https://github.com/QwenLM/Qwen3-TTS/blob/main/LICENSE)、[模型](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) | Apache-2.0 | GPU ICL 声音克隆 |
| MeloTTS | vits-melo-tts-zh_en ONNX 导出 | [官方许可证](https://github.com/myshell-ai/MeloTTS/blob/main/LICENSE)、[sherpa 模型发布](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models) | MIT | 可选 CPU TTS |
| faster-qwen3-tts | 0.5.4 | [许可证](https://github.com/andimarafioti/faster-qwen3-tts/blob/main/LICENSE) | MIT | sidecar 流式 Torch backend |
| qwen-tts-hf | 0.1.1.post1 | https://pypi.org/project/qwen-tts-hf/ | Apache-2.0 | faster-qwen 的模型封装依赖，实测包 metadata |
| PyTorch / torchaudio | 2.11.0 + cu128 | https://github.com/pytorch/pytorch | BSD-3-Clause / BSD-2-Clause | 仅 GPU sidecar |
| Transformers | 5.15.1 | https://github.com/huggingface/transformers | Apache-2.0 | 仅 GPU sidecar |
| SoundFile | >=0.13,<1（实测 0.14.0） | https://github.com/bastibe/python-soundfile | BSD-3-Clause | sidecar 读取参考音频，libsndfile 为 LGPL-2.1-or-later |

## 2. 前端

| 名称 | 版本 | 来源 | 许可证 | 用途 |
|---|---|---|---|---|
| 原生 HTML/CSS/JS | — | 本项目 | 本项目 | 无第三方前端依赖 |

## 3. 计划接入（接入时核实版本/许可证）

| 名称 | 计划用途 | 许可证（待核实） |
|---|---|---|
| LiveKit Server | WebRTC 媒体面 | Apache-2.0（待核实） |
| FunASR | 本地 ASR（开发/对照） | MIT（待核实） |
| CosyVoice | 本地 TTS（开发/对照） | Apache-2.0（待核实） |
| 云 LLM API | Agent 推理 | 以服务商条款为准 |

## 4. 维护规则

参考音频授权与软件/模型许可证独立：部署方必须取得录音本人的同意及适用授权。当前试听用 `zero_shot_prompt.wav` 不作为产品音色授权证明，也不随本仓库提交。sidecar 生产模式要求 `QWEN_REFERENCE_CONSENT=1`；授权凭据由部署方保存。模型挂载目录应保留原始模型卡/许可证，重新分发导出权重时按其具体条款处理。

- 每新增一个依赖/模型，在同一 PR 里更新本表；缺版本/许可证不允许合并。
- 提交前跑一次 `pip freeze` 或 `uv lock`，把实际版本回填。
- 不使用成品化商业“整机外呼机器人”；所有核心编排/合规/评测代码自研。
