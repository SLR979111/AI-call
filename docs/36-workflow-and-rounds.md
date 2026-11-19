# 36 · 规则与预测优化工作流 / 多轮记录

> 分支：`integration/prediction-rules`  
> 状态：集成分支，未合并主分支。  
> 范围：规则引擎 + 预测候选生成；不含真实语音 P50/P95 结论。

## 1. 分支与工作区

```text
feature/rule-engine
  规则引擎稳定分支

feature/compliance-cache
  合规与缓存审计，已独立推送
  不含预测/规则改动

agent/rule-opt
  规则代理隔离分支

agent/prediction-opt
  预测生成代理隔离分支

integration/prediction-rules
  当前集成分支
```

隔离方式：

```text
每个代理一个 git worktree
规则代理只改规则文件
预测代理只改候选生成/合并/审计
最后在 integration 分支合并
```

## 2. 代理分工

### 规则引擎代理

负责：

```text
工单短语本
中文短答
短英文
否定/改口
问句方面词
IDF n-gram
唯一候选/分值差
留出集回归
```

不负责：

```text
decision 模型
预测候选生成
预测音频
```

### 预测生成代理

负责：

```text
TaskCard 确定性候选族
checkpoint/intent/polarity 标签
ticket + 模型候选合并
候选配额
候选使用审计
按卡剔除开关
```

不负责：

```text
规则匹配表
decision 模型
规则话术
```

## 3. 端到端工作流

```text
1. 规则代理扩规则和留出集
2. 预测代理按 TaskCard 生成候选族
3. 规则候选 + 模型候选合并，ticket 先发布
4. 规则引擎在候选池上跑多场景
5. 统计每个 ticket intent 被选中次数
6. 拟合集 + 留出集都为 0 的 intent 标记剔除
7. 剔除后必须补新族，不能只缩小候选池
8. 默认臂 vs 剔除臂做 A/B
9. 不下降才考虑设为默认
10. 真实语音基准另跑，不混文本命中率
```

关键约束：

```text
规则不确定返回 llm，不硬猜
候选剔除必须有拟合+留出证据
全局剔除不许误伤其他场景
模板候选只从 TaskCard 生成
不编 URL、电话、承诺
```

## 4. 多轮跑分记录

### 规则第 1 轮

```text
规则场景 + 变体 + 第一轮留出
35 passed
```

### 规则第 2 轮

```text
新增第二轮留出
4 组规则测试通过
```

### 规则第 3 轮

规则代理提交 `a89efba`：

```text
新增 80 条留出
同义词归一
问句方面词门控
否定/改口/超范围请求
IDF 加权 n-gram
唯一业务词/分值差高置信
```

结果：

```text
第三轮留出 80/80
全量规则测试通过
```

### 预测第 1 轮

预测代理提交 `db08628`：

```text
ticket_forecast.py
Candidate 增加 checkpoint/intent/polarity
合并去重 + 候选上限 10
```

测试：

```text
132 passed
```

### 预测第 2 轮：配额修正

提交 `f31ed3a`：

```text
ticket 不能占满全部槽
reserve = min(4, max(1, limit // 3))
limit=10 → ticket 7 + 模型 3
```

测试：

```text
135 passed
```

### 预测第 3 轮：扩族 + 审计

提交 `b87aae8`：

```text
宽带：connectivity / slow / router
网络：price / recommend / install
官方：handoff
物流：status / contact / address
发票：type / header / status
```

新增：

```text
TICKET_FORECAST_EXCLUDE
ticket_audit.py
按 intent 统计规则选中次数
```

### 集成

提交 `264df79` / `0deb483` / `f8f141a` / `4ab982e`：

```text
规则分支 + 预测分支合并
新增物流、发票真实场景卡
per-card 剔除配置
A/B 脚本
```

集成测试：

```text
149 passed
```

## 5. 当前场景命中

规则引擎 + ticket 候选，拟合集：

| 场景 | 命中 |
|---|---:|
| 京东 | 9/12 |
| 官方工单 | 6/8 |
| 宽带 | 6/6 |
| 网络 | 6/6 |
| 物流 | 4/5 |
| 发票 | 4/5 |

留出集：

| 场景 | 命中 |
|---|---:|
| 京东 | 8/8 |
| 官方工单 | 3/3 |
| 宽带 | 4/4 |
| 网络 | 3/3 |
| 物流 | 3/4 |
| 发票 | 2/4 |

