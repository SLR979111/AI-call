# 32 · 预测命中优化进度（WIP）

> 分支：`feature/decision-rules`  
> 状态：WIP，未合并，未 push。

## 已完成

### 1. 短 decision 模型

- `new/app/decision_model.py`
- 只在规则返回 `llm` 后调用
- 只输出候选 id 或 `generate`
- 超时 900ms
- 已接入 `brain.py` / `main.py`

真实短请求：

```text
首 token：60–116ms
完整 JSON：715–902ms，中位约 810ms
```

### 2. 工单短语本

- `new/app/ticket_phrases.py`
- 从 TaskCard 的 title/goal/facts/checkpoints/opening 生成短语
- 中文/英文短答
- 工单常见问题规则

### 3. 规则与 decision 迭代结果

12 条 JD 固定输入，文本输入到正文开始播放：

| 版本 | 复用 | 规则复用 | decision 复用 | 总 P50 | 总 P95 |
|---|---:|---:|---:|---:|---:|
| PR20 | 6/12 | — | — | 1321.5ms | 2092.2ms |
| 当前，无 decision | 4/12 | 1 | 0 | 986.5ms | 1470.6ms |
| 当前，decision | 5/12 | 1 | 3 | 1220.5ms | 1940.3ms |
| 当前，宽泛规则 + decision | **6/12** | **2** | **4** | **44.5ms** | **1176.0ms** |

当前最好版本的分组：

```text
复用 6/12：
  rule 2/12
  decision_model 4/12

不复用 6/12
```

复用轮延迟：

```text
P50：约 42ms
P95：约 50ms
```

说明：

```text
命中率追平 PR20
总 P50/P95 明显低于 PR20
规则命中仍然只有 2/12，主要靠 decision 模型
```

### 4. 还没跑的场景

```text
官方工单
网络宽带
强对抗
```

需要同一套固定输入跑 PR20 和当前分支，再统计规则/decision/总 P50/P95。

## 下一步

1. 用工单短语本生成**预测候选**，不只是匹配用户输入，提升规则/候选覆盖率。
2. 跑官方工单、网络宽带、强对抗场景。
3. 对比规则命中、decision 命中、总命中、P50/P95。
4. 如果 decision 串行仍拖慢，考虑与完整生成并行。
5. 直到命中率不再提升或用户喊停。

## 分支边界

- 合规/缓存审计 PR：`feature/compliance-cache`，已 push。
- 预测优化：`feature/decision-rules`，未 push。
