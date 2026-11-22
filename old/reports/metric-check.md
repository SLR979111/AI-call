# 比赛指标自检

- 时间：2026-11-06T17:50:08+08:00
- 服务：`http://127.0.0.1:8083`
- 范围：`all`
- 结论：**PASS**

| 部分 | 检查点 | 结果 | 实测 | 门槛 | 备注 |
|---|---|---|---|---|---|
| closure | 至少 3 轮 | ✅ PASS | 3 轮 | >= 3 |  |
| closure | 任务成功 | ✅ PASS | True | true | outcome=rescheduled |
| closure | 工具调用返回 | ✅ PASS | order.query | 至少 1 次 ToolReturned |  |
| closure | 业务结果回写 | ✅ PASS | {"new_time": "2026-11-01T15:00", "ticket": "pending"} | TurnCommitted 带 new_time/ticket | Fake 模拟；真实工单写回待接 |
| latency | 首字 P50 | ✅ PASS | 1113.82 ms | <= 2000 ms |  |
| latency | 首字 P95（观察项） | ✅ PASS | 1120.45 ms | <= 3000 ms |  |
| task | 任务成功率 | ✅ PASS | 100% (6 cases) | >= 80% |  |
| compliance | 合规检查发生 | ✅ PASS | 4 次 | > 0 |  |
| compliance | 回复不复述敏感信息 | ✅ PASS | leaked=False | false | BLOCK/REWRITE 规则本身由 pytest::test_compliance_rewrite_and_block 覆盖 |
| barge | 收到 turn_aborted | ✅ PASS | True | true |  |
| barge | reason=BARGE_IN | ✅ PASS | BARGE_IN | BARGE_IN |  |
| barge | 客户端停止时延 | ✅ PASS | 20.0 ms | <= 100 ms |  |
| barge | 服务端确认时延 | ✅ PASS | 216.7 ms | <= 2000 ms |  |

## 原始指标

```json
{
  "closure": {
    "session_id": "s_01M6R47DRC93CV8B21BEDYZ55V",
    "turns": 3,
    "outcome": "rescheduled",
    "tools": [
      "order.query"
    ],
    "business_result": {
      "new_time": "2026-11-01T15:00",
      "ticket": "pending"
    }
  },
  "latency": {
    "samples": 9,
    "p50_ms": 1113.82,
    "p95_ms": 1120.45,
    "session_ids": [
      "s_01M6R47H0E8GKR4P5C4GMNBDWY",
      "s_01M6R47M779E4AHR4ZX89TAV7V",
      "s_01M6R47QF18KMRZDGQHAMGMMF5"
    ],
    "note": "Fake/协议代理指标，不是声学实测"
  },
  "task": {
    "cases": 6,
    "task_success_rate": 1.0,
    "p50_ms": 675.31,
    "p95_ms": 678.97,
    "failure_counts": {}
  },
  "compliance": {
    "checked": 4,
    "blocked": 0,
    "rewritten": 0,
    "leaked": false,
    "note": "本段验证服务端不泄露；规则级 BLOCK/REWRITE 见 pytest 单测"
  },
  "barge": {
    "session_id": "s_01M6R47ZA99RV879688WZT6YQA",
    "reported_stop_ms": 20.0,
    "server_ack_ms": 216.7,
    "reason": "BARGE_IN",
    "note": "stop 为客户端上报，协议级验证；真实 VAD 待 W3"
  }
}
```

> Fake/协议级结果；真实 ASR/TTS 接入后请用同一脚本复跑。
