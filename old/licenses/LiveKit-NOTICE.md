# LiveKit 许可与版本说明

> 本文件只记录实际使用的版本、来源与许可事实，不构成法律意见。

## 服务端

- 组件：`livekit/livekit-server`
- 本地镜像标签：`livekit/livekit-server:latest`
- 实际运行版本：1.13.11
- 许可：Apache-2.0
- 来源：<https://github.com/livekit/livekit>
- 用法：Docker `--dev`，端口 7880/7881/7882
- 修改：未修改服务端源码
- 交付说明：正式交付必须固定 digest/tag；Docker 镜像再分发时随附 Apache-2.0 文本与 NOTICE（如上游提供）

## 客户端

- 组件：`livekit-client`
- npm 版本：2.23.0
- 仓库内文件：`web/rtc/vendor/livekit-client.esm.mjs`
- 许可：Apache-2.0
- 来源：<https://github.com/livekit/client-sdk-js>
- 修改：未修改；直接 vendored 供浏览器 ESM 懒加载
- 交付说明：随包保留本说明与 Apache-2.0 文本

## 未使用/未部署

- `aiortc`：当前 `--rtc-pcm` 路径未启用，交付说明中标注为可选，不安装。
- `coturn`：当前 LiveKit dev 环境未部署，正式 TURN 上线前单独补版本、许可与配置说明。

## 当前结论

```text
LiveKit Server：已使用 1.13.11
LiveKit 客户端：已使用 2.23.0，vendored
许可证材料：本说明已落，Apache-2.0 全文需随正式交付包补齐
```
