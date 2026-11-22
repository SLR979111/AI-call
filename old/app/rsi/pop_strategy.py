from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..agent.pop_audit import PopAuditAgent
from ..agent.pop_strategy import PopStrategyAgent


BASELINE = "pop-rule-c430d49"
FOLLOWUP_SWITCH = "answer_followups"
SWITCH_LABELS = {
    "identity_first": {
        "identity_confirmation", "privacy_before_identity", "safe_exit_evidence",
    },
    "case_grounding": {
        "route_mismatch", "missing_reason", "missing_remedy",
    },
    "callback_receipt": {
        "callback_request_id", "callback_order", "callback_writeback",
        "callback_failure_handling", "callback_false_claim", "unauthorized_tool",
    },
}
_SWITCH_INSTRUCTIONS = {
    "identity_first": "开场告知身份和录音，单独确认是否为本店铺负责人。等对方明确确认后再说单据详情；错人、拒绝或要求停止时礼貌结束。",
    "case_grounding": "根据当前申请单的三级分类精确查询制度资料，按条目规定区分电话和京麦。说明具体原因、整改动作和下一步，资料缺失或冲突时请求核实，勿猜测。",
    "callback_receipt": "回电只用系统提供的当前申请单号。仅在回电工具成功并返回有效工单时告知登记成功；失败时明确告知未登记，避免重复创建。",
}


def baseline_policy() -> dict[str, Any]:
    return {
        "release_id": BASELINE,
        "parent_release_id": None,
        "switches": dict.fromkeys(SWITCH_LABELS, False),
        "observed_failures": {},
    }


def _candidate_id(parent: str, switches: dict[str, bool]) -> str:
    content = json.dumps([parent, switches], sort_keys=True).encode("utf-8")
    return "pop-rule-candidate-" + hashlib.sha256(content).hexdigest()[:12]


def validate_policy(policy: dict[str, Any]) -> None:
    if set(policy) != set(baseline_policy()):
        raise ValueError("Unexpected POP policy fields")
    switches = policy["switches"]
    if not isinstance(switches, dict) or set(switches) not in (
        set(SWITCH_LABELS), set(SWITCH_LABELS) | {FOLLOWUP_SWITCH},
    ):
        raise ValueError("Unexpected POP policy switches")
    if any(type(enabled) is not bool for enabled in switches.values()):
        raise ValueError("POP policy switches must be booleans")
    parent = policy["parent_release_id"]
    if parent is None:
        if policy != baseline_policy():
            raise ValueError("POP baseline is fixed")
        return
    if not isinstance(parent, str) or not re.fullmatch(r"pop-rule-(?:c430d49|candidate-[0-9a-f]{12})", parent):
        raise ValueError("Invalid POP parent release")
    if not switches["identity_first"]:
        raise ValueError("Candidate POP policies require identity-first handling")
    if FOLLOWUP_SWITCH in switches and not all(switches.values()):
        raise ValueError("The follow-up strategy requires all business guards")
    if not any(switches.values()) or policy["release_id"] != _candidate_id(parent, switches):
        raise ValueError("POP policy release ID does not match its contents")
    failures = policy["observed_failures"]
    labels_by_switch = {**SWITCH_LABELS, FOLLOWUP_SWITCH: {"dialogue_fit"}}
    if not isinstance(failures, dict) or set(failures) - set(switches):
        raise ValueError("Unexpected POP strategy rationale")
    for switch, labels in failures.items():
        if not switches[switch] or not isinstance(labels, dict) or not labels or set(labels) - labels_by_switch[switch]:
            raise ValueError("Unexpected POP strategy rationale labels")
        if any(type(count) is not int or count < 1 for count in labels.values()):
            raise ValueError("Invalid POP strategy rationale count")


def propose_policy(development_results: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
    """Map observed development failures to reviewable policy switches."""
    validate_policy(baseline)
    switches = dict(baseline["switches"])
    observed = {}
    for switch, triggers in SWITCH_LABELS.items():
        if switches[switch]:
            continue
        labels = {}
        for label in sorted(triggers):
            count = sum(label in result.get("failure_labels", ()) for result in development_results)
            if count:
                labels[label] = count
        if labels:
            switches[switch] = True
            observed[switch] = labels
    if not observed:
        return {**baseline, "switches": switches, "observed_failures": dict(baseline["observed_failures"])}
    switches["identity_first"] = True
    parent = baseline["release_id"]
    return {
        "release_id": _candidate_id(parent, switches),
        "parent_release_id": parent,
        "switches": switches,
        "observed_failures": observed,
    }


def strategy_prompt(policy: dict[str, Any]) -> str:
    """Return optional LLM prompt guidance for the same reviewed policy."""
    validate_policy(policy)
    instructions = [
        _SWITCH_INSTRUCTIONS[key] for key, enabled in policy["switches"].items()
        if enabled and key in _SWITCH_INSTRUCTIONS
    ]
    if policy["switches"].get(FOLLOWUP_SWITCH):
        instructions.append("追问优先回答当轮的问题，沿用已核验的单据与回电结果，只给当前需要的下一步。")
    return "\n".join(instructions)


def propose_followup_policy(previous: dict[str, Any], failed_dialogues: int) -> dict[str, Any]:
    """Use the previous round's semantic failures to enable the follow-up policy."""
    validate_policy(previous)
    if failed_dialogues < 1 or FOLLOWUP_SWITCH in previous["switches"] or not all(previous["switches"].values()):
        raise ValueError("A grounded policy and observed follow-up failures are required")
    parent = previous["release_id"]
    switches = {**previous["switches"], FOLLOWUP_SWITCH: True}
    return {
        "release_id": _candidate_id(parent, switches),
        "parent_release_id": parent,
        "switches": switches,
        "observed_failures": {FOLLOWUP_SWITCH: {"dialogue_fit": failed_dialogues}},
    }


def build_agent(policy: dict[str, Any], knowledge_path: Path, pause_ms: int = 0) -> PopAuditAgent:
    """Build an isolated Agent at the explicitly selected release."""
    validate_policy(policy)
    if policy["release_id"] == BASELINE:
        return PopAuditAgent(knowledge_path, pause_ms)
    return PopStrategyAgent(knowledge_path, pause_ms, **policy["switches"])
