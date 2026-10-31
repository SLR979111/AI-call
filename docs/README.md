# 智语外呼 · 架构工作台

> 语音外呼客服数字员工：实时语音交互 + 任务型 Agent + RSI 自进化
> 当前阶段：**W2 模块补齐 / 契约 v0.2 待 cx 复签**。目标不是写功能，是让四个人的代码跑在同一条会话里。

---

## 1. 当前结论（给团队的一句话）

> 现在最大的系统阻塞不是模型、不是 ASR/TTS，而是**没有一份可执行的接口契约和一条能跑通的 Fake 全链路**。
> 先冻结契约，再用 5 天把前端/语音/Agent/评测接到同一条主线上；否则三周会被联调吃光。
>
> 另一个独立风险：**提交物不是架构的附属品**。技术方案书、自测报告、测试集录音、开源清单、5 分钟视频、3+7 分钟演示/答辩，都需要从 W1 并行产出，见 [08](08-submission-package.md)。

---

## 2. 文档索引

| 文档 | 内容 | 状态 |
|---|---|---|
| [`reference/competition-brief.txt`](reference/competition-brief.txt) | 赛题书全文（PDF 提取） | 已归档 |
| [`01-requirement-metric-module-matrix.md`](01-requirement-metric-module-matrix.md) | 需求—指标—模块—负责人矩阵；指标字典；门槛覆盖 | v0.1（角色为能力代号） |
| [`02-architecture-v0.1.md`](02-architecture-v0.1.md) | 模块划分、依赖规则、时序、接口签名、部署、选型、延迟可行性 | v0.1 |
| [`03-interface-contract-v0.1.md`](03-interface-contract-v0.1.md) | **契约包**：ID/时间/音频/错误/取消、Agent/Tool/Voice/Registry/Eval 接口、Flow Pack、合规、契约测试 | **待会签** |
| [`04-event-schema-v0.1.md`](04-event-schema-v0.1.md) | Event Envelope、事件字典、`TurnLatency`、客户端埋点、存储与回放、指标映射 | **待会签** |
| [`05-week1-integration-plan.md`](05-week1-integration-plan.md) | D0–D5 集成主线、媒体路径决策、每周 Gate、风险触发器 | 执行中 |
| [`06-gpu-inference-plan.md`](06-gpu-inference-plan.md) | 6×3090 分配、ASR/TTS 本地服务、远程可达性、容量与降级 | **本地语音主路径** |
| [`07-cloud-api-plan.md`](07-cloud-api-plan.md) | LLM 云 API + 本地语音：Router、延迟/成本、故障降级 | **当前主路径** |
| [`08-submission-package.md`](08-submission-package.md) | 官方提交物拆解、评分证据、W1–W3 生产计划、视频/答辩脚本、合规红线 | **待执行** |
| [`09-delivery-runtime.md`](09-delivery-runtime.md) | 交付 Docker：CPU-only 目标环境、sherpa-onnx 语音、模型打包、Compose、HTTPS/WebRTC、离线降级 | **交付主路径** |
| [`10-master-summary.md`](10-master-summary.md) | 总汇总：整体架构、API 速查、团队工作清单、天命个人清单、里程碑 | **先看这个** |
| [`11-contract-v0.2-patch.md`](11-contract-v0.2-patch.md) | 回应 cx 评审：G1–G9 补丁、12 条消息、枚举/鉴权/降级/b3 | **v0.2 生效口径** |
| [`reply_cx_v0.2.1.md`](reply_cx_v0.2.1.md) | 给 cx 的 C1–C6 回复（可直接转发） | 待发送 |
| [`12-technical-proposal-draft.md`](12-technical-proposal-draft.md) | 技术方案书初稿：架构/时延/评测/RSI/成本/分工 | **初稿，待 W3 实测替换** |
| [`contracts-signoff.md`](contracts-signoff.md) | 契约会签表、自动检查命令、已确认事实 | 待四位 owner 签字 |
| [`../README.md`](../README.md) | 代码骨架：本地运行、测试、Docker、端点 | ✅ Fake 骨架可跑 |

> ⚠️ `01/02` 里的 A–E 是「能力角色代号」，与《三周分工》的成员代号不同。**从 03 起一律用姓名**，以 03 的角色映射为准。

---

## 3. 角色映射

| 成员 | 三周分工 | 架构模块 | 契约角色 |
|---|---|---|---|
| 天命 | A 后端/集成 | M3 编排器、M5 Tool Gateway、M7 Trace、M9 Registry、媒体服务 | 契约 Owner；唯一取消/发布者 |
| SinCircle | B 实时语音 | M2 VAD/ASR/TTS 适配器 | 语音接口提供方 |
| pseudonym | C Agent | M4 Flow Engine + LLM + RAG + 工具决策 | Agent 接口与 Flow Pack |
| 湖中日月 | D 评测/RSI | M8 评测、归因、Candidate | EvalRunner 与合规测试集 |
| cx | E 前端/材料 | M10 控制台、WebRTC 客户端 | 控制面消息与播放埋点 |

