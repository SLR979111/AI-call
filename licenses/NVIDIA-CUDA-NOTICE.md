# NVIDIA CUDA / cuDNN 交付声明

本项目 GPU sidecar 使用 PyTorch cu128 轮子，会引入 NVIDIA CUDA/cuDNN/NCCL 等专有运行时。

## 性质

这些组件**不是开源许可证**，受 **NVIDIA 软件许可协议（EULA）** 约束。本仓库不复制 EULA 全文。

官方入口：

- https://docs.nvidia.com/cuda/eula/
- https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html

## 交付前必须确认

1. 交付形态是源码、wheel 还是 Docker 镜像；
2. Docker 镜像再分发是否符合 NVIDIA EULA；
3. 是否需要在镜像/文档中附 NVIDIA 版权与许可提示；
4. 具体 `nvidia-*` 包版本与来源；
5. 是否允许接收方在无 NVIDIA GPU 的环境运行（CPU fallback）。

## 当前状态

- GPU sidecar 固定：`torch==2.11.0` / `torchaudio==2.11.0`，cu128 轮子；
- 来源：`https://download.pytorch.org/whl/cu128`；
- 镜像：`services/qwen_tts/Dockerfile`，基础 `python:3.12-slim`；
- 镜像内已复制 `licenses/NVIDIA-CUDA-NOTICE.md` 与 `licenses/`；
- 具体 `nvidia-*` 传递包已从镜像读取：nvidia-cublas-cu12 12.8.4.1、nvidia-cudnn-cu12 9.19.0.56、nvidia-nccl-cu12 2.28.9、nvidia-cuda-runtime-cu12 12.8.90 等；
- 未取得法务/赛事对镜像再分发的确认；
- 在确认前，不把带 CUDA 的镜像作为“可自由再分发”资产。

> 本声明不是法律意见，也不替代 NVIDIA EULA。
