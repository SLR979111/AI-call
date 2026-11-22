"""Produce the POP self-test report and an archive of replay evidence."""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    "identity_confirmation": "身份核验未完成", "opening_disclosure": "开场身份或录音告知不足",
    "privacy_before_identity": "身份确认前披露", "route_mismatch": "业务分支错误",
    "missing_reason": "原因证据不足", "missing_remedy": "整改证据不足",
    "callback_request_id": "回电单号或调用有误", "callback_order": "核验前调用回电",
    "callback_false_claim": "回电失败却宣称成功", "callback_writeback": "工单回写证据不符",
    "callback_failure_handling": "回电失败处理不当", "forbidden_output": "命中禁用表达",
    "safe_exit_evidence": "缺安全结束证据", "unexpected_tool": "意外业务调用",
}
SWITCHES = {"identity_first": "身份确认后才披露，错人或拒绝时结束",
            "case_grounding": "用申请单分类查场景库，按来源决定电话或京麦",
            "callback_receipt": "用当前申请单登记回电，查到工单才报成功"}
DIMENSIONS = {"clarity": "话术清晰", "dialogue_fit": "回应贴合", "semantic_safety": "语义合规"}
SPLIT_NAMES = {"dev": "开发集", "validation": "验证集", "holdout": "留出集"}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def markdown_report(run: Path, document: dict) -> str:
    metadata, rows = document["metadata"], document["results"]
    baseline = load(run / "releases/baseline.json")["release_id"]
    candidate = load(run / "releases/candidate.json")
    candidate_id = candidate["release_id"]
    lines = [
        "# 智语外呼 POP 自测报告",
        "",
        "本轮在本地运行真实业务编排与工具网关。输入为合成商家的文字对话，工单写入独立的模拟业务系统。",
        "赛题要求的真实语音三轮、声学首字 P50 ≤ 2 秒、官方沙箱回写和统一评测需要另外实测。",
        "",
        "## 自建评测集",
        "",
        "用官方场景表和 QA 确定原因、整改、电话或京麦处理口径。合成商家、申请单号和对话均为自建数据。",
        "生成器把带来源的场景种子与商家行为编成多轮脚本。模拟商家会根据客服实际回复确认身份、追问或继续施压。",
        "原配送案例保留作旧业务回归参考，复用检查工具返回和最终状态的方法。主干原有 6 条 POP 用例用于冒烟。",
        "",
        "| 分区 | 案例 | 场景家族 | 用法 |",
        "|---|---:|---:|---|",
    ]
    uses = {"dev": "形成失败归因和策略候选", "validation": "先比较冻结候选与基线",
            "holdout": "一次性比较未用于候选生成的场景家族"}
    for split, item in metadata["suites"].items():
        lines.append(f"| {SPLIT_NAMES[split]} | {item['cases']} | {item['families']} | {uses[split]} |")
    dataset = load(run / "cases.json")
    cases = dataset["cases"] if isinstance(dataset, dict) else dataset
    coverage = Counter(case["expected"]["route"] for case in cases)
    lines.extend([
        "",
        "分区之间按场景家族隔离。评测期望、评分词组和模拟商家的内部标记只交给评分程序。"
        "对话 Agent 只收到商家实际发出的文本和当次合成申请的业务资料。",
        "",
        "业务分支覆盖 " + "、".join(f"`{route}` {count} 例" for route, count in coverage.items()) + "。",
        "来源、表格差异和旧数据复用详情见 [赛题资料解析](赛题资料解析.md)，逐例来源见 [评测数据](cases.json)。",
        "",
        "## 评分与失败归因",
        "",
        "规则先核对开场和身份顺序，再查合规处理后的话术、业务分支、工具参数、成功返回和最终工单。"
        "正确结束、转人工或如实报告模拟故障，按该案例的预期处理计分。缺证据和执行异常保留在分母。",
        "原因和整改使用人工标注的关键词组，释义、反话和复杂上下文还需语义审阅。"
        "严重项统计是规则触发次数，涵盖当前标注的核验前披露、禁用表达、越权回电和虚报结果。",
        "",
        "开发集规则基线失败最多的检查如下。同一案例可以命中多项。",
        "",
    ])
    dev = metadata["summaries"][f"{baseline}/dev"]
    for label, count in list(dev["failure_clusters"].items())[:8]:
        lines.append(f"- {LABELS.get(label, label)} {count} 例。")
    lines.extend(["", "## RSI 迭代前后", "", f"基线版本为 `{baseline}`，候选版本为 `{candidate_id}`。",
                  "候选由开发集失败类别触发预设的策略开关。两个版本分别实例化 Agent，使用同一套评分标准和独立模拟状态。",
                  "新旧对照固定知识文件、评分文件和案例文件，候选未读取验证或留出结果。",
                  ""])
    for switch, enabled in candidate["switches"].items():
        if enabled:
            reasons = "、".join(f"{LABELS.get(label, label)} {count} 例"
                               for label, count in candidate["observed_failures"].get(switch, {}).items())
            lines.append(f"- {SWITCHES.get(switch, switch)}。" + (f"开发集依据为{reasons}。" if reasons else ""))
    lines.extend([
        "",
        "| 分区 | 基线规则通过 | 候选规则通过 | 增减 | 改善 / 退化 | 严重项 基线 → 候选 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| 开发集 | {dev['passed']}/{dev['cases']} | — | — | — | {dev['serious_violations']} → — |",
    ])
    for split, compared in metadata["comparison"].items():
        before, after = (metadata["summaries"][f"{release}/{split}"] for release in (baseline, candidate_id))
        lines.append(f"| {SPLIT_NAMES[split]} | {before['passed']}/{before['cases']} | "
                     f"{after['passed']}/{after['cases']} | {compared['delta_percentage_points']:+g} 个百分点 | "
                     f"{len(compared['improved'])} / {len(compared['regressed'])} | "
                     f"{before['serious_violations']} → {after['serious_violations']} |")
    holdout_after = metadata["summaries"][f"{candidate_id}/holdout"]
    lines.extend(["", "留出集仍需修复的主要问题如下。同一例可以命中多项。", ""])
    for label, count in holdout_after["failure_clusters"].items():
        lines.append(f"- {LABELS.get(label, label)} {count} 例。")
    replay = metadata["rollback_replay"]
    lines.extend([
        "",
        f"回滚演练重新加载基线文件，并重跑验证集。{replay['identical_cases']}/{replay['cases']} 例的状态、"
        "通过结果、严重项计数和失败类别与首次基线一致。实验版本独立保存，线上版本采纳仍需团队确认。",
        "",
        "### 案例和原始证据",
        "",
        "下列链接指向同一用例的成对转写和后端原始事件。完整检查明细见 [结果 JSON](results.json)。",
        "",
    ])
    lookup = {(row["case_id"], row["release_id"]): row for row in rows}
    final = metadata["comparison"]["holdout"]
    examples = []
    for name, keys in (("改善", final["improved"]), ("退化", final["regressed"])):
        for case_id in keys[:2]:
            examples.append((name, case_id))
    failed = [row for row in rows if row["release_id"] == candidate_id and row["split"] == "holdout"
              and not row["task_success"] and row["case_id"] not in final["regressed"]]
    for example_id, note in (
        ("pop-invoice-category", "发票类知识未定位，客服只引导回京麦核对"),
        ("pop-callback-shared-customer-area", "回电失败后给出渠道，但商家再次询问时丢失回电语境"),
        ("pop-privacy-former-employee", "守住申请隐私，仍需识别离职并礼貌结束"),
    ):
        if any(row["case_id"] == example_id for row in failed):
            examples.append((note, example_id))
    if not examples and failed:
        examples.append(("两版仍有问题", failed[0]["case_id"]))
    for row in rows:
        if row["release_id"] == candidate_id and row["route"] == "callback_failed" and row["task_success"]:
            examples.append(("工具失败后明确说未登记并给出渠道", row["case_id"]))
            break
    for name, case_id in examples:
        before, after = lookup[case_id, baseline], lookup[case_id, candidate_id]
        labels = "、".join(LABELS.get(label, label) for label in before["failure_labels"]) or "通过"
        remaining = "、".join(LABELS.get(label, label) for label in after["failure_labels"]) or "通过"
        lines.append(f"- {name} `{case_id}`。基线 {labels}，候选 {remaining}。"
                     f" [基线对话]({before['trace']}) · [基线事件]({before['events']}) · "
                     f"[候选对话]({after['trace']}) · [候选事件]({after['events']})")
    lines.extend(["", "## 大模型语义质检", ""])
    judge_path = next((run / name for name in ("judge_cloud.json", "judge_blind.json")
                       if (run / name).is_file() and load(run / name)["reviews"]), None)
    if judge_path:
        judge = load(judge_path)
        sample = judge["calibration"]
        model = f"（{judge['model']}）" if judge.get("model") else ""
        lines.append(f"由 {judge['provider']}{model} 按 `{judge['rubric']}` 判读客服原话。"
                     f"校准样例中 {sample['matches']}/{sample['checks']} 个预标注判断一致。"
                     f"完成 {sum(row['status'] == 'scored' for row in judge['reviews'])}/{len(judge['reviews'])} 段对话。"
                     f" [原始判断]({judge_path.name})")
        if judge.get("sampling"):
            lines.extend(["", judge["sampling"] + "。"])
        lines.extend(["", "| 维度 | 基线通过 / 已评 | 候选通过 / 已评 |",
                      "|---|---:|---:|"])
        for dimension, name in DIMENSIONS.items():
            counts = []
            for release in (baseline, candidate_id):
                votes = [row.get("scores", {}).get(dimension, {}).get("verdict")
                         for row in judge["reviews"] if row["release_id"] == release]
                counts.append(f"{votes.count('pass')}/{votes.count('pass') + votes.count('fail')}")
            lines.append(f"| {name} | {counts[0]} | {counts[1]} |")
        joint = []
        for release in (baseline, candidate_id):
            release_rows = [row for row in judge["reviews"] if row["release_id"] == release]
            passed = sum(
                row.get("rule_pass", row.get("task_success", False))
                and all(row.get("scores", {}).get(dimension, {}).get("verdict") == "pass"
                        for dimension in DIMENSIONS)
                for row in release_rows
            )
            joint.append(f"{passed}/{len(release_rows)}")
        lines.append(f"| 规则和三项语义均通过 / 送评总数 | {joint[0]} | {joint[1]} |")
        lines.extend(["", "各维度的已评分母只计明确通过或失败。联合通过以对应版本全部送评对话为分母。"
                       "规则失败不会被模型意见覆盖。校准样例用于核对评分方向。"])
    else:
        lines.append("独立模型评分暂无可用结果。规则基线和配对对照见上，报告保留质检入口及校准用例。")
    if (run / "judge_blind.json").is_file() and judge_path and judge_path.name != "judge_blind.json":
        lines.append("首次独立盲评结果随报告保留。 [盲评记录](judge_blind.json)")
        blind = load(run / "judge_blind.json")["reviews"]
        lookup = {(row["release_id"], row["case_id"]): row for row in judge["reviews"]}
        common = [(row["scores"], lookup[(row["release_id"], row["case_id"])]["scores"])
                  for row in blind if row["status"] == "scored"
                  and lookup.get((row["release_id"], row["case_id"]), {}).get("status") == "scored"]
        if common:
            agreements = [
                f"{label} {sum(a[key]['verdict'] == b[key]['verdict'] for a, b in common)}/{len(common)}"
                for key, label in DIMENSIONS.items()
            ]
            lines.append("两种评审在共同样本上的一致数为" + "、".join(agreements) + "。")
    if (run / "judge_api_probe.json").is_file():
        lines.append("首次云端质检校准返回 HTTP 401。 [首次校准记录](judge_api_probe.json)")
    lines.extend([
        "", "## 复现与边界", "",
        f"- 仓库基线 `{metadata['upstream_commit']}`，数据 SHA-256 `{metadata['dataset_sha256']}`。",
        "- 运行 `python scripts/eval_pop.py --output data/pop-rerun`，重新生成策略文件和原始事件。",
        "- 恢复基线可传 `--policy <运行目录>/releases/baseline.json --split validation`，并选一个新的输出目录。",
        "- 合成留出集反映这批未参与优化的场景，官方隐藏集表现、真实通话时延和云客服表现须另外测量。",
        "- 先用开发集验证了候选中的通用行为。看到留出结果后保留当前结果，后续修正需用新的独立案例复验。",
        "",
    ])
    return "\n".join(lines)


def archive(run: Path, output: Path) -> None:
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as bundle:
        for path in sorted(run.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}:
                bundle.write(path, path.relative_to(run).as_posix())
        data_dir = ROOT / "eval/pop"
        for name in ("judge_calibration.json",):
            path = data_dir / name
            if path.is_file():
                bundle.write(path, name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    document = load(args.run / "results.json")
    source_notes = ROOT / "eval/pop/赛题资料解析.md"
    if source_notes.is_file():
        shutil.copyfile(source_notes, args.run / source_notes.name)
    (args.run / "POP自测报告.md").write_text(markdown_report(args.run, document), encoding="utf-8")
    if args.bundle:
        archive(args.run, args.bundle)
