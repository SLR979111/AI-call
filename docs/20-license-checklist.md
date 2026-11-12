# 20 · 许可证问题清单与责任人

> 完整清单见 [THIRD_PARTY.md](../THIRD_PARTY.md)。
> 本页只列“谁负责、做什么”，不构成法律意见。
> 状态：✅ 已核实 / ⚠️ 待核实 / ❌ 缺或许可不清。

## 1. 责任人总表

| 角色 | 负责范围 |
|---|---|
| **天命** | 交付打包、Docker/基础镜像、依赖锁、NVIDIA/LGPL/GCC 义务、`THIRD_PARTY.md` 总维护 |
| **SinCircle** | ASR/TTS/模型权重、Melo/MiniCPM/W4A16、语音数据与音色授权 |
| **pseudonym** | LLM Provider、Prompt/Function Calling、Agent 依赖、模型行为 |
| **cx** | WebRTC/LiveKit/coturn、前端依赖与媒体组件 |
| **湖中日月** | 评测集、Judge、RSI 数据与外部评测依赖 |
| **天命（牵头找赛事/法务接口）** | AUP、CC-BY、NVIDIA EULA、商用/再分发判断 |

## 2. P0：必须先处理

| # | 组件/问题 | 状态 | 主责 | 配合 | 必须动作 |
|---|---|---|---|---|---|
| 1 | sherpa Zipformer 中文 ASR 权重 | ❌ | **SinCircle** | 天命、法务 | 向发布方确认许可；拿不到则换有明确许可的 ASR，并在交付包标注 |
| 2 | Moshi 权重 | ✅ | **SinCircle** | 天命 | 代码 Apache-2.0；权重 CC-BY-4.0，署名/附许可/标修改 |
| 3 | Freeze-Omni | ⚠️ | **SinCircle** | 天命、法务 | 审腾讯 AUP；代码 License.txt 非 SPDX，单独确认 |
| 4 | PyTorch cu128 / NVIDIA CUDA | ⚠️ | **天命** | SinCircle、赛事/法务接口 | 镜像已构建：torch 2.11.0+cu128、cuDNN 9.19、NCCL 2.28；仍在等 EULA 再分发确认 |
| 5 | libsndfile | ✅ | **天命** | 赛事/法务接口 | 镜像内 libsndfile1 1.2.2-2+deb13u1；LGPL 原文/notice 已随镜像 |
| 5b | GCC runtime / libgomp1 | ✅ | **天命** | SinCircle | 镜像内 libgomp1 14.2.0-19；GCC exception 原文/notice 已随镜像 |
| 5c | Docker 权重策略 | ✅ | **天命** | SinCircle | app/qwen 镜像不 COPY models；compose read-only 挂载；ASR 权重许可未确认前不烘镜像；两镜像已 build |
| 6 | 依赖版本 | ✅ | **天命** | 全员 | 已生成 `requirements.lock`（40 包）和 `services/qwen_tts/requirements.lock`；Docker 改用锁文件 |
| 7 | onnxruntime 元数据 | ⚠️ | **天命** | — | 清理 1.22 dist-info，重记 1.28.0 |
| 8 | aiortc | ➖ | **cx** | 天命 | 当前未启用 `--rtc-pcm`；交付说明标为可选，不安装 |

## 3. P1：需要补证/落实

