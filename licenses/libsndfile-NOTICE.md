# libsndfile / SoundFile 交付说明

本项目通过 Python `soundfile` 调用 libsndfile，处理参考音频与 wav 读写。

## 许可

libsndfile 采用 **LGPL-2.1-or-later**。全文见同目录 `libsndfile-LGPL-2.1.txt`。

## 交付义务

- 本项目的自有代码不因动态链接 libsndfile 而需要改为 LGPL；
- 分发 Docker 镜像/二进制包时，必须：
  - 附 LGPL-2.1 全文与版权声明；
  - 说明使用了 libsndfile 及其版本；
  - 允许接收方替换/重新链接修改后的 libsndfile；
- 如果修改 libsndfile 本身，需提供修改后的对应源码；
- 不得删除许可证与版权声明。

## 当前来源

- Docker 基础镜像 apt `libsndfile1`（见 `services/qwen_tts/Dockerfile`）；
- `soundfile` wheel 可能自带 `_soundfile_data/libsndfile*`；
- 具体版本待交付镜像锁定后回填。

## 替换/重链接

交付镜像保留系统动态库方式；接收方可用同 ABI 的 libsndfile 替换系统库。若改为静态链接，需重新评估 LGPL 义务。

> 本说明不是法律意见；最终条款以 LGPL-2.1 原文为准。