强对抗：

```text
0/6 复用（正确）
保证、投诉、换人、拒绝、新问题不命中
```

## 6. 候选审计

拟合+留出都为 0 的 intent：

| 场景 | 建议剔除 |
|---|---|
| 京东 | how_to_submit |
| 官方工单 | 无 |
| 宽带 | how_to_submit, timing |
| 网络 | how_to_submit, timing |
| 物流 | timing |
| 发票 | 无 |

不全局剔除原因：

```text
官方工单 how_to_submit 在拟合集有命中
全局剔除会误伤
```

已增加：

```text
TICKET_FORECAST_EXCLUDE_BY_TASK
```

按卡剔除，默认空。

## 7. 剔除臂 A/B

| 场景 | 默认候选 | 剔除后 | 拟合命中 | 留出命中 |
|---|---:|---:|---:|---:|
| 京东 | 9 | 8 | 9/12 → 9/12 | 8/8 → 8/8 |
| 官方工单 | 5 | 5 | 6/8 → 6/8 | 3/3 → 3/3 |
| 宽带 | 5 | 3 | 6/6 → 6/6 | 4/4 → 4/4 |
| 网络 | 5 | 3 | 6/6 → 6/6 | 3/3 → 3/3 |
| 物流 | 4 | 3 | 4/5 → 4/5 | 3/4 → 3/4 |
| 发票 | 4 | 4 | 4/5 → 4/5 | 2/4 → 2/4 |

结论：

```text
剔除不损失规则命中
但只剔除会让候选池变小
所以下一轮必须补新族
```

## 8. 剔除后补充（已完成）

原则：

```text
剔除 0 命中族
→ 补同场景的新候选族
→ 候选数量不下降
```

新增替代族：

```text
京东：upload_issue（上传/页面入口/材料重传）
宽带：device_check、optical_light、repair
网络：contract、coverage、promotion
物流：lost_or_damaged
官方、发票：未建议剔除，不补
```

每卡候选数：

| 场景 | 默认 | 剔除 | 剔除后 | baseline |
|---|---:|---:|---:|---:|
| 京东 | 10 | how_to_submit | 9 | 9 |
| 官方工单 | 5 | 无 | 5 | 5 |
| 宽带 | 8 | how_to_submit, timing | 6 | 5 |
| 网络 | 8 | how_to_submit, timing | 6 | 5 |
| 物流 | 5 | timing | 4 | 4 |
| 发票 | 4 | 无 | 4 | 4 |

剔除后 A/B：

| 场景 | 默认候选 | 剔除后候选 | 默认拟合 | 剔除拟合 | 默认留出 | 剔除留出 |
|---|---:|---:|---:|---:|---:|---:|
| 京东 | 10 | 9 | 9/12 | 9/12 | 8/8 | 8/8 |
| 官方工单 | 5 | 5 | 6/8 | 6/8 | 3/3 | 3/3 |
| 宽带 | 8 | 6 | 6/6 | 6/6 | 4/4 | 4/4 |
| 网络 | 8 | 6 | 6/6 | 6/6 | 3/3 | 3/3 |
| 物流 | 5 | 4 | 4/5 | 4/5 | 3/4 | 3/4 |
| 发票 | 4 | 4 | 4/5 | 4/5 | 2/4 | 2/4 |

结论：

```text
剔除后候选数不低于 baseline
规则命中没有下降
补族成功
```

注意：新增 JD 替代族后，2 字业务词曾丢失导致“截图发你了”退化；已通过 2 字词优先级修复。

## 9. 未完成

```text
真实语音规则/decision/未命中 P50、P95
PR20 同标准对比
补充族后的最终 A/B
合并到主分支
```

本文不声称真实语音命中率已提升；只记录候选覆盖、规则命中、留出泛化和剔除 A/B。

## 10. 复现命令

规则测试：

```text
cd new
H:\aicall\new\.venv\Scripts\python.exe -m pytest -q --disable-warnings --ignore=tests/test_latency.py
```

候选审计：

```text
cd new
python -m app.ticket_audit --task tasks/jd-qualification.json --fit fit.txt --heldout heldout.txt
```

剔除 A/B：

```text
cd new
python scripts/ticket_exclusion_ab.py
```
