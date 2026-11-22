# 37 · 工单载入后的后台完整对话预热

## 范围

本分支只包含后台预热：

```text
feature/background-prewarm-clean
base: d46938f (origin/master)
```

不包含：

```text
规则引擎优化
预测候选生成
decision 模型
compliance-cache
```

## 行为

任务载入或保存时：

```text
POST /api/warm
→ 后台合成 Session
→ 正常调用 Brain/Cloud
→ 生成模型候选和 TTS 音频
→ Sink 丢弃所有帧和 PCM
→ 只写模型/TTS 缓存和独立日志
```

预设对话并行运行：

```text
card_1 / card_2 / ...
official
official_status
adversarial
adversarial_2
short
english
```

每轮跑完继续下一轮，直到前台通话开始。

前台 `start`：

```text
cancel_task_prewarm(card)
→ 取消所有合成会话
→ 5 秒内阻止迟到的 /api/warm
→ 已写缓存保留
```

## 配置

```text
TASK_PREWARM_ON_LOAD=1
TASK_PREWARM_CONCURRENCY=4
TASK_PREWARM_LOOP_DELAY=1
TASK_PREWARM_MAX_ROUNDS=0
```

## 日志

```text
new/runtime/task-prewarm/<时间>-<task_key>.jsonl
new/runtime/task-prewarm/sessions/
```

## 文件

```text
new/app/task_prewarm.py
new/app/main.py
new/web/call.js
new/.env.example
new/tests/test_task_prewarm.py
```

## 验证

```text
91 passed
```

## 边界

```text
best-effort
不保证首通 100% 命中缓存
后台会消耗模型/TTS 配额
前台通话优先
```
