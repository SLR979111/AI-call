# GCC Runtime / libgomp1 交付说明

本项目 Docker 镜像安装 `libgomp1`；GPU sidecar/NumPy 还可能携带 `libgfortran`。二者属于 GCC Runtime Library。

## 许可

**GPL-3.0-or-later WITH GCC-exception-3.1**。例外全文见同目录 `GCC-RUNTIME-EXCEPTION-3.1.txt`。

## 交付义务

- GCC Runtime Exception 允许独立程序按自选条款分发，不要求自有代码 GPL；
- 分发 libgomp1/libgfortran 二进制时：
  - 保留 GPL/例外文本；
  - 提供对应 GCC runtime 源码，或有效期至少三年的书面要约；
  - 不得削弱/删除例外；
- 修改 runtime 或静态链接时逐案复核。

## 当前来源

- Dockerfile `apt-get install ... libgomp1`；
- NumPy/OpenBLAS 可能携带 `libgfortran`；
- 实际镜像版本：`libgomp1 14.2.0-19`（app/qwen 镜像）；未安装 libgfortran。

> 本说明不是法律意见；最终条款以 GCC Runtime Exception 原文为准。
