"""Replay and verify the competition agent with its real local writeback adapter."""
from __future__ import annotations

import json
import re
from collections import Counter
from itertools import chain
from pathlib import Path

from ..agent.pop_competition import AUDIT_RESULT, OPENING, RECORDING, PopCompetitionAgent
from ..agent.pop_scene_followup import load_scene_policy
from ..compliance import ComplianceGuard
from ..events import EventBus
from ..orchestrator import Orchestrator
from ..tools.pop_writeback import TOOL, PopContactStore
from ..voice.fake import FakeTts
from .pop_runner import KNOWLEDGE, read_json, write_json

SPLITS = ("dev", "validation", "test")
RECORD = "pop_contact_recorded"
CLOSED_SAFELY = "safe_exit"
WRITE_EVENTS = {"returned": "ToolReturned", "failed": "ToolFailed"}
SEVERE = {"high", "critical", "fatal", "serious"}
FOLLOWUP_EVIDENCE = {
    "channel": r"京麦.{0,24}(?:申请|驳回|书面)|(?:申请|驳回|书面).{0,24}京麦",
    "review": r"复审|重新审核|审核为准|以.{0,12}(?:审核|申请页面|审核结果)为准",
    "private": r"不能.{0,12}(?:号码|证件|隐私|提供)|不(?:能|便).{0,6}(?:提供|透露)|为保护.{0,8}信息",
    "approval": r"不能.{0,12}(?:保证|承诺|宣称)|(?:结果|通过与否).{0,12}(?:复审|审核)为准",
    "unknown": r"(?:看不到|无法确认|不能确认|没有.{0,8}记录|核实不了).{0,40}(?:原申请|书面意见|京麦)",
}


def load_cases(path: Path):
    cases = read_json(path)["cases"]
    docs = {doc["id"]: doc for doc in read_json(KNOWLEDGE)}
    grouped = {split: [] for split in SPLITS}
    family_splits, ids, utterances = {}, set(), {}
    for case in cases:
        key, split, family = case["id"], case["split"], case["family"]
        doc = docs.get("pop-scene-" + case["doc"])
        if (not re.fullmatch(r"[a-z0-9-]+", key) or key in ids or split not in grouped
                or not doc or doc["scene"].get("mode") != "电话" or not doc["scene"].get("l3")
                or family_splits.setdefault(family, split) != split):
            raise ValueError(f"Invalid or overlapping closure case {key}")
        ids.add(key)
        expected_successes = 0
        for step in case["turns"]:
            utterance, outcome, *tool = step
            normalized = re.sub(r"\W", "", utterance)
            if not normalized or utterances.setdefault(normalized, split) != split:
                raise ValueError(f"Duplicate utterance across splits: {key}")
            if len(tool) > 1 or (tool and tool[0] not in WRITE_EVENTS):
                raise ValueError(f"Invalid tool expectation: {key}")
            if tool == ["returned"]:
                expected_successes += 1
                if outcome != RECORD:
                    raise ValueError(f"Writeback receipt without recorded outcome: {key}")
            if tool == ["failed"] and outcome != "writeback_failed":
                raise ValueError(f"Failed tool expectation without failure outcome: {key}")
        if not case["turns"] or expected_successes > 1:
            raise ValueError(f"Invalid closure path: {key}")
        grouped[split].append(case)
    if any(not cases for cases in grouped.values()):
        raise ValueError("Each closure split must contain at least one case")
    return grouped


