# 38 · 规则 + 预测候选 + 后台预热（单一干净分支）

> 分支：`feature/rule-prediction-warmup-v2`  
> base：`origin/master d46938f`  
> 范围：只包含规则引擎、预测候选生成、后台预热。

## 1. 不包含

```text
compliance-cache
cache audit
decision 模型
文本/音频解耦
prediction audio budget
历史实验数据
```

## 2. 规则引擎

- TaskCard 自动短语本
- 中文短答 / 短英文
- 否定 / 改口 / 新问题 / 催结果 / 投诉 / 换人
- 问句方面词
- 同义词归一
- IDF n-gram
- 唯一业务词 / 分差门槛

测试覆盖：

```text
京东
官方工单
宽带
网络
物流
发票
强对抗
```

## 3. 预测候选

- TaskCard checkpoint/facts/opening 生成 ticket 候选
- checkpoint / intent / polarity 标签
- ticket + 模型候选合并去重
- 候选上限 10
- ticket 7 + 模型 3（limit=10）
- 按卡剔除开关
- 剔除后用替代族补回
- intent 使用审计

## 4. 后台预热

- 任务载入/保存后 POST /api/warm
- 后台合成 Session
- 正常调用模型和 TTS
- Sink 丢弃所有帧和 PCM
- 多组预设情景并行轮回
- 前台通话 start 立即取消
- 已写缓存保留

配置：

```text
TASK_PREWARM_ON_LOAD=1
TASK_PREWARM_CONCURRENCY=4
TASK_PREWARM_LOOP_DELAY=1
TASK_PREWARM_MAX_ROUNDS=0
```

## 5. 验证

```text
new 全量离线测试：125 passed
```

## 6. 边界

```text
不声称真实语音 P50/P95 已提升
不包含 PR20 语音对比
后台预热是 best-effort
```
