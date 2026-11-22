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
      "opening": "您好，我是协助核对京东店铺资料的第三方客服，不是京东审核人员……",
      "reason": "本次退回原因是……",
      "action": "您需要……",
      "closing": "结果以复审为准……"
    },
    "forbidden": ["保证通过", "包过", "百分百", "一定通过", "必过", "今天一定", "明天一定", "已帮您提交", "已转人工", "京东官方审核人员"],
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
不承诺通过、加急一定处理或具体审核时限；官方页面“预计1个工作日内审核通过”只能作为参考
不编造入口、网址、电话
不建议涂改、修图或绕过审核
没有信息就说无法确认
不声称已登记、已提交、已转人工
```

LLM 输出包含禁语、JSON 不合法、调用失败时，自动回退模板并标 `template_fallback`。

## 4.1 合规依据

电话指导与话术按京东官方页面和本次工单事实生成，资料与边界见：

- [30 · 京东 POP 商家资质合规与电话话术依据](30-jd-compliance-and-call-script.md)
- [POP 品牌管理常见问题指南](https://helpcenter.jd.com/vender/issue/1000-43743.html)
- [品牌使用指南](https://helpcenter.jd.com/vender/issue/1000-45463.html)
- [京东开放平台招商标准](https://storage.jd.com/karmavender/file/pdf/20191018/f5c6f6e642024883a5d324569b87b264.pdf)

官方页面写“预计一个工作日”时，指导文件只能把它作为参考信息，不能写成承诺。当前工单里的两段授权链、名称不一致、截图模糊属于本次案例假设，不能扩大成“京东统一规定”。

## 5. 代码

```text
app/guidance/models.py      TicketInput / GuidanceDocument
app/guidance/generator.py   模板、LLM prompt、JSON 解析、禁语检查
app/guidance/service.py     存储、审批、Markdown
app/guidance/api.py         路由
tests/test_guidance.py      7 个回归
```

## 6. 尚未做

```text
从真实工单系统拉取
人工审批 UI
PDF 导出
外呼任务/拨号器分发
通话结果回写指导版本
```
