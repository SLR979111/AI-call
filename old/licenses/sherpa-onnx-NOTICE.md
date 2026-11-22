# sherpa-onnx 交付说明

本项目使用 sherpa-onnx 2.0.1 运行 ASR/TTS。

## 许可

sherpa-onnx 代码/运行时采用 **Apache-2.0**。全文见同目录 `sherpa-onnx-LICENSE`。

## 注意

- wheel 的 `dist-info` 带有 Apache-2.0 LICENSE；
- 当前 wheel 未附带独立第三方 NOTICE/第三方清单；
- 若交付包包含 sherpa-onnx，保留 LICENSE 与版权声明；
- **ASR 模型权重不是 sherpa-onnx 代码的一部分**，其许可证仍未确认，不能因为本文件而视为已授权。

## 模型权重

见 `THIRD_PARTY.md` 的 “sherpa Zipformer 中文 ASR 权重” 行；在许可确认前不随镜像再分发。
