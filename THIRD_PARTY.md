# THIRD_PARTY · 开源组件与预训练模型清单（v1.2）

> 赛题要求：逐项标注名称、版本、来源、许可证。
> 本表是仓库内**唯一许可证清单**；新增依赖/模型必须在同一 PR 更新本表。
> 本表用于工程与参赛材料整理，不构成法律意见。
> 权重不随仓库分发；部署方需保留模型卡、LICENSE/NOTICE 和授权凭证。
> 本轮由 3 个子任务分别核实 Python 依赖、模型权重、系统/传递依赖。
> 许可证原文/声明见 [licenses/](licenses/)；交付行动项见 [docs/20](docs/20-license-checklist.md)。

## 1. Python 直接依赖（本机实际版本）

| 名称 | 实际版本 | 许可证 | 来源/证据 | 状态 | 备注 |
|---|---|---|---|---|---|
| Python | 3.11.17 | PSF-2.0 | python.org | 已核实 | 本地语音环境 |
| FastAPI | 0.162.1 | MIT | `.web311/.../METADATA` | 已核实 | `.deps` 0.135.3 |
| Starlette | 1.10.0 | BSD-3-Clause | `.web311/.../METADATA` | 已核实 | 传递依赖 |
| Uvicorn | 0.54.0 | BSD-3-Clause | `.web311/.../METADATA` | 已核实 | `.deps` 0.44.0 |
| websockets | 15.0.1 | BSD-3-Clause | `.web311/.../METADATA` | 已核实 | `.deps` 16.0 超出 `<16` |
| Pydantic | 2.14.3 | MIT | `.web311/.../METADATA` | 已核实 | `.deps` 2.13.4 |
| jsonschema | 4.26.0 | MIT | `.web311/.../METADATA` | 已核实 | — |
| HTTPX | 0.28.1 | BSD-3-Clause | `.web311/.../METADATA` | 已核实 | — |
| pytest | 9.1.1（仅系统） | MIT | Python 3.13 `site-packages` | 许可证已核实 | 项目 3.11 未安装；`<9.0` 不匹配 |
| python-dotenv | 1.2.4 | BSD-3-Clause | `.web311/.../METADATA` | 已核实 | — |
| NumPy | 1.23.5（本地语音） | BSD-3-Clause | `pytorch-env/.../LICENSE.txt` | 已核实 | 低于 `>=1.26` |
| SciPy | 1.10.1（本地语音） | BSD-3-Clause | `pytorch-env/.../LICENSE.txt` | 已核实 | 低于 `>=1.12` |
| sherpa-onnx | 2.0.1 | Apache-2.0 | `.web311/.../LICENSE` | 已核实 | wheel 无第三方 NOTICE |
| onnxruntime | 1.28.0 | MIT | `.web311/onnxruntime/LICENSE` | 已核实 | 残留 1.22 dist-info，metadata 版本有歧义 |
| aiortc | 1.14.0（未安装） | BSD-3-Clause | PyPI/GitHub LICENSE | 官方已核实 | 项目环境未安装；`--rtc-pcm` 不可用 |

## 2. 预训练模型/权重

| 模型 | 来源 | 代码许可 | 权重许可 | 状态 | 备注 |
|---|---|---|---|---|---|
| sherpa Zipformer 中文 ASR 权重 | `csukuangfj/...-2025-06-30` ← `yuekai/...` | icefall 代码 Apache-2.0 | **缺**：转换仓库无 LICENSE，上游 HF 未声明 | 权重缺 | 训练语料多源（AISHELL/WeNetSpeech 等），商用需逐项核实 |
| MeloTTS vits-melo-tts-zh_en | 本地 `models/melo`；myshell-ai/MeloTTS | MIT | MIT（本地 LICENSE） | 已核实 | 训练数据/说话人授权未标注 |
| Qwen3-TTS-12Hz-1.7B-Base | Qwen | Apache-2.0 | Apache-2.0 | 已核实 | — |
| MiniCPM-o 4.5 | openbmb | Apache-2.0 | Apache-2.0 | 已核实 | 内部组件另核 |
| MiniCPM-o 4.5 W4A16 | 88plug 社区量化 | 量化脚本未单列 | Apache-2.0（沿用 base） | 已核实（元数据） | 再分发保留 base 许可/署名 |
| Moshi | kyutai-labs/moshi | **Apache-2.0** | **CC-BY-4.0** | 已核实 | 代码/权重义务不同；CC-BY 需署名、标许可、指出修改 |
| Freeze-Omni | VITA-MLLM | 仓库 `License.txt`，GitHub 识别 NOASSERTION/Other | Apache-2.0 **+ 腾讯 AUP** | 代码待核实；权重已核实 | AUP 限制军事、高风险自动决策、欺诈、冒充等 |
| Step-Audio 2 mini | stepfun-ai | Apache-2.0 | Apache-2.0（HF 标签） | 已核实（标签级） | 回合制，不是双工 |
| Qwen3-Omni | Qwen | Apache-2.0 | Apache-2.0 | 已核实 | HF 标签 `other` 与 card `apache-2.0` 不一致，保留 LICENSE 原文 |
| Qwen3-4B-Instruct-2507（测试候选） | Qwen | Apache-2.0 | Apache-2.0 | 已核实（HF card） | 8GB 建议 4-bit；工具能力需实测 |

### MiniCPM-o 内部组件