合规是共享责任：pseudonym 出规则，天命接调用点，湖中日月出测试，cx 做展示。

---

## 4. 阶段看板

| 阶段 | 交付物 | 状态 | Gate |
|---|---|---|---|
| 1 | Requirement–Metric–Module Matrix | ✅ v0.1 | 条件通过（缺人名/官方口径） |
| 2 | Architecture v0.1 | ✅ v0.1 | 条件通过（契约细节待补） |
| 3 | Module Responsibility | ✅ 合入 03 第 1 节 | 已用姓名重写 |
| 4 | Interface Contract v0.1 | ✅ 待会签 | 四位 owner 能跑契约测试 |
| 5 | Event Schema v0.1 | ✅ 待会签 | 事件字典 + 顺序断言 |
| 6 | Latency Budget v0.1 | ⏳ W1 末用真实数据校准 | 每阶段有 owner 与验证 |
| 7 | Failure Model v0.1 | ⏳ W2 | 每类失败有检测/降级/恢复 |
| 8 | Minimum E2E Skeleton | ✅ v0.1（Fake） | 3 轮 + 回放 + `TurnLatency` + Tool Gateway 已跑通 |

---

## 5. 已冻结的架构决策

1. **模块化单体**，单进程 asyncio，进程内事件总线；不引入 MQ/微服务/K8s。
2. **单一编排者**：只有天命（M3）启动/取消轮次、写会话状态；音频块带 `turn_id` 隔离。
3. **双状态机分离**：M3 管运行时，M4 管业务（Flow Pack）。
4. **Agent 拆分**：Flow/Context/LLM 在 M4；RAG 在 M4；工具执行在 M5；合规独立。
5. **合规三层**：生成前约束 → 句级门禁 → 工具守卫；BLOCK 必须能中断 TTS。
6. **Flow Pack**：流程/槽位/话术/合规/工具绑定配置化，换场景不改代码。
7. **版本注册中心**：prompt / flow / knowledge / model / config 五类工件；session 固定 pin。
8. **客户端本地 VAD 立即静音**，服务端取消异步补偿（打断 ≤300ms 的前提）。
9. **媒体路径：WS 骨架先行，W2 换 WebRTC**；控制面 JSON 不变，音频切轨。
10. **评测与 RSI 只通过 registry 影响生产**：候选 → 回归 → 晋升/回滚。
11. **LLM 云 + 语音本地**：LLM 走云 API（Router 做多供应商/超时/熔断/成本上限）；ASR/TTS/Embedding/Rerank/Judge 在 6×3090 本地；本地 14B 仅作断网降级。
12. **交付运行时 CPU-only**：现场跑在主办方设备的 Docker 上，不依赖 3090/隧道；ASR/TTS 用 sherpa-onnx CPU，LLM 云；6×3090 只做开发/评测。

---

## 6. 三周硬门槛

| 门槛 | 目标 | 当前状态 |
|---|---|---|
| 首字 P50 | ≤2s（官方）；内控 P50 1.2s / P95 2s | 待 D3 出第一版数据 |
| 连续对话 | ≥3 轮；10 轮稳定 | 待 D5 |
| 完整闭环 | 六阶段 + 结构化回写 | 待 W2 |
| 合规 | 严重违规 0；必说 100%；禁语召回 100% | 待 D4 |
| 一键启动 | 干净机 ≤10min | 待 W2 |
| RSI | 1 组候选 vs 基线，门槛不回退 | 待 W3 |

---

## 7. 待确认

- [ ] 官方时延口径（`t0` 是用户最后一个有声帧还是 VAD 端点）——官方资料未发放
- [ ] 场景资料与 Mock 接口发放时间——未知，先按通用服务外呼 Flow Pack 做
- [ ] 云 LLM 账号/Key/配额/预算；是否至少两家
- [ ] GPU 主机位置、能否从赛场网络访问、是否独占（**现在语音本地，已是 P0**）
- [ ] 是否需要云 ASR/TTS 作为 GPU 整体不可用的应急适配器
- [ ] 三周后的提交/答辩具体日期

---

## 8. 工作约定

- 任何设计变更先改契约，再改代码；破坏性修改提前 24h 通知。
- 每个模块必须能脱离真实依赖单测（Fake ASR/Agent/TTS/Tool）。
- 每周一次集成 + 冒烟 + 3 分钟演示；日常异步同步，不设每日站会。
- 不提交密钥、真实号码、未脱敏数据。
