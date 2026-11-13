# 合规规则外置与场景扩展 - 功能文档

## 概述

本文档描述阶段 3（合规规则外置）和阶段 4（场景覆盖扩展）的功能实现。

## 阶段 3：合规规则外置

### 功能目标

- 将硬编码的合规规则外置到 YAML 配置文件
- 支持动态加载和热更新规则
- 提供质检报告生成功能
- 增强合规检查的可维护性和可扩展性

### 核心文件

1. **`config/rules.yaml`** - 合规规则配置文件
   - 文本合规规则（REWRITE、BLOCK、TRANSFER）
   - 工具调用规则（允许的工具列表、参数校验）
   - 质检规则（响应时间、对话轮次、违规次数）
   - 报告配置（质检报告、合规报告）

2. **`app/compliance/loader.py`** - 规则加载器
   - `ComplianceRules` 类：规则容器
   - `load_rules()` 函数：从 YAML 加载规则
   - 正则表达式编译和校验

3. **`app/compliance/guard_enhanced.py`** - 增强的合规守卫
   - 支持 YAML 规则和硬编码规则（向后兼容）
   - 三点守卫：输入、句子、工具
   - 自动降级到硬编码规则（YAML 加载失败时）

4. **`app/compliance/reporter.py`** - 质检报告生成器
   - `QualityReporter`：会话质检报告
   - `ComplianceReporter`：合规违规报告
   - 支持 JSON 和 Markdown 两种格式

### 使用方法

#### 1. 使用 YAML 规则

```python
from app.compliance import ComplianceGuard

# 加载 YAML 规则
guard = ComplianceGuard(rules_path="config/rules.yaml")

# 检查句子
result = await guard.check_sentence("保证通过")
print(result.decision)  # "REWRITE"
print(result.text)      # "以实际结果为准"
```

#### 2. 生成质检报告

```python
from app.compliance import QualityReporter

reporter = QualityReporter(output_dir="data/quality_reports")

# 开始会话
reporter.start_session("s_123")

# 记录对话轮次
reporter.record_turn("s_123")

# 记录合规违规
reporter.record_compliance_violation(
    "s_123", "false_promise", "REWRITE", 
    "保证通过", "warn"
)

# 记录工具调用
reporter.record_tool_call(
    "s_123", "order_query", "success", 123.45
)

# 结束会话
reporter.end_session("s_123", "completed", "order_rescheduled")

# 生成报告
files = reporter.generate_report("s_123", formats=["json", "markdown"])
print(files)  # {"json": Path(...), "markdown": Path(...)}
```

### 规则配置示例

```yaml
# config/rules.yaml
version: "1.0"

text_rules:
  - id: false_promise
    pattern: "保证|一定|绝对|百分百"
    decision: REWRITE
    replacement: "以实际结果为准"
    severity: warn
    enabled: true

  - id: privacy_leak
    pattern: "身份证号|银行卡号"
    decision: BLOCK
    replacement: "抱歉，这个问题我无法提供。"
    severity: fatal
    enabled: true

tool_rules:
  allowed_tools:
    - order_query
    - order_reschedule
    - callback_create

quality_check:
  metrics:
    - id: response_time
      warning_threshold: 2000
      error_threshold: 5000
```

---

## 阶段 4：场景覆盖扩展

### 功能目标

- 补充二级分类场景定义
- 扩展场景话术和知识库
- 添加评测用例
- 提升场景覆盖率和对话质量

### 核心文件

1. **`flowpacks/pop_qualification_audit_extended.json`** - 扩展场景定义
   - 新增 DETAIL 状态（详细说明驳回原因）
   - 扩展 reject_l1/l2/l3 槽位定义
   - 新增二级分类场景映射
   - 支持 5 个一级分类 × 3-4 个二级分类

2. **`knowledge/faq_extended.json`** - 扩展知识库
   - 13 条知识条目
   - 覆盖常见问题、场景话术、合规规则
   - 分类：常见问题、场景话术、合规规则

3. **`docs/test_cases_pop_scenarios.md`** - 评测用例
   - 11 个测试用例
   - 覆盖基础场景、二级分类、合规测试、性能测试
   - 包含自动化测试脚本示例

### 二级分类场景映射

| 一级分类 | 二级分类 | 三级原因示例 |
|---------|---------|------------|
| 入驻 | 公司主体 | 主体资质到期、主体信息不一致 |
| 入驻 | 法人信息 | 法人身份证过期、法人信息不完整 |
| 入驻 | 联系方式 | 联系电话无法接通、联系地址不详细 |
| 品牌 | 商标注册证 | 商标状态不符合、商标注册时间不符合 |
| 品牌 | 品牌授权 | 授权链路不完整、授权书过期 |
| 品牌 | 品牌使用权 | 使用权证明缺失、使用期限不符 |
| 资质 | 营业执照 | 营业执照过期、经营范围不包含 |
| 资质 | 税务登记 | 税务登记证缺失、纳税人识别号错误 |
| 资质 | 许可证 | 行业许可证缺失、许可证过期 |
| 经营 | 经营范围 | 经营类目不匹配、超出核准范围 |
| 经营 | 经营地址 | 地址信息不完整、地址与执照不符 |
| 财务 | 开户许可 | 开户许可证过期、开户行信息错误 |
| 财务 | 对公账户 | 账户信息不完整、账户名称不一致 |