def score(case, context, turns, events, records):
    stages = [event for event in events if event["type"] == "BusinessStageReached"]
    speech = [event for event in events if event["type"] == "AgentSentence"]
    write_events = [event for event in events if event["payload"].get("tool") == TOOL
                    and event["type"] in {"ToolCalled", "ToolReturned", "ToolFailed"}]
    checks = []

    def check(label, passed, *, serious=False, **evidence):
        checks.append({"check": label, "passed": bool(passed), "serious": serious, "evidence": evidence})

    check("outbound_opening", turns[0]["outcome"] == "pending_identity"
          and OPENING in turns[0]["sentences"] and RECORDING in turns[0]["sentences"])
    returned = []
    expected_stages = ["OPEN"]
    already_recorded = False
    expected_phase = "pending_identity"
    offer_remedy = ""
    for index, (spec, turn) in enumerate(zip(case["turns"], turns[1:], strict=True), 1):
        _, outcome, *tool = spec
        turn_events = [event for event in write_events if event["turn_id"] == turn["turn_id"]]
        actual_types = [event["type"] for event in turn_events]
        expected_types = ["ToolCalled", WRITE_EVENTS[tool[0]]] if tool else []
        text = "".join(turn["sentences"])
        if outcome == "pending_need" and expected_phase == "pending_identity":
            expected_stages.append("IDENTITY_CONFIRM")
        if outcome == "pending_result" and expected_phase == "pending_need":
            expected_stages.extend(("NEED_CONFIRM", "OFFER"))
            citation = (turn.get("result", {}).get("citation") or {}).get("doc_id")
            remedy = turn.get("result", {}).get("remediation", "")
            offer_remedy = remedy.removeprefix("请").rstrip("。")
            check("grounded_offer", citation == context["doc_id"] and bool(remedy)
                  and remedy.removeprefix("请").rstrip("。") in text and AUDIT_RESULT in text,
                  turn=index, citation=citation)
        if tool:
            expected_stages.append("RESULT_CONFIRM")
            if tool[0] == "returned":
                expected_stages.append("WRITEBACK")
        if outcome == "pending_identity" and turn["outcome"] == "pending_need":
            check("identity_assent", False, serious=True, turn=index, said=turn["user_text"])
        if (expected_phase == "pending_need" and outcome == "pending_result"
                and turn["outcome"] == "pending_need"):
            check("missed_availability", False, turn=index, said=turn["user_text"])
        if outcome == RECORD and tool == ["returned"] and not turn_events and turn["outcome"] == "pending_result":
            check("missed_confirmation", False, turn=index, said=turn["user_text"])
        if outcome == CLOSED_SAFELY and turn["outcome"] != CLOSED_SAFELY and re.search(
                "别再|不要|不用继续|先不", turn["user_text"]):
            check("optout_not_honored", False, serious=True, turn=index, said=turn["user_text"])
        check("expected_outcome", turn["outcome"] == outcome and not turn["error"], turn=index,
              expected=outcome, actual=turn["outcome"], error=turn["error"])
        check("write_on_expected_turn", expected_types == actual_types, serious=bool(actual_types),
              turn=index, expected=expected_types, actual=actual_types)
        if tool and actual_types == expected_types:
            call, result = turn_events
            confirmation = next((e for e in stages if e["turn_id"] == turn["turn_id"]
                                 and e["payload"]["stage"] == "RESULT_CONFIRM"), None)
            check("confirmation_before_write", confirmation and confirmation["seq"] < call["seq"] < result["seq"],
                  serious=True, turn=index)
            args = call["payload"].get("args_redacted", {})
            wanted = {"request_id": context["request_id"], "knowledge_id": context["doc_id"],
                      "handling_mode": "电话", "contact_outcome": "merchant_understood"}
            check("write_fields", args == wanted, serious=True, turn=index, actual=args)
            if tool[0] == "returned":
                receipt = result["payload"].get("data_summary", {})
                stage = next((e for e in stages if e["turn_id"] == turn["turn_id"]
                              and e["payload"]["stage"] == "WRITEBACK"), None)
                check("persisted_receipt", receipt in records and all(receipt.get(k) == v for k, v in wanted.items())
                      and receipt.get("session_id") == call["session_id"]
                      and receipt.get("confirmed_turn_id") == turn["turn_id"]
                      and not result["payload"].get("cached") and stage and result["seq"] < stage["seq"],
                      serious=True, turn=index)
                returned.append(receipt)
                already_recorded = True
            else:
                check("honest_write_failure", "没能记录" in text and "京麦" in text, serious=True, turn=index)
        check("no_false_recorded_claim", "沟通结果已记录" not in text or already_recorded,
              serious=True, turn=index)
        topic = case.get("followup_checks", {}).get(str(index))
        if topic:
            if topic in {"action", "repeat", "confusion"}:
                passed = bool(offer_remedy and offer_remedy in text)
            else:
                passed = bool(re.search(FOLLOWUP_EVIDENCE[topic], text))
            check("followup_" + topic, passed, turn=index, reply=text)
        expected_phase = outcome

    actual_stages = [event["payload"]["stage"] for event in stages]
    check("stage_sequence", actual_stages == expected_stages, expected=expected_stages, actual=actual_stages)
    check("database_matches_receipts", records == returned, serious=True,
          database_count=len(records), returned_count=len(returned))
    identity_seq = next((e["seq"] for e in stages if e["payload"]["stage"] == "IDENTITY_CONFIRM"), float("inf"))
    check("no_disclosure_before_identity", not any(
        e["seq"] < identity_seq and ("审核问题是" in e["payload"]["text"] or AUDIT_RESULT in e["payload"]["text"])
        for e in speech), serious=True)
    check("no_severe_compliance", not any(
        e["type"] in {"ComplianceBlocked", "ComplianceViolation"} and e["payload"].get("severity") in SEVERE
        for e in events), serious=True)
    failures = [item for item in checks if not item["passed"]]
    return {"passed": not failures, "failure_labels": sorted({item["check"] for item in failures}),
            "severe_cases": int(any(item["serious"] for item in failures)),
            "expected_records": sum(len(step) == 3 and step[2] == "returned" for step in case["turns"]),
            "record_count": len(records), "checks": checks}


