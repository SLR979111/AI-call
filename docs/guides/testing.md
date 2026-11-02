# 测试与比赛指标

> 测试分四层：单元/契约、前端协议、端到端、比赛指标。CI 每次 push 全跑。

## 1. 测试分层

| 层 | 命令 | 覆盖 | 当前 |
|---|---|---|---|
| 单元 + 契约 | `make test` | 编排、工具、合规、Registry、RSI、RAG、LLM Mock、WS | 21 passed |
| 契约 Schema | `make contract-test` | 5 个 fixture / 90 条文档 | PASS |
| 前端协议 | `cd web/rtc && npm test` | golden 回放、PCM、文件回灌、打断语义 | 4 组 PASS |
| 端到端 | `make e2e` | 2 轮 + 打断 + 挂断 | PASS |
| 比赛指标 | `make metric` | 3 轮闭环、P50、任务成功率、合规、打断 | 13 项 PASS |

CI：`.github/workflows/ci.yml` 在 Python 3.12 上跑 pytest → 契约 → 前端 4 组 → WS E2E。

## 2. 比赛指标检查点

| # | 检查点 | 门槛 | 当前实现 |
|---|---|---|---|
| 1 | 至少 3 轮闭环 | `>= 3` | `/eval/sessions` 3 轮 |
| 2 | 任务成功 | `outcome` 为 rescheduled/completed | Fake 100% |
| 3 | 业务结果回写 | `TurnCommitted.result` 带 `new_time/ticket` | Fake 模拟；真实写回待接 |
| 4 | 首字 P50 | `<= 2000 ms` | 883 ms（Fake 代理） |
| 5 | 首字 P95（观察） | `<= 3000 ms` | 887 ms |
| 6 | 任务成功率 | `>= 80%` | 100%（5 cases） |
| 7 | 合规检查发生 | `> 0` | 2 次 |
| 8 | 回复不泄露 PII | 不含敏感原文 | 无泄露 |
| 9 | 打断停止 | `<= 100 ms` | 20 ms（客户端上报） |
| 10 | 服务端确认 | `<= 2000 ms` | 212 ms |
| 11 | 打断 reason | `BARGE_IN` | PASS |

> 指标 4/9 是 **Fake/协议级**：真实声学首字与真实 VAD 要等 ASR/TTS 接入后复跑。指标 9 的 20ms 是客户端上报值，不是声学测量。

## 3. 分段执行

```bash
# 全量（默认，服务需已启动）
make metric

# 单段
make metric-closure
make metric-latency
make metric-task
make metric-compliance
make metric-barge

# 指定其他服务
make metric METRIC_BASE=http://127.0.0.1:8082
```

底层脚本：

```bash
python scripts/metric_check.py --base http://127.0.0.1:8081 --section all
python scripts/metric_check.py --section barge --md-out reports/metric-barge.md
```

输出：

- 控制台：逐项 PASS/FAIL + 实测值
- `reports/metric-check.md`：Markdown 表格 + 原始指标
- `--json-out`：机器可读 JSON
- 退出码：有 FAIL 为 1；适合 CI/一键检查

## 4. 最近一次结果

`reports/metric-check.md`（2026-11-02）：

```text
closure  3 轮 / 成功 / order.query / 回写 new_time+ticket   PASS
latency  P50 883 ms / P95 887 ms（Fake）                    PASS
task     100%（5 cases）                                    PASS
compliance 检查 2 次 / 无泄露                               PASS
barge    20 ms 停止 / 212 ms 服务端确认                     PASS
```

## 5. 待补的真实指标

- 真实 CPU ASR/TTS 接入后，替换 `latency` 的 Fake 代理值；
- 真实 VAD 后，`barge` 的停止时延改为麦克风实测；
- 真实 LLM 后，`task` 换成真实云模型评测集；
- 真实工单写回后，把“业务结果回写”升级为 `order.reschedule`/`ticket.create` 的 `ToolReturned` 校验。

## 6. 测试数据与边界

- `data/` 是运行时事件日志，已 gitignore；报告里的 session 可回放。
- 不把真实手机号/身份证写入仓库；测试使用合成 PII 字符串。
- 真实模型/Key 不进仓库，用 `.env` 或环境变量注入。
