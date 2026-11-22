"""Allowlisted closure strategy versions shared by offline replay and WebRTC."""
from dataclasses import replace
from pathlib import Path

from ..agent.pop_repair import RESILIENT

VERIFIED_CLOSURE_RELEASE = Path(__file__).resolve().parents[2] / "eval/pop/releases/closure-verified.v1.json"


RULES = {
    "explicit_identity": ("identity_assent", "身份问句接受明确身份陈述或直接确认，通用承接语继续核验。"),
    "natural_availability": ("missed_availability", "已核验后识别完整的方便听、请讲等沟通意愿，忙碌和拒绝优先。"),
    "grounded_confirmation": ("missed_confirmation", "接收明确听懂并说明将按当单要求处理的完整陈述。保留追问、撤回和待澄清的拦截。"),
    "explicit_stop": ("optout_not_honored", "明确要求停止说话或联系时退出。"),
}


def release(flags):
    flags = tuple(sorted(set(flags)))
    if set(flags) - RULES.keys():
        raise ValueError("Unknown closure policy option")
    policy = replace(RESILIENT, **dict.fromkeys(flags, True))
    return {"schema": 1, "parent": RESILIENT.version, "version": policy.version, "enabled": list(flags)}


def load_policy(payload):
    flags = payload.get("enabled")
    if not isinstance(flags, list) or any(type(flag) is not str for flag in flags) or payload != release(flags):
        raise ValueError("Invalid closure release")
    return replace(RESILIENT, **dict.fromkeys(flags, True))


def propose(failures):
    labels = {label for case in failures for label in case["failure_labels"]}
    options = [flag for flag, (failure, _) in RULES.items() if failure in labels]
    if not options:
        return []
    return [release([flag]) for flag in options] + ([release(options)] if len(options) > 1 else [])
