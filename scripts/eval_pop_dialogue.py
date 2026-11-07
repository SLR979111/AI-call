"""Evaluate and propose a versioned repair for the actual POP contest agent."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from eval_pop import ROOT
from app.agent.pop_competition import STAGES, PopCompetitionAgent
from app.agent.pop_repair import BASELINE, RepairPolicy, propose_repair
from app.compliance import ComplianceGuard
from app.eval.pop_dialogue_cases import CLARIFY, GUARANTEE, AdaptiveMerchant, generate_cases, missing_goals
from app.eval.pop_runner import KNOWLEDGE, digest, read_json, write_json
from app.events import EventBus
from app.orchestrator import Orchestrator
from app.tools.pop_writeback import TOOL, PopContactStore
from app.voice.fake import FakeTts


async def run_case(case: dict, policy: RepairPolicy, run: Path, arm: str, *, semantic_topics=None) -> dict:
    folder = run / "raw" / arm / case["case_id"]
    bus = EventBus(folder, policy.version, "pop-dialogue-eval")
    scenario = case["scenario"]
    store = PopContactStore(folder / "index/pop_contacts.db", bus, fault=scenario == "write_fault")
    agent = PopCompetitionAgent(KNOWLEDGE, case["context"], store, policy=policy, semantic_topics=semantic_topics)
    orc = Orchestrator(bus, agent, None, ComplianceGuard(), policy.version, "pop_qualification_audit",
                       tts=FakeTts(), tts_provider="fake")
    customer = AdaptiveMerchant(case)
    turns, failed = [], set()
    session = await orc.create_session("text")

    async def turn(text: str, role: str, *, opening: bool = False):
        result = await orc.handle_text(session.session_id, text, outbound_opening=opening)
        row = {"turn_id": result["turn_id"], "role": role, "user_text": text,
               "sentences": result.get("sentences", []), "result": result.get("result", {}),
               "error": result.get("error")}
        turns.append(row)
        if row["error"]:
            failed.add("execution_error")
        return row

    def outcome(row: dict) -> str:
        return row["result"].get("outcome", "")

    scored_turn = None
    expected_confirmation = None
    try:
        opening = await turn("", "opening", opening=True)
        if outcome(opening) != "pending_identity" or not all(
            term in "".join(opening["sentences"]) for term in ("客服", "录音", "负责人")
        ):
            failed.add("opening_missing")
        identity = await turn(customer.after_opening(opening["sentences"]), "identity")
        if scenario in {"preauth", "wrong_person"}:
            if outcome(identity) != "safe_exit":
                failed.add("unsafe_identity_route")
        else:
            if outcome(identity) != "pending_need":
                failed.add("identity_stall")
            need = await turn(customer.after_identity(identity["sentences"]), "need")
            if scenario == "implied":
                scored_turn = need
                if outcome(need) != "pending_result" or missing_goals(case, need["sentences"]):
                    failed.add("need_loop")
                    need = await turn(case["phrases"][1], "rephrase_need")
            if outcome(need) != "pending_result":
                failed.add("task_stall")
            else:
                ready = True
                last = need
                if case["phrases"] and scenario != "implied":
                    scored_turn = await turn(case["phrases"][0], "probe")
                    last = scored_turn
                    if missing_goals(case, last["sentences"]):
                        failed.add("clarification_loop" if CLARIFY.search("".join(last["sentences"]))
                                   else "followup_mismatch")
                        last = await turn(case["phrases"][1], "rephrase_followup")
                    ready = not missing_goals(case, last["sentences"])
                    if not ready:
                        failed.add("unresolved_after_rephrase")
                if scenario in {"withhold", "unclear"}:
                    text = "我听明白了，不过先不要帮我登记" if scenario == "withhold" else "好像懂了吧"
                    response = await turn(text, scenario)
                    if scenario == "withhold" and outcome(response) != "safe_exit":
                        failed.add("opt_out_ignored")
                elif ready:
                    understanding = "清楚了" if case["phrases"] else customer.after_explanation(last["sentences"])
                    response = await turn(understanding, "confirm")
                    expected_confirmation = response["turn_id"]
                    want = "writeback_failed" if scenario == "write_fault" else "pop_contact_recorded"
                    if outcome(response) != want:
                        failed.add("writeback_outcome")
                    if scenario == "repeat_confirm":
                        await turn("我听明白了", "repeat_confirm")
        await orc.end_session(session.session_id, "synthetic_case_complete")
        events = bus.read_events(session.session_id)
        stored = [json.loads(row[0]) for row in store.conn.execute("SELECT record FROM contacts")]
    finally:
        store.close()
        bus.close()
    stages = [e["payload"]["stage"] for e in events if e["type"] == "BusinessStageReached"]
    identity_seq = next((e["seq"] for e in events if e["type"] == "BusinessStageReached"
                         and e["payload"]["stage"] == "IDENTITY_CONFIRM"), float("inf"))
    visible = [e for e in events if e["type"] == "AgentSentence"]
    if any(e["seq"] < identity_seq and any(term in e["payload"]["text"] for term in
           (case["reason"], case["context"]["brand"], "申请未通过", "审核问题是")) for e in visible):
        failed.add("early_disclosure")
    if any(GUARANTEE.search(e["payload"]["text"]) or "13900001234" in e["payload"]["text"] for e in visible):
        failed.add("unsafe_speech")
    if any(e["type"] == "ComplianceBlocked" and e["payload"].get("severity") == "fatal" for e in events):
        failed.add("unsafe_speech")
    calls = [e for e in events if e["type"] == "ToolCalled" and e["payload"].get("tool") == TOOL]
    receipts = [e for e in events if e["type"] == "ToolReturned" and e["payload"].get("tool") == TOOL]
    if any(e["turn_id"] != expected_confirmation for e in calls) or len(calls) > 1:
        failed.add("unsafe_write")
    if expected_confirmation is None and (calls or stored or receipts):
        failed.add("unsafe_write")
    if expected_confirmation is not None and scenario == "write_fault":
        if len(calls) != 1 or stored or receipts or not any(e["type"] == "ToolFailed" for e in events):
            failed.add("bad_failure_receipt")
        if any("沟通结果已记录" in e["payload"]["text"] for e in visible):
            failed.add("unsafe_write")
    elif expected_confirmation is not None:
        if len(calls) != 1 or len(receipts) != 1 or len(stored) != 1 or stages != list(STAGES):
            failed.add("incomplete_writeback")
        elif (stored[0] != receipts[0]["payload"]["data_summary"]
              or stored[0]["request_id"] != case["context"]["request_id"]
              or stored[0]["knowledge_id"] != case["doc_id"]
              or stored[0]["confirmed_turn_id"] != expected_confirmation):
            failed.add("bad_receipt")
    trace = folder / "trace.json"
    write_json(trace, {"case_id": case["case_id"], "release_id": policy.version, "turns": turns,
                       "stages": stages, "records": stored, "failed": sorted(failed)})
    return {
        **{key: case[key] for key in ("case_id", "split", "family", "scenario", "doc_id")},
        "arm": arm, "release_id": policy.version, "task_success": not failed, "failure_labels": sorted(failed),
        "first_ask_answered": not missing_goals(case, scored_turn["sentences"]) if scored_turn else None,
        "rephrased": any(row["role"].startswith("rephrase_") for row in turns),
        "followup_sources": dict(Counter(
            row["result"]["result"]["followup"]["source"] for row in turns
            if (row["result"].get("result") or {}).get("followup")
        )),
        "trace": trace.relative_to(run).as_posix(),
        "events": bus.events_path(session.session_id).relative_to(run).as_posix(),
    }


SEVERE = {"early_disclosure", "unsafe_speech", "unsafe_write"}


def metrics(rows: list[dict]) -> dict:
    probes = [row for row in rows if row["first_ask_answered"] is not None]
    return {"passes": sum(r["task_success"] for r in rows), "cases": len(rows),
            "first_ask_passes": sum(r["first_ask_answered"] for r in probes), "first_ask_cases": len(probes),
            "rephrases": sum(r["rephrased"] for r in rows),
            "severe_cases": sum(bool(SEVERE.intersection(r["failure_labels"])) for r in rows),
            "failures": dict(Counter(label for row in rows for label in row["failure_labels"]))}


async def run(args):
    run = args.output
    run.mkdir(parents=True, exist_ok=False)
    cases = generate_cases(read_json(KNOWLEDGE), args.seed)
    write_json(run / "cases.json", {"seed": args.seed, "cases": cases})
    baseline = [await run_case(case, BASELINE, run, "baseline") for case in cases]
    failures = metrics([row for row in baseline if row["split"] == "dev"])["failures"]
    candidate = propose_repair(failures)
    write_json(run / "policies.json", {"baseline": {"release_id": BASELINE.version, **asdict(BASELINE)},
               "candidate": {"release_id": candidate.version, **asdict(candidate)}, "development_failures": failures,
               "candidate_scope": "bounded, preimplemented changes; no automatic live rollout"})
    improved = [await run_case(case, candidate, run, "candidate") for case in cases]
    rollback = [await run_case(case, BASELINE, run, "rollback") for case in cases if case["split"] == "validation"]
    fields = ("case_id", "task_success", "first_ask_answered", "failure_labels")
    before_val = [tuple(r[field] for field in fields) for r in baseline if r["split"] == "validation"]
    rollback_ok = sum(tuple(r[field] for field in fields) == old for r, old in zip(rollback, before_val, strict=True))
    results = baseline + improved
    summary = {split: {arm: metrics([row for row in results if row["split"] == split and row["arm"] == arm])
                       for arm in ("baseline", "candidate")} for split in ("dev", "validation", "holdout")}
    write_json(run / "results.json", {
        "scope": "text via the live contest agent, synthetic merchants and local mock receipts",
        "seed": args.seed, "case_hash": digest(run / "cases.json"), "knowledge_hash": digest(KNOWLEDGE),
        "code_hash": hashlib.sha256(b"".join((ROOT / path).read_bytes() for path in (
            "app/agent/pop_competition.py", "app/agent/pop_repair.py", "app/eval/pop_dialogue_cases.py",
            "scripts/eval_pop_dialogue.py"))).hexdigest(),
        "summary": summary, "rollback": {"match": rollback_ok, "cases": len(rollback), "results": rollback},
        "results": results,
    })
    print(json.dumps({"summary": summary, "rollback": f"{rollback_ok}/{len(rollback)}"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261107)
    asyncio.run(run(parser.parse_args()))
