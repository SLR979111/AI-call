# POP 评测与 RSI

本目录用于京东 POP 商家资质驳回外呼。对通话页的 10 个场景进行自动出题、策略优化和回归，见 [多场景 RSI](contest-rsi-nov08.md)。下面记录早期离线业务评测的复现方法。

## 工作流

1. 从赛题 ZIP 的场景主表和 QA 读出口径。结合 `seeds.tsv` 生成案例、分区和带条件的模拟商家脚本。来源与旧数据复用见 [资料解析](赛题资料解析.md)。
2. 使用本地文本入口跑开发集。每例从空的模拟工单系统开始，用已生效的客服话术、工具返回、工单记录和调用顺序评分。
3. 把开发失败归到身份、知识分流、工具回写等类别，按规则生成一份新策略。版本文件只允许有限开关。比较前固定案例和评分文件。
4. 使用验证和留出集成对评测两个版本。留出集的业务家族与开发集不同。评测后用基线文件复跑验证集，核对回滚是否重现原结果。
5. 以固定量表做模型语义评审，记录原话证据及校准结果。自测报告并列展示规则和语义评测。

## 命令

以下命令在项目根目录运行。安装项目的 `requirements.txt` 后，离线运行无需模型权重或模型密钥。

```powershell
python scripts/eval_pop.py --output data/pop-rerun
python scripts/report_pop.py data/pop-rerun --bundle data/pop-rerun-evidence.zip
```

`--output` 使用新目录。运行会写出 `cases.json`、版本文件、逐例结果、`raw` 中的后端事件 JSONL 和对话及最终工单快照。两个版本的日志都有实际运行时版本 ID。

如需重新生成数据，使用带 `openpyxl` 的 Python。生成命令和校验见 [资料解析](赛题资料解析.md)。

恢复已有基线版本可重新从保存的策略文件创建 Agent。

```powershell
python scripts/eval_pop.py --policy data/pop-rerun/releases/baseline.json --split validation --output data/pop-rollback
```

配置兼容 Chat Completions 的质检服务后，设置 `POP_JUDGE_BASE_URL`、`POP_JUDGE_MODEL` 和 `POP_JUDGE_API_KEY`。默认质检验证集和留出集的新旧两版，仅传合成对话。

```powershell
python scripts/judge_pop.py --run-dir data/pop-rerun --output data/pop-rerun/judge_cloud.json
python scripts/report_pop.py data/pop-rerun
```

仅做独立盲评时，可用 `python scripts/blind_review_pop.py prepare data/pop-rerun` 生成不带版本名和规则分的输入。盲评者按固定量表回填 `review-1.json` 和 `review-2.json`，键为样本 ID，值为包含三维评分和逐字证据的对象。用 `collect` 检查证据并写入报告输入。

本轮自动生成候选并做离线复测。候选的提示词建议单独保存，实际跑分使用规则 Agent 的话术、检索和工具策略。线上发布和自动晋升不在此流程中。文本跑分、模拟回写和抽样语义审阅的适用范围以报告为准。
