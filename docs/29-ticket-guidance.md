# 29 · 工单到电话指导文件（B 方案）

> 状态：后端已实现，未接外呼任务/拨号器。  
> 入口：`/tickets/guidance`  
> 存储：`data/guidance/<ticket_id>.json` + `.md`

## 1. 链路

```text
工单字段
→ TicketInput 归一化
→ LLM 结构化生成（可选）
→ Schema 校验 / 禁语检查
→ 失败回退模板
→ JSON + Markdown
→ 人工审批
→ 供外呼任务读取
```

## 2. 输入

```json
{
  "ticket_id": "T100",
  "request_id": "Q983001",
  "brand": "青禾",
  "reject_l1": "入驻",
  "reject_l2": "公司主体",
  "reject_l3": "主体资质到期",
  "remediation": "更新有效资质后重新提交",
  "materials": [],
  "steps": [],
  "use_llm": true
}
```

`materials`、`steps` 为空时按驳回原因生成默认内容。

## 3. API

### 生成

```http
POST /tickets/guidance
```

返回：

```json
{
  "document": {
    "ticket_id": "T100",
    "reason": "入驻 / 公司主体 / 主体资质到期",
    "materials": ["更新有效资质后重新提交"],
    "steps": ["打开京麦本次被驳回的原申请", "按书面驳回意见补充或修正材料", "重新提交并等待复审"],
    "script": {
      "opening": "您好，这里是京东商家资质审核客服……",
      "reason": "本次退回原因是……",
      "action": "您需要……",
      "closing": "结果以复审为准……"
    },
    "forbidden": ["保证通过", "包过", "百分百", "一定通过", "加急", "必过"],
    "approved": false,
    "version": 1,
    "source": "llm|template|template_fallback"
  },
  "markdown": "# 电话指导……"
}
```

### 查询

```text
GET /tickets/guidance
GET /tickets/guidance/{ticket_id}
GET /tickets/guidance/{ticket_id}/markdown
```

### 审批

```http
POST /tickets/guidance/{ticket_id}/approve
```

```json
{"approver": "reviewer"}
```

审批后 `approved=true`，`version+1`。

## 4. 硬约束

```text
不承诺通过/加急/审核时限
不编造入口、网址、电话
不建议涂改、修图或绕过审核
没有信息就说无法确认
不声称已登记、已提交、已转人工
```

LLM 输出包含禁语、JSON 不合法、调用失败时，自动回退模板并标 `template_fallback`。

## 5. 代码

```text
app/guidance/models.py      TicketInput / GuidanceDocument
app/guidance/generator.py   模板、LLM prompt、JSON 解析、禁语检查
app/guidance/service.py     存储、审批、Markdown
app/guidance/api.py         路由
tests/test_guidance.py      5 个回归
```

## 6. 尚未做

```text
从真实工单系统拉取
人工审批 UI
PDF 导出
外呼任务/拨号器分发
通话结果回写指导版本
```
