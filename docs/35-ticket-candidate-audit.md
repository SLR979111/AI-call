# 35 · ticket 候选多轮审计与剔除记录

> 分支：`integration/prediction-rules`  
> 审计输入：拟合集 + 未参与拟合的留出集  
> 结论：只提出剔除建议，不默认剔除。

## 审计方法

```text
generate_ticket_candidates(card)
→ ContextAwareDecisionEngine.should_reuse
→ 统计每个 ticket intent 被选中的次数
→ 拟合集和留出集都为 0 才建议剔除
```

审计脚本：

```text
python -m app.ticket_audit --task tasks/xxx.json --fit fit.txt --heldout heldout.txt
```

## 结果

| 场景 | ticket 数 | 拟合集 0 命中 | 留出集 0 命中 | 两边都 0，建议剔除 |
|---|---:|---|---|---|
| 京东 | 9 | how_to_submit, in_review, saved | how_to_submit, missing_material, submitted, timing | how_to_submit |
| 官方工单 | 5 | 无 | handoff, how_to_submit | 无 |
| 宽带 | 5 | how_to_submit, timing | how_to_submit, timing | **how_to_submit, timing** |
| 网络 | 5 | how_to_submit, timing | how_to_submit, timing | **how_to_submit, timing** |
| 物流 | 4 | timing | timing | **timing** |
| 发票 | 4 | 无 | invoice_header, timing | 无 |

## 为什么不全局剔除

`how_to_submit` 在京东/官方工单的拟合集有命中：

```text
官方工单 fit: how_to_submit 1 次
```

如果全局设置：

```text
TICKET_FORECAST_EXCLUDE=how_to_submit,timing
```

会误伤官方工单。

所以新增按卡剔除：

```text
TICKET_FORECAST_EXCLUDE_BY_TASK='{"京东标题":["how_to_submit"],"宽带标题":["how_to_submit","timing"]}'
```

实现位置：

```text
new/app/ticket_forecast.py
```

## 当前建议

先不写默认剔除，作为实验臂：

```text
JD：how_to_submit
宽带：how_to_submit, timing
网络：how_to_submit, timing
物流：timing
```

只有：

```text
拟合集 0
留出集 0
新留出仍 0
```

才进入默认配置。

## 已知 caveat

```text
审计输入是固定文本，不是真实通话
规则引擎选不到不等于模型候选没价值
ticket 候选被截断也会影响统计
剔除后必须重跑规则命中，确认不下降
```
