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

## 整通语音与结果回写 RSI

`closure_cases.v1.json` 将合成通话按业务家族分到开发、验证、最终留出。运行器使用比赛入口的同一 Agent、Orchestrator 和 SQLite 模拟回写。逐轮核对阶段、调用时机、工具参数、回执和实际落库。盲评记录话术清晰度、对话适配和语义合规。

```powershell
python -X utf8 scripts/run_pop_closure_rsi.py --output data/pop-closure-rerun --judge
```

候选源于开发集的可定位失败，改动限定在预审规则。比较门槛为至少净增两通正确处理、正确回写增加、已通过的通话无退化、候选无严重规则失败。最终留出前锁定版本。回放基线文件核对回滚，原始会话日志、回执、落库和逐例核验随运行保存。

使用现有 Sherpa 和 Melo，运行 `scripts/build_pop_closure_voice.py --model <Melo模型目录> --output data/pop-closure-fixtures --plan eval/pop/closure_voice.v2.json` 生成回放录音。两次启动 `scripts/serve_pop_contest.py` 时均用该目录作为 `--reuse-audio`，并使用 `--rtc-pcm --prime-tts`。候选启动时加入 `--closure-policy data/pop-closure-rerun/candidate.json`。Windows 上若语音原生库无法读取含中文的模型路径，可使用不含中文的本地路径映射。

```powershell
node scripts/check_pop_contest_browser.mjs http://127.0.0.1:8091 data/pop-closure-before rtc_pcm closure eval/pop/closure_voice.v2.json
node scripts/check_pop_contest_browser.mjs http://127.0.0.1:8092 data/pop-closure-after rtc_pcm closure eval/pop/closure_voice.v2.json
python -X utf8 scripts/audit_pop_closure_voice.py --before data/pop-closure-before --after data/pop-closure-after --evidence data/pop-closure-rerun --plan eval/pop/closure_voice.v2.json
```

语音核验检查录音哈希、运行配置、连续语音、WebRTC 音频、六阶段和最终回执。门控通过后保存 `verified-release.json` 供本地复验。首声为合成录音末次有声帧到浏览器首播，真人声学时延需在现场设备实测。

## 可追问评测、候选策略与口语版

[RSI 优化日志](整通任务RSI复测与比赛创新.md)记录各轮改动、正常与压力结果、WebRTC 时延、失败归因及适用范围。[结构化结果](results/rsi-2026-11-22.json)汇总已执行实验。

业务解释和整改来自主干的 `knowledge/pop_audit.json`。资料来源和归类约定见[赛题资料解析](赛题资料解析.md)。`reactive_calls.v1.txt` 是合成评测样本，模拟商家在实际答复没答全时追问。`releases/closure-verified.v1.json` 是可随仓库复跑的闭环策略起点。

云端实验由 `POP_JUDGE_BASE_URL`、`POP_JUDGE_MODEL`、`POP_JUDGE_API_KEY` 配置，访问令牌由运行者自行设置。开发集比较两组候选，锁定后才继续验证和留出。

```powershell
python -X utf8 scripts/run_pop_reactive_rsi.py --output data/reactive-control --splits dev validation test
python -X utf8 scripts/run_pop_reactive_rsi.py --arm plan --compare data/reactive-control --output data/reactive-plan
python -X utf8 scripts/run_pop_reactive_rsi.py --arm plan-retry --compare data/reactive-control --output data/reactive-retry
python -X utf8 scripts/analyze_pop_reactive_rsi.py lock --candidates data/reactive-plan data/reactive-retry --output data/reactive-lock.json
$candidateArm = (Get-Content data/reactive-lock.json -Raw | ConvertFrom-Json).fingerprint.arm
python -X utf8 scripts/run_pop_reactive_rsi.py --arm $candidateArm --compare data/reactive-control --lock data/reactive-lock.json --splits validation test --output data/reactive-independent
if ($candidateArm -eq 'plan-retry') {
    python -X utf8 scripts/check_pop_phone_style.py --style eval/pop/phone_style.v1.json --control data/reactive-independent --output data/phone-style-regression
}
```

使用 `requirements-rtc.txt` 中的 WebRTC 依赖后，可在现有 `/rtc/?transport=rtc_pcm&downlink=pcm` 页面试听。TTS 使用 G2 适配器的默认声音 `stella`，语音服务凭据从本地环境加载。以下为本轮实际对照的候选，显式开启才生效。

```powershell
python -X utf8 scripts/serve_pop_contest.py --output data/pop-voice-review --rtc-pcm --g2-voice --prime-tts --prime-followup-tts --closure-policy eval/pop/releases/closure-verified.v1.json --turn-intent plan-retry --phone-style eval/pop/phone_style.v1.json
```

录音对照使用 `scripts/build_pop_closure_voice.py --plan eval/pop/reactive_voice.v1.json` 生成 16 kHz 合成商家输入。启动服务时增加 `--reuse-audio <录音目录> --doc-id pop-scene-053 --request-id Q984053`，再用 `scripts/check_pop_contest_browser.mjs` 的 `closure` 模式和同一份计划回放。浏览器工具需要 Playwright 和 Chromium，网页使用已有的通话控件。

干净 checkout 跑测试前先创建本地输出目录，项目的 pytest 临时目录位于其下。

```powershell
New-Item -ItemType Directory -Force data | Out-Null
python -X utf8 -m pytest -q tests/test_pop_closure_rsi.py tests/test_pop_turn_plan.py tests/test_pop_competition.py tests/test_pop_strategy.py tests/test_pop_rules.py tests/test_pop_harness.py
```
