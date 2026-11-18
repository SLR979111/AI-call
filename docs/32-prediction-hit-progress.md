# 32 · 预测命中优化进度（WIP）

> 分支：`feature/decision-rules`  
> 状态：WIP，尚未合并，尚未最终跑完场景对比。

## 已完成

### 1. 短 decision 模型

- `new/app/decision_model.py`
- 只在规则返回 `llm` 后调用
- 只输出候选 id 或 `generate`，不生成新回复
- 默认超时 900ms
- 已接入 `new/app/brain.py` 和 `new/app/main.py`

真实短判断请求实测：

```text
首 token：60–116ms
完整 JSON：715–902ms，中位约 810ms
```

### 2. 工单短语本

- `new/app/ticket_phrases.py`
- 从 `TaskCard` 的 title/goal/facts/checkpoints/opening 生成短语
- 支持短肯定/否定和短英文
- 在精确规则失败后做宽泛匹配

### 3. 12 条 JD 基准结果

同一套 12 条输入，文本输入到正文开始播放：

| 版本 | 复用 | 规则复用 | decision 模型复用 | 总 P50 | 总 P95 |
|---|---:|---:|---:|---:|---:|
| PR20 | 6/12 | — | — | 1321.5ms | 2092.2ms |
| 当前分支，无 decision | 4/12 | 1 | 0 | 986.5ms | 1470.6ms |
| 当前分支，接 decision | 5/12 | 1 | 3 | 1220.5ms | 1940.3ms |

复用的拆分：

```text
PR20：6/12
当前无 decision：4/12
当前 decision：5/12
  rule：1
  decision_model：3
  model：1
```

结论：

```text
decision 模型把命中从 4/12 提到 5/12
但串行调用让总 P50 从 986.5ms 退到 1220.5ms
复用轮 P50 从 102.5ms 退到 657ms
```

### 4. 宽泛规则已写但未完成基准

`context_aware_decision.py` 新增：

```text
_broad_match
工单短语匹配
短英文
```

12 条 benchmark 已启动但因工具中断没有完整结果，不能算完成。

## 下一步

1. 重启 8190 加载宽泛规则，重跑 12 条 JD。
2. 与 PR20、当前无 decision、当前 decision 三组对比。
3. 如果 decision 串行仍拖慢，考虑：
   - decision 只在高语义候选时调用
   - decision 与完整生成并行，先返回者生效
   - 本地小模型替代 DeepSeek 短调用
4. 增加官方工单、网络宽带、强对抗场景，每场景 6–12 条。
5. 分别输出：
   - 规则命中率
   - decision 模型命中率
   - 总复用率
   - 复用/不复用/总的 P50、P95
6. 直到命中率不再提升或用户喊停。

## 注意

- 合规/缓存审计 PR 在 `feature/compliance-cache`，已推送，不包含预测改动。
- 预测改动在 `feature/decision-rules`（本分支）。
- 本文件是 WIP 记录，不是最终结论。
