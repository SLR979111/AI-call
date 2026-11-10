# 第三方许可证原文与声明

本目录保存交付包需要随附的第三方许可证原文/声明。完整组件清单见 [THIRD_PARTY.md](../THIRD_PARTY.md)，交付行动项见 [docs/20-license-checklist.md](../docs/20-license-checklist.md)。

| 文件 | 覆盖组件 | 许可证/性质 | 义务 |
|---|---|---|---|
| `sherpa-onnx-LICENSE` | sherpa-onnx 2.0.1 | Apache-2.0 | 保留 LICENSE/NOTICE |
| `onnxruntime-ThirdPartyNotices.txt` | onnxruntime 1.28.0 | MIT + 第三方 notices | 随包保留 |
| `libsndfile-LGPL-2.1.txt` | libsndfile / SoundFile | LGPL-2.1-or-later | 动态链接、附许可、允许替换/重链接 |
| `libsndfile-NOTICE.md` | libsndfile 交付说明 | LGPL 义务说明 | 与上者一起分发 |
| `GCC-RUNTIME-EXCEPTION-3.1.txt` | libgomp1 / libgfortran | GPL-3.0 WITH GCC-exception-3.1 | 保留例外文本、提供对应源码/要约 |
| `GCC-RUNTIME-NOTICE.md` | GCC runtime 交付说明 | 例外义务说明 | 与上者一起分发 |
| `NVIDIA-CUDA-NOTICE.md` | PyTorch cu128 / CUDA / cuDNN | NVIDIA 专有 EULA | 不附 EULA 原文；确认再分发权 |

## 交付前

- `THIRD_PARTY.md` 与 `docs/20-license-checklist.md` 必须随包；
- 本目录所有文件必须随包；
- NVIDIA 组件若只随镜像分发，需先确认 NVIDIA EULA 再分发条款；
- 修改 libsndfile/libgfortran 时，需按 LGPL/GCC exception 提供对应源码或书面要约；
- 模型权重不因本目录的存在而自动获得再分发许可；ASR 权重许可仍未确认。
