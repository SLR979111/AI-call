# Qwen3-TTS 模型许可与资产说明

> 本文件只记录资产来源与许可事实，不构成法律意见。

## 模型

- 模型：Qwen3-TTS-12Hz-1.7B-Base
- 用途：Qwen TTS sidecar（`services/qwen_tts`）
- 发布资产：GitHub Release `weights-v1`
  - `qwen3-tts-1.7b-base.tar.gz.part0`
  - `qwen3-tts-1.7b-base.tar.gz.part1`
  - `qwen3-tts-1.7b-base.tar.gz.part2`
  - `SHA256SUMS`
- 解压目标：`models/qwen`
- 镜像策略：不 COPY 模型权重，只读挂载
- 许可：以模型卡为准（Model Card 标注 Apache-2.0）；交付前由 SinCircle 保存模型卡链接与许可原文

## 参考音频

- 路径：`data/voice/reference.wav`
- 转写：`QWEN_REF_TEXT`
- 授权声明：`QWEN_REFERENCE_CONSENT=1`
- 要求：必须是已取得录音/音色授权的声音，逐字转写与音频一致
- 现状：**未提供**，因此 qwen 服务不能启动

## 当前结论

```text
代码/镜像：已就绪
权重：下载中，解压后 4.54GB
参考音频/逐字转写/授权：未就绪
P0：授权音频未到位前不得对外启用 qwen 音色
```
