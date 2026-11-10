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
| 4 | PyTorch cu128 / NVIDIA CUDA | ⚠️ | **天命** | SinCircle、赛事/法务接口 | 已落 `licenses/NVIDIA-CUDA-NOTICE.md`；仍需确认镜像再分发权 |
| 5 | libsndfile | ⚠️ | **天命** | 赛事/法务接口 | 已落 `licenses/libsndfile-LGPL-2.1.txt` + `libsndfile-NOTICE.md`；待锁版本并随包 |
| 5b | GCC runtime / libgomp1 | ⚠️ | **天命** | SinCircle | 已落 `licenses/GCC-RUNTIME-EXCEPTION-3.1.txt` + notice；Dockerfile 的 libgomp1 需登记，准备源码/要约 |
| 6 | 依赖版本 | ⚠️ | **天命** | 全员 | 生成锁文件；对齐 numpy/scipy/pytest/websockets |
| 7 | onnxruntime 元数据 | ⚠️ | **天命** | — | 清理 1.22 dist-info，重记 1.28.0 |
| 8 | aiortc | ⚠️ | **cx** | 天命 | 若启用 `--rtc-pcm`，安装并锁版本；否则从交付说明移除 |

## 3. P1：需要补证/落实

| # | 组件 | 状态 | 主责 | 配合 | 待补 |
|---|---|---|---|---|---|
| 9 | MiniCPM-o 4.5 内部 Qwen3/SigLIP2 | ✅ | **SinCircle** | 天命、法务 | 已核实：Qwen3-8B Apache-2.0、SigLIP2 Apache-2.0；Whisper 代码 MIT/权重 Apache-2.0；CosyVoice2 Apache-2.0 |
| 10 | Melo 训练数据/说话人授权 | ⚠️ | **SinCircle** | 法务 | MIT 只覆盖代码/权重，训练数据与音色授权单独确认 |
| 11 | LiveKit Server | ⚠️ | **天命** | cx、赛事/法务接口 | 服务端锁版本，核实 Apache-2.0 + NOTICE |
| 11b | LiveKit 客户端 SDK / aiortc | ⚠️ | **cx** | 天命 | 客户端锁版本；aiortc 实际未安装，补安装说明 |
| 12 | coturn | ⚠️ | **cx** | 天命 | 锁版本；审 OpenSSL/libevent 传递依赖 |
| 13 | FunASR / CosyVoice | ⚠️ | **SinCircle** | 湖中日月 | 接入时逐模型核实代码+权重；目前未引入 |
| 14 | W4A16 量化派生 | ✅ | **SinCircle** | 天命 | 保留 base Apache-2.0、量化说明、校准来源 |
| 15 | LLM Provider / API / 云端 Realtime | ⚠️ | **天命** | pseudonym、赛事/法务接口 | Provider 路由/成本/服务条款由天命；模型与数据边界由 pseudonym 会签 |
| 15b | Prompt / Function Calling / Agent 依赖 | ⚠️ | **pseudonym** | 天命 | 记录 Prompt 资产、工具依赖、数据边界 |
| 16 | 评测/Judge/RSI 外部依赖 | ⚠️ | **湖中日月** | 天命 | 记录 judge 服务/数据来源许可；合成数据声明 |
| 17 | 前端第三方依赖 | ✅ | **cx** | — | 当前零依赖；新增时更新 THIRD_PARTY |

## 4. 交付包责任

- [ ] `THIRD_PARTY.md` 总维护：**天命**
- [ ] MIT/BSD/Apache LICENSE/NOTICE 收集：**天命**
- [x] libsndfile LGPL 文本 + 替换/重链接说明：**天命**（已落 `licenses/`）
- [x] GCC runtime/libgomp1 例外文本：**天命**（已落 `licenses/`）
- [ ] 模型卡链接：sherpa ASR、Melo、Qwen3-TTS、MiniCPM-o、W4A16：**SinCircle**
- [ ] Moshi 若使用：CC-BY-4.0 署名与修改说明：**SinCircle**
- [ ] 参考音频/录音授权凭证：**SinCircle**
- [x] NVIDIA 组件声明：**天命**（已落 `licenses/NVIDIA-CUDA-NOTICE.md`；再分发权待确认）
- [ ] LiveKit Server/coturn：**天命**；LiveKit 客户端 SDK/aiortc：**cx**
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
最大缺口：sherpa ASR 权重许可缺失（SinCircle 主责）
最高义务风险：Moshi CC-BY、Freeze AUP、NVIDIA EULA、libsndfile LGPL
交付包总责任：天命
模型/语音许可：SinCircle
WebRTC/媒体许可：cx
LLM 条款：pseudonym
评测数据：湖中日月
```

## 7. 交付形态前提

本清单按“仓库源码 + Docker/二进制交付”双形态检查。`docs/09` 默认模型烘进镜像，但 **sherpa ASR 权重许可未确认前不得烘进可分发镜像**；若只交源码/让部署方自行下载，需在交付说明中写清。

在 sherpa ASR 权重许可未确认前，不建议把该权重作为“可商用/可再分发”资产对外交付。