### 知识库扩展

**常见问题（5 条）**：
- 如何查看驳回原因？
- 重新提交后多久能审核完成？
- 可以更换品牌重新申请吗？
- 驳回后可以申诉吗？
- 需要补充哪些材料？

**场景话术（5 条）**：
- 商标状态不符合要求 - 处理方式
- 授权链路不完整 - 处理方式
- 主体资质到期 - 处理方式
- 资质造假 - 处理方式
- 信息不一致 - 处理方式

**合规规则（3 条）**：
- 不承诺审核结果
- 保护商家隐私
- 录音告知

### 评测用例

**基础场景测试（4 个）**：
- 用例 1：完整流程 - 商标状态不符合
- 用例 2：授权链路不完整
- 用例 3：用户要求转人工
- 用例 4：用户要求回电

**二级分类场景测试（2 个）**：
- 用例 5：入驻 - 公司主体 - 主体资质到期
- 用例 6：品牌 - 品牌授权 - 授权书过期

**合规测试（2 个）**：
- 用例 7：合规拦截 - 虚假承诺
- 用例 8：合规拦截 - 隐私泄露

**性能测试（2 个）**：
- 用例 9：首字时延测试（P50 < 900ms）
- 用例 10：多轮对话稳定性测试

**质检报告测试（1 个）**：
- 用例 11：质检报告生成

---

## 集成说明

### 1. 环境变量配置

在 `.env` 文件中添加：

```bash
# 合规规则路径
COMPLIANCE_RULES_PATH=config/rules.yaml

# 质检报告输出目录
QUALITY_REPORT_DIR=data/quality_reports
COMPLIANCE_REPORT_DIR=data/compliance_reports
```

### 2. 集成到 Orchestrator

```python
from pathlib import Path
from app.compliance import ComplianceGuard, QualityReporter

# 初始化合规守卫（使用 YAML 规则）
guard = ComplianceGuard(rules_path=Path("config/rules.yaml"))

# 初始化质检报告器
quality_reporter = QualityReporter(output_dir="data/quality_reports")
```

### 3. 使用扩展场景

```python
from app.flow.pack import FlowPack

# 加载扩展场景
flow_pack = FlowPack.from_file("flowpacks/pop_qualification_audit_extended.json")

# 使用扩展知识库
from app.rag.knowledge import KnowledgeBase
knowledge = KnowledgeBase(Path("knowledge"))
# 会自动加载 faq_extended.json 和 pop_audit.json
```

---

## 测试方法

### 1. 单元测试

```bash
# 运行合规规则测试
pytest tests/test_compliance.py -v

# 运行场景测试
pytest tests/test_pop_scenarios.py -v
```

### 2. 性能测试

```bash
# 测试首字时延（需要真实 LLM API）
python scripts/test_ttft.py

# 运行评测用例
python scripts/run_eval.py --scenario pop_extended
```

### 3. 质检报告测试

```python
from app.compliance import QualityReporter

reporter = QualityReporter()
reporter.start_session("test_session")
reporter.record_turn("test_session")
reporter.end_session("test_session", "completed")
files = reporter.generate_report("test_session")
print(f"报告生成: {files}")
```

---

## 验收标准

### 阶段 3：合规规则外置

- ✅ YAML 规则文件完整且格式正确
- ✅ 规则加载器正确解析 YAML
- ✅ 合规守卫支持 YAML 规则和硬编码规则
- ✅ 质检报告生成器正常工作
- ✅ 向后兼容（不传 rules_path 时使用硬编码规则）

### 阶段 4：场景覆盖扩展

- ✅ 扩展场景定义包含 5 个一级分类
- ✅ 每个一级分类有 3-4 个二级分类
- ✅ 知识库扩展到 13 条知识条目
- ✅ 评测用例覆盖基础场景、二级分类、合规、性能
- ✅ 所有测试用例通过

---

## 后续工作

1. **红队测试（阶段 5）**
   - Prompt Injection 攻击测试
   - 安全加固
   - 异常输入处理

2. **性能优化**
   - 规则匹配性能优化
   - 知识库检索加速
   - 质检报告异步生成

3. **监控与告警**
   - 合规违规实时告警
   - 质检指标监控
   - 异常场景自动上报

---

## 参考文档

- [合规规则配置](../config/rules.yaml)
- [扩展场景定义](../flowpacks/pop_qualification_audit_extended.json)
- [扩展知识库](../knowledge/faq_extended.json)
- [评测用例](./test_cases_pop_scenarios.md)
- [原有合规模块](../app/compliance.py)
