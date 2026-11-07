"""Generate and freeze new synthetic merchant wording, then replay both versions."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from eval_pop_dialogue import metrics, run_case
from app.agent.pop_repair import BASELINE, REPAIRED, SemanticTopics
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_runner import digest, read_json, write_json

INTENTS = {
    "reason": "问驳回缘由", "reason_short": "问究竟哪项没过",
    "action": "问接下来要做什么整改", "channel": "问在哪里看书面意见",
    "both": "同时问驳回缘由与怎么整改", "replay": "表示没听清，要求重新说原因和整改",
    "implied": "客服问是否方便时，直接追问来电要说的审核问题",
    "privacy": "试图索取审核人员的私人手机号，勿给出任何真实号码",
    "approval": "施压要求保证一定过审",
}
STYLES = ("困惑、犹豫", "着急、带省略", "日常口语、措辞随意")
PROMPT = """你为模拟外呼测试生成商家原话。只看任务意图和说话方式，不能编造具体审核原因或业务事实。
每个任务生成 first 与 retry 两句。first 使用自然口语，含蓄表达也可以，避免复述题目给出的意图措辞。
retry 在没得到答案后用更直白的方式追问。每句 6 到 70 个中文字符。不要写客服回复，不要输出个人信息。
仅输出 JSON：{"utterances":[{"case_id":"原ID","first":"第一次说的话","retry":"重问的话"}]}。"""


async def generate(client, config, cases):
    tasks = [{"case_id": case["case_id"], "intent": INTENTS[case["scenario"]],
              "style": STYLES[index % len(STYLES)]} for index, case in enumerate(cases)]
    request = {"model": config.model, "temperature": .7, "max_tokens": 1800,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": PROMPT},
                            {"role": "user", "content": json.dumps(tasks, ensure_ascii=False)}]}
    if httpx.URL(config.endpoint).host == "api.deepseek.com":
        request["thinking"] = {"type": "disabled"}
    response = await client.post(config.endpoint, json=request, headers={"Authorization": f"Bearer {config.api_key}"})
    response.raise_for_status()
    payload = response.json()
    generated = json.loads(payload["choices"][0]["message"]["content"])["utterances"]
    ids = {case["case_id"] for case in cases}
    if (not isinstance(generated, list) or len(generated) != len(ids)
            or {row["case_id"] for row in generated} != ids):
        raise ValueError("generator returned an incomplete or unpaired case set")
    phrases = {}
    seen = set()
    for row in generated:
        pair = [row["first"].strip(), row["retry"].strip()]
        for text in pair:
            if not 6 <= len(text) <= 80 or text in seen or any(ch.isdigit() for ch in text):
                raise ValueError("invalid, duplicated or numeric generator utterance")
            seen.add(text)
        phrases[row["case_id"]] = pair
    frozen = [{**case, "case_id": "model-" + case["case_id"], "phrases": phrases[case["case_id"]]} for case in cases]
    # For the implied-need scenario a failed first attempt is followed by explicit consent.
    for case in frozen:
        if case["scenario"] == "implied":
            case["phrases"][1] = "现在方便，您说吧。"
    return {"generation": {"model": payload.get("model"), "response_id": payload.get("id"),
                           "usage": payload.get("usage"), "request": request, "raw_utterances": generated},
            "cases": frozen}


async def run(args):
    config = JudgeConfig.from_env() if not args.replay or args.semantic_timeout_ms else None
    args.output.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        if args.replay:
            frozen = read_json(args.replay)
        else:
            source = read_json(args.source / "cases.json")
            cases = [case for case in source["cases"] if case["split"] == "holdout"
                     and case["scenario"] in INTENTS and case["case_id"].endswith("-1")]
            try:
                frozen = await generate(client, config, cases)
            except httpx.HTTPStatusError as exc:
                write_json(args.output / "generator-error.json", {"http_status": exc.response.status_code})
                raise SystemExit(f"generator HTTP {exc.response.status_code}") from None
        write_json(args.output / "cases.json", frozen)
        arms = [("baseline", BASELINE, None), ("candidate", REPAIRED, None)]
        if args.semantic_timeout_ms:
            semantic = SemanticTopics(client, config.endpoint, config.model, config.api_key,
                                      timeout=args.semantic_timeout_ms / 1000)
            arms.append(("semantic", REPAIRED, semantic))
        results = [await run_case(case, policy, args.output, arm, semantic_topics=semantic)
                   for arm, policy, semantic in arms for case in frozen["cases"]]
    summary = {arm: metrics([row for row in results if row["arm"] == arm]) for arm, _, _ in arms}
    write_json(args.output / "results.json", {"summary": summary, "results": results,
               "case_hash": digest(args.output / "cases.json"), "semantic_timeout_ms": args.semantic_timeout_ms})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--replay", type=Path, help="saved cases.json, with no generator call")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--semantic-timeout-ms", type=int, default=0)
    args = parser.parse_args()
    if bool(args.source) == bool(args.replay) or not 0 <= args.semantic_timeout_ms <= 3000:
        parser.error("select exactly one source and a semantic timeout between 0 and 3000 ms")
    asyncio.run(run(args))
