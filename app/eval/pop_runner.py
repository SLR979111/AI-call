"""Run synthetic POP conversations against a freshly loaded, isolated agent."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..compliance import ComplianceGuard
from ..events import EventBus
from ..orchestrator import Orchestrator
from ..rsi.pop_strategy import build_agent
from ..tools.gateway import MockBusiness, ToolGateway
from ..voice.fake import FakeTts
from .pop_rules import score_case

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge" / "pop_audit.json"
SPLITS = ("dev", "validation", "holdout")
BUSINESS_FIELDS = (
    "request_id", "brand", "shop_name", "reject_l1", "reject_l2", "reject_l3", "protected_contact_ref",
)
SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suites(path: Path) -> dict[str, list[dict]]:
    payload = read_json(path)
    cases = payload["cases"] if isinstance(payload, dict) else payload
    if not cases:
        raise ValueError("empty suite")
    case_ids = set()
    family_splits: dict[str, str] = {}
    suites: dict[str, list[dict]] = {split: [] for split in SPLITS}
    for case in cases:
        case_id = case["case_id"]
        split = case["split"]
        if not SAFE_ID.fullmatch(case_id) or case_id in case_ids or split not in suites:
            raise ValueError(f"invalid or duplicate case ID or split: {case_id}")
        if not case["family"] or not case["steps"] or not case["expected"].get("route"):
            raise ValueError(f"incomplete case: {case_id}")
        if family_splits.setdefault(case["family"], split) != split:
            raise ValueError(f"family overlaps partitions: {case['family']}")
        if set(case["context"]) - set(BUSINESS_FIELDS):
            raise ValueError(f"unexpected public context field: {case_id}")
        if any(not isinstance(value, str) for value in case["context"].values()):
            raise ValueError(f"invalid public context value: {case_id}")
        protected = case["context"].get("protected_contact_ref")
        if protected and not protected.startswith("SYN-"):
            raise ValueError(f"expected a synthetic protected contact reference: {case_id}")
        if case.get("tool_fault") not in {None, "failed"}:
            raise ValueError(f"unsupported tool fault: {case_id}")
        for step in case["steps"]:
            terms = step.get("if_previous_matches", [])
            if not isinstance(step["say"], str) or not step["say"].strip():
                raise ValueError(f"empty simulated utterance: {case_id}")
            if not isinstance(terms, list) or any(not isinstance(term, str) or not term for term in terms):
                raise ValueError(f"invalid simulated condition: {case_id}")
            if type(step.get("verifies_identity", False)) is not bool:
                raise ValueError(f"invalid simulated identity flag: {case_id}")
            if "otherwise" in step and (not isinstance(step["otherwise"], str) or not step["otherwise"].strip()):
                raise ValueError(f"invalid simulated fallback: {case_id}")
        case_ids.add(case_id)
        suites[split].append(case)
    if any(not suite for suite in suites.values()):
        raise ValueError("all three partitions must contain cases")
    return suites


class SimulatedMerchant:
    """The merchant adapts to the previous spoken turn using curated branches."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = iter(enumerate(steps, start=1))

    def next_input(self, previous_sentences: list[str]) -> dict | None:
        previous = "".join(previous_sentences)
        for number, step in self.steps:
            terms = step.get("if_previous_matches", [])
            matched = not terms or any(term in previous for term in terms)
            text = step["say"] if matched else step.get("otherwise")
            if text:
                return {
                    "step": number,
                    "branch": "primary" if matched else "fallback",
                    "user_text": text,
                    "verified_identity": matched and step.get("verifies_identity", False),
                }
        return None


class CaseAgent:
    """Expose the synthetic business record, excluding simulator state and answers."""

    def __init__(self, delegate: Any, context: dict) -> None:
        self.delegate = delegate
        self.context = dict(context)

    async def start_session(self, session_id, version_set, flow_pack):
        start = getattr(self.delegate, "start_session", None)
        if start is not None:
            await start(session_id, version_set, flow_pack)

    def handle(self, request, cancel, tools):
        return self.delegate.handle({**request, "business_context": self.context}, cancel, tools)


async def run_case(case: dict, policy: dict, raw_dir: Path) -> dict:
    release = policy["release_id"]
    case_dir = raw_dir / release / case["split"] / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=False)
    bus = EventBus(case_dir, release, "pop-offline", mode="text")
    mock = MockBusiness()
    if case.get("tool_fault") == "failed":
        mock.fault = "500"
    tools = ToolGateway(case_dir, mock, bus)
    agent = CaseAgent(build_agent(policy, KNOWLEDGE, pause_ms=0), case["context"])
    orchestrator = Orchestrator(
        bus, agent, tools, ComplianceGuard(), release, "pop_qualification_audit",
        tts=FakeTts(), tts_provider="fake",
    )
    merchant = SimulatedMerchant(case["steps"])
    trace: dict = {"turns": [], "events": [], "tickets": [], "release_id": release}
    session_id = None
    previous: list[str] = []
    try:
        session_id = (await orchestrator.create_session("text")).session_id
        while (utterance := merchant.next_input(previous)) is not None:
            output = await orchestrator.handle_text(session_id, utterance["user_text"])
            previous = output.get("sentences", [])
            trace["turns"].append({
                **utterance,
                "turn_id": output["turn_id"],
                "sentences": previous,
                "result": output.get("result") or {},
            })
            if output.get("error"):
                trace["execution_error"] = str(output["error"].get("code", "turn_error"))
                break
        await orchestrator.end_session(session_id, "synthetic_case_complete")
    except Exception as exc:
        trace["execution_error"] = type(exc).__name__
    finally:
        if session_id:
            trace["events"] = bus.read_events(session_id)
        trace["tickets"] = list(mock.tickets.values())
        tools.close()
        bus.close()
    trace["session_id"] = session_id
    trace["case_id"] = case["case_id"]
    trace_file = case_dir / "trace.json"
    write_json(trace_file, {key: value for key, value in trace.items() if key != "events"})
    score = score_case(case, trace)
    return {
        **score,
        "case_id": case["case_id"],
        "family": case["family"],
        "split": case["split"],
        "route": case["expected"]["route"],
        "release_id": release,
        "turn_count": len(trace["turns"]),
        "trace": trace_file.relative_to(raw_dir.parent).as_posix(),
        "events": (case_dir / "sessions" / session_id / "events.jsonl").relative_to(raw_dir.parent).as_posix()
        if session_id else None,
    }
