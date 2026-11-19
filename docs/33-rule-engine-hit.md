# 33 · 通用工单规则引擎命中优化

> 分支：`feature/rule-engine`  
> 范围：只改规则引擎，不接 decision 模型，不改预测链路。

## 目标

```text
按 TaskCard 自动生成宽泛短语本
补中文短答和短英文
支持否定/改口
用固定场景回归规则命中
支持离线多轮拟合同一工单的不同问法
```

## 已实现

### 1. 工单短语本

`new/app/ticket_phrases.py`

- 从 title / goal / facts / opening / checkpoints 提取词
- 对长中文事实补充 3–4 字片段
- 输出 ticket-specific phrases

### 2. 规则候选

```text
精确规则先判
工单短语匹配
用户 2–5 字片段和候选 when/examples/reply 对齐
唯一候选才复用
多个候选按分数和差距判断
```

### 3. 短答

- 中文：好的 / 行 / 可以 / 明白 / 收到 / 没问题等
- 短英文：ok / okay / yes / sure / fine / got it / no / nope / not now
- 修正“是不是/行不行”被当成否定的问题

### 4. 否定和对抗

- 明确否定、改口、换人、投诉、拒绝不触发复用
- 规则命中不了时仍返回 `llm`，不硬猜

### 5. 多场景回归

新增：

```text
new/tests/test_rule_engine_scenarios.py
new/tests/test_rule_engine_variants.py
new/tasks/official-work-order.json
```

覆盖：

```text
京东资质
宽带支持
网络套餐
官方工单
强对抗
短英文
```

## 验证

```text
35 passed（规则场景 + 变体 + 原匹配测试）
113 passed（new 全量离线测试）
```

测试检查的是规则命中/不误命中，不跑模型、不跑 ASR。

## 关于“多跑几轮模拟同一工单多次出现”

可以，但要区分：

```text
重复跑同一输入
→ 规则不会自己学习
→ 只是回归，不会提升

重复跑不同问法，再把失败问法加入短语本/变体集
→ 相当于离线拟合
→ 能提升规则召回
```

正确做法：

```text
训练/拟合集：同一工单的多种问法
留出集：没参与调试的新问法
只在留出集也命中，才算泛化
```

否则就是把测试集背下来，真实命中率会虚高。

## 未做

```text
decision 模型
prediction 改动
语音 P50/P95
PR20 对比
```

这些不在本分支范围内。