| 组件 | 已知许可 | 状态 |
|---|---|---|
| Whisper-medium | 代码 MIT；[HF 权重卡](https://huggingface.co/openai/whisper-medium) Apache-2.0 | 已核实（区分代码/权重） |
| CosyVoice2 | Apache-2.0（[代码仓库](https://github.com/QwenAudio/CosyVoice)） | 已核实（代码级） |
| SigLIP2 | Apache-2.0（[HF 模型卡](https://huggingface.co/google/siglip2-base-patch16-224)） | 已核实 |
| Qwen3-8B | Apache-2.0（[HF 模型卡](https://huggingface.co/Qwen/Qwen3-8B)） | 已核实 |

## 3. 系统/传递依赖

| 组件 | 来源 | 许可证 | 义务/风险 | 状态 |
|---|---|---|---|---|
| libsndfile | SoundFile / Docker apt | LGPL-2.1-or-later | 动态链接自有代码不必 LGPL；分发镜像需附 LGPL 全文、版权、允许替换/重链接。原文：`licenses/libsndfile-LGPL-2.1.txt`；说明：`licenses/libsndfile-NOTICE.md` | 条款已核实，版本待核实 |
| OpenBLAS | NumPy wheel | BSD-3-Clause | 保留版权/许可/免责声明 | 已核实 |
| LAPACK | OpenBLAS | BSD-3-Clause（修改版） | 保留 LAPACK 版权与许可 | 已核实 |
| GCC runtime/libgomp1/libgfortran | Docker apt `libgomp1`；NumPy/OpenBLAS | GPL-3.0-or-later WITH GCC-exception-3.1 | 例外允许独立模块按自选条款分发；分发二进制需保留 GPL/例外文本并提供对应源码/要约。原文：`licenses/GCC-RUNTIME-EXCEPTION-3.1.txt`；说明：`licenses/GCC-RUNTIME-NOTICE.md` | 类型已核实，版本待核实 |
| NVIDIA CUDA/cuDNN/NCCL 等 | PyTorch 2.11.0 cu128 | NVIDIA 专有 EULA | **当前清单原缺失，最高风险**；Docker 再分发需遵守 NVIDIA EULA。声明：`licenses/NVIDIA-CUDA-NOTICE.md` | 待核实 |
| PyTorch / torchaudio | GPU sidecar | BSD-3-Clause / BSD-2-Clause | 保留版权/许可；CUDA 组件另算 | 已核实 |
| Transformers | GPU sidecar | Apache-2.0 | 保留 LICENSE/NOTICE | 已核实 |
| faster-qwen3-tts | GPU sidecar | MIT | 保留版权/许可；上游 Qwen3-TTS Apache 归属 | 已核实 |
| qwen-tts-hf | GPU sidecar | Apache-2.0 | 非官方兼容发行版；保留 NOTICE | 已核实 |
| SoundFile | GPU sidecar | BSD-3-Clause | libsndfile 另见上 | 已核实 |
| LiveKit Server/SDK | 未固定版本 | Apache-2.0（待核实） | 自研 JWT 不产生 SDK 许可义务 | 待核实 |
| coturn | 未固定 | BSD-3-Clause（待核实） | 传递 OpenSSL/libevent | 待核实 |
| FunASR | 未引入 | MIT（待核实）+ 模型权重另算 | 接入时逐模型核实 | 待核实 |
| CosyVoice | 未引入 | Apache-2.0（待核实）+ 模型权重另算 | 接入时核对模型卡 | 待核实 |

## 4. 已发现的高风险/缺口

1. **sherpa ASR 权重无许可证**：发布包和上游 HF 卡均未声明，且训练语料多源；商用需向发布方确认。
2. **Moshi 代码 Apache-2.0，权重 CC-BY-4.0**：不能只保留 Apache 声明；用权重必须履行 CC-BY 署名/标识义务。
3. **Freeze-Omni 权重 Apache-2.0 受腾讯 AUP 约束**；代码 `License.txt` 非标准 SPDX，需逐条复核。
4. **PyTorch cu128 捆绑 NVIDIA 专有组件**：旧版清单未列，Docker 镜像再分发需审 NVIDIA EULA。
5. **libsndfile LGPL**：分发镜像需附 LGPL 文本、版权、替换/重链接条件。
6. **依赖版本与 requirements 不一致**：numpy 1.23.5、scipy 1.10.1 低于约束；websockets `.deps` 16.0 超出；pytest 9.1.1 超出；需要锁文件或调整约束。
7. **onnxruntime 元数据歧义**：实际 1.28.0，残留 1.22 dist-info。
8. **aiortc 未安装**：`--rtc-pcm` 当前不可用。
9. **Melo 训练数据/说话人授权未标注**：MIT 只覆盖代码/权重文件，不覆盖数据授权。

## 5. 许可证义务清单

- Apache-2.0：保留 LICENSE/NOTICE、标注修改、不得暗示官方背书、专利条款。
- MIT/BSD：保留版权、许可、免责声明。
- CC-BY-4.0：署名、许可链接、指出是否修改；无专利授权。
- LGPL-2.1：动态链接；分发需附许可文本、允许替换/重链接。
- GPL-3.0-with-GCC-exception：保留例外文本；分发 libgfortran 二进制需提供对应源码/要约。
- 模型权重与代码许可分开；参考音频授权与软件许可分开。

## 6. 维护规则

```text
新增依赖或模型 → 同一 PR 更新本表
新增模型权重 → 附模型卡/许可证链接
发布前 → 锁文件/实际版本回填
交付包 → 附本表 + LICENSE/NOTICE 原文
```
