# 27 · 预测式对话契约 v1

> 对应 schema：`contracts/schemas/predictive.v1.json`  
> API 前缀：`/predictive/v1`  
> 生命周期：预生成 → 分类 → 匹配 → 命中输出/未命中套话 → 预生成下一轮

## 1. 生命周期

```text
prepare(stage)
  → 生成当前阶段候选
  → classify(text)
  → match(stage, label)
      hit  → 输出候选 → prepare(next_stage)
      miss → 输出分类器选择的套话 → LLM 生成正式回答
```

硬规则：

```text
1. 每次 handle 前自动 prepare，不能依赖调用方先手动准备
2. 命中输出和未命中套话都必须经过分类器
3. 命中后必须预生成下一轮
4. 未命中不得编造候选；只能出套话并交给 LLM
5. 身份确认前不得输出案件
6. 任何非空输入都必须命中候选或预生成 fallback；fallback 的 source=filler，并标记 needs_llm=true内容
```

## 2. 分类器实现

当前 v1 使用纯规则分类器（无模型、无网络），保证低延迟和离线可跑。

接口允许后续替换为：

```text
小模型分类器
向量/embedding 相似度分类
规则 + 模型混合
LLM 分类（只在高价值/低置信场景）
```

无论换哪种实现，契约不变，且必须满足：

```text
1. 任意非空输入都有 label，不能返回空/异常
2. 低置信度落到 other，并使用预生成 fallback
3. fallback source=filler，needs_llm=true
4. 分类器升级不能改变请求/响应字段
```

分类器评估由湖中日月负责，指标至少包含：

```text
标签准确率
fallback 率
误命中率
分类 P50/P95
```

## 3. 分类标签

```text
affirmative
deny_identity
closing
transfer
abuse
busy
negative
confused
already
question_identity
other
empty
```

## 4. API

前端可直接读取契约：`GET /predictive/v1/contract`（返回 `contracts/schemas/predictive.v1.json`）。

### POST `/predictive/v1/classify`

请求：

```json
{
  "operation": "classify",
  "text": "好",
  "stage": "REASON",
  "context": {"reject_l3": "主体资质到期"}
}
```

响应：

```json
{
  "schema_version": "1.0",
  "operation": "classify",
  "classification": {
    "label": "affirmative",
    "stage": "REASON",
    "confidence": 0.95,
    "reason": "short affirmative"
  }
}
```

### POST `/predictive/v1/prepare`

请求：

```json
{
  "operation": "prepare",
  "stage": "REASON",
  "context": {"reject_l3": "主体资质到期"}
}
```

响应：

```json
{
  "schema_version": "1.0",
  "operation": "prepare",
  "stage": "REASON",
  "candidates": [
    {
      "candidate_id": "REASON:affirmative",
      "stage": "REASON",
      "label": "affirmative",
      "text": "原因是主体资质到期。请更新有效资质后重新提交。",
      "next_stage": "REMEDIATION",
      "source": "prepared"
    }
  ]
}
```

### POST `/predictive/v1/handle`

请求：

```json
{
  "operation": "handle",
  "text": "好",
  "stage": "REASON",
  "context": {
    "reject_l3": "主体资质到期",
    "remediation": "更新有效资质后重新提交"
  }
}
```

响应：

```json
{
  "schema_version": "1.0",
  "operation": "handle",
  "decision": {
    "text": "原因是主体资质到期。请更新有效资质后重新提交。",
    "hit": true,
    "stage": "REASON",
    "next_stage": "REMEDIATION",
    "classification": {
      "label": "affirmative",
      "stage": "REASON",
      "confidence": 0.95,
      "reason": "short affirmative"
    },
    "output_classification": {
      "label": "other",
      "stage": "REASON",
      "confidence": 0.5,
      "reason": "no rule"
    },
    "candidate": {
      "candidate_id": "REASON:affirmative",
      "stage": "REASON",
      "label": "affirmative",
      "text": "原因是主体资质到期。请更新有效资质后重新提交。",
      "next_stage": "REMEDIATION",
      "source": "prepared"
    },
    "filler_used": false,
    "needs_llm": false,
    "prepared_candidates": []
  }
}
```

## 5. 命中/未命中

### 命中

```text
hit=true
candidate 非空
text 来自预生成候选
next_stage 非空时，下一轮 prepare 该阶段
```

### 未命中

```text
hit=false
candidate=null
filler_used=true
text 来自分类器对应的套话
prepared_candidates 仍返回当前阶段候选
```

## 6. 错误

```text
422 text 为空
422 stage 为空
```

## 7. 与现有 Agent 的关系

```text
身份确认阶段：保留现有安全分支
身份确认后：handle 先走 /predictive/v1/handle
命中：直接输出并推进阶段
未命中：先出套话，再走 LLM
```

## 8. 验证

```text
schema：contracts/schemas/predictive.v1.json
fixture：contracts/fixtures/predictive.v1.handle.json
```