| # | 组件 | 状态 | 主责 | 配合 | 待补 |
|---|---|---|---|---|---|
| 9 | MiniCPM-o 4.5 内部 Qwen3/SigLIP2 | ✅ | **SinCircle** | 天命、法务 | 已核实：Qwen3-8B Apache-2.0、SigLIP2 Apache-2.0；Whisper 代码 MIT/权重 Apache-2.0；CosyVoice2 Apache-2.0 |
| 10 | Melo 训练数据/说话人授权 | ⚠️ | **SinCircle** | 法务 | MIT 只覆盖代码/权重，训练数据与音色授权单独确认 |
| 11 | LiveKit Server | ⚠️ | **天命** | cx、赛事/法务接口 | 实际 1.13.11；已落 `licenses/LiveKit-NOTICE.md`；正式交付锁 digest 并补 Apache-2.0/NOTICE |
| 11b | LiveKit 客户端 SDK | ⚠️ | **cx** | 天命 | `livekit-client 2.23.0` 已 vendored；Apache-2.0 全文/NOTICE 待随包 |
| 12 | coturn | ⚠️ | **cx** | 天命 | 当前 dev 未部署；正式 TURN 上线前锁版本并审 OpenSSL/libevent |
| 13 | FunASR / CosyVoice | ⚠️ | **SinCircle** | 湖中日月 | 接入时逐模型核实代码+权重；目前未引入 |
| 14 | W4A16 量化派生 | ✅ | **SinCircle** | 天命 | 保留 base Apache-2.0、量化说明、校准来源 |
| 15 | LLM Provider / API / 云端 Realtime | ⚠️ | **天命** | pseudonym、赛事/法务接口 | Provider 路由/成本/服务条款由天命；模型与数据边界由 pseudonym 会签 |
| 15b | Prompt / Function Calling / Agent 依赖 | ⚠️ | **pseudonym** | 天命 | 记录 Prompt 资产、工具依赖、数据边界 |
| 16 | 评测/Judge/RSI 外部依赖 | ⚠️ | **湖中日月** | 天命 | 记录 judge 服务/数据来源许可；合成数据声明 |
| 17 | 前端第三方依赖 | ✅ | **cx** | — | 当前零依赖；新增时更新 THIRD_PARTY |
| 18 | Qwen3-TTS 权重/参考音频 | ⚠️ | **SinCircle** | 天命 | 权重按 Model Card 保存许可；`data/voice/reference.wav` + 逐字转写 + 录音授权未到位前不得启用 |

## 4. 交付包责任

- [ ] `THIRD_PARTY.md` 总维护：**天命**
- [ ] MIT/BSD/Apache LICENSE/NOTICE 收集：**天命**
- [x] libsndfile LGPL 文本 + 替换/重链接说明：**天命**（已落 `licenses/`）
- [x] GCC runtime/libgomp1 例外文本：**天命**（已落 `licenses/`）
- [ ] 模型卡链接：sherpa ASR、Melo、Qwen3-TTS、MiniCPM-o、W4A16：**SinCircle**
- [x] 依赖锁：`requirements.lock` / `services/qwen_tts/requirements.lock`：**天命**
- [x] CPU 镜像 `aicall-app:license-lock`（729MB）和 qwen 镜像 `aicall-qwen-tts:license-lock`（12.6GB）已 build
- [ ] Moshi 若使用：CC-BY-4.0 署名与修改说明：**SinCircle**
- [ ] 参考音频/录音授权凭证：**SinCircle**
- [x] NVIDIA 组件声明：**天命**（已落 `licenses/NVIDIA-CUDA-NOTICE.md`；再分发权待确认）
- [ ] LiveKit Server/coturn：**天命**；LiveKit 客户端 SDK/aiortc：**cx**
- [x] LiveKit 版本/来源说明：**天命**（`licenses/LiveKit-NOTICE.md`）
- [x] Qwen3-TTS 资产说明：**天命**（`licenses/Qwen3-TTS-NOTICE.md`；音色授权仍由 SinCircle）
- [ ] LLM API 条款/数据边界：**pseudonym**
- [ ] 评测/Judge 数据许可：**湖中日月**

## 5. 发布前检查

```bash
python -m pip freeze > reports/pip-freeze.txt
python -m pip check
git diff -- THIRD_PARTY.md docs/20-license-checklist.md
```

## 6. 当前结论

```text
内部演示：可以
对外交付/比赛材料：不得宣称“许可证全清”

P0 阻塞：
1. sherpa ASR 权重许可（SinCircle）
2. NVIDIA CUDA EULA 再分发确认（天命 + 法务）
3. Melo/参考音频训练数据与音色授权（SinCircle）
4. LiveKit Apache-2.0 全文/NOTICE 正式随包（天命/cx）
5. Qwen3-TTS 模型卡与参考音频授权（SinCircle）

已收口：
- libsndfile LGPL / GCC runtime
- 依赖锁、两镜像构建
- Docker 不烘模型
- LiveKit/Qwen 版本与来源说明
```

## 7. 交付形态前提

本清单按“仓库源码 + Docker/二进制交付”双形态检查。`docs/09` 已改为**模型挂载、不烘进镜像**；sherpa ASR 权重许可未确认前不得烘进可分发镜像。若只交源码/让部署方自行下载，需在交付说明中写清。

在 sherpa ASR 权重许可未确认前，不建议把该权重作为“可商用/可再分发”资产对外交付。