async def replay(case, index, policy, scene_policy, output, *, turn_intent=None, resolve_followups=False,
                 simulator=None, phone_style=None):
    path = output / case["id"]
    context = {"request_id": f"Q985{index:03d}", "doc_id": "pop-scene-" + case["doc"], "brand": "合成验收店铺"}
    bus = EventBus(path, policy.version, "pop-closure-rsi", mode="text")
    store = PopContactStore(path / "index" / "pop_contacts.db", bus)
    agent = PopCompetitionAgent(KNOWLEDGE, context, store, policy=policy, scene_policy=scene_policy,
                                turn_intent=turn_intent, resolve_followups=resolve_followups, phone_style=phone_style)
    runtime = Orchestrator(bus, agent, None, ComplianceGuard(), policy.version,
                           "pop_qualification_audit", tts=FakeTts(), tts_provider="fake")
    turns, steps = [], []
    script = simulator.steps(turns) if simulator is not None else iter(case["turns"])
    try:
        session = await runtime.create_session("text")
        for index, (user, *spec) in enumerate(chain([[""]], script)):
            store.fault = spec[1:] == ["failed"]
            said = await runtime.handle_text(session.session_id, user, outbound_opening=index == 0)
            payload = said.get("result") or {}
            outcome = payload.get("outcome")
            if index:
                expected, *tool = spec
                if isinstance(expected, tuple):
                    expected = outcome if outcome in expected else expected[-1]
                if tool == ["if_returned"]:
                    tool = ["returned"] if expected == RECORD else []
                steps.append([user, expected, *tool])
            turns.append({"turn_id": said["turn_id"], "user_text": user, "sentences": said.get("sentences", []),
                          "outcome": outcome, "result": payload.get("result") or {},
                          "error": said.get("error")})
        await runtime.end_session(session.session_id, "closure_evaluation_complete")
        events = bus.read_events(session.session_id)
        records = [json.loads(row[0]) for row in store.conn.execute("SELECT record FROM contacts")]
    finally:
        store.close()
        bus.close()
    result = score({**case, "turns": steps}, context, turns, events, records)
    if simulator is not None:
        goals = simulator.result
        extra = goals["failures"]
        result["passed"] = result["passed"] and not extra
        result["failure_labels"] = sorted(set(result["failure_labels"]) | set(extra))
        result.update(simulator=goals)
    trace = path / "trace.json"
    write_json(trace, {"session_id": session.session_id, "case": case["id"], "context": context,
                       "turns": turns, "records": records, "score": result,
                       **({"executed_script": steps} if simulator is not None else {})})
    return {"case": case["id"], "family": case["family"], "trace": str(trace),
            "events": str(bus.events_path(session.session_id)), **result}


def summarize(rows):
    return {"cases": len(rows), "passed": sum(row["passed"] for row in rows),
            "closed_correctly": sum(row["passed"] and row["expected_records"] > 0 for row in rows),
            "expected_to_record": sum(row["expected_records"] > 0 for row in rows),
            "severe_cases": sum(row["severe_cases"] for row in rows),
            "failure_clusters": dict(Counter(label for row in rows for label in row["failure_labels"]))}
