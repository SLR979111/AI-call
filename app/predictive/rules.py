"""Rule classifier and pre-generated POP candidates for the predictive pipeline."""
from __future__ import annotations

import re
from typing import Any

from ..agent.pop_presets import preset_for
from .interfaces import Candidate, Classification

NEXT_STAGE = {
    "REASON": "REMEDIATION",
    "REMEDIATION": "CONFIRM_ACTION",
    "CONFIRM_ACTION": "WRITEBACK",
    "WRITEBACK": "CLOSE",
    "CLOSE": "END",
}

EXEMPLARS = {
    "affirmative": "好",
    "deny_identity": "不是本人",
    "closing": "好的，谢谢",
    "transfer": "我要投诉",
    "abuse": "你们是不是骗子",
    "busy": "我现在忙",
    "negative": "不用了",
    "confused": "什么意思",
    "already": "已经提交了",
    "question_identity": "你叫啥",
    "other": "今天天气不错",
}


class RuleClassifier:
    def classify(self, text: str, stage: str, context: dict[str, Any]) -> Classification:
        compact = re.sub(r"[\s，,。.!！?？]", "", text or "")
        if not compact:
            return Classification("empty", stage, 0.95, "empty text")
        if any(x in compact for x in ("不是本人", "不是负责人", "不是店铺负责人", "不是老板",
                                      "打错了", "找错了", "我不是")):
            return Classification("deny_identity", stage, 0.99, "denial markers")
        if compact in {"不是", "不对"}:
            return Classification("deny_identity", stage, 0.9, "short denial")
        if any(x in compact for x in ("骗子", "骚扰", "别再打", "不要打", "举报")):
            return Classification("abuse", stage, 0.95, "abuse marker")
        if any(x in compact for x in ("投诉", "转人工", "人工客服", "找人工")):
            return Classification("transfer", stage, 0.95, "transfer marker")
        if any(x in compact for x in ("谢谢", "再见", "拜拜", "知道了", "明白了")):
            return Classification("closing", stage, 0.9, "closing marker")
        if any(x in compact for x in ("忙", "没空", "晚点", "稍后", "等会", "发短信")):
            return Classification("busy", stage, 0.9, "busy marker")
        if any(x in compact for x in ("不用", "不需要", "不想", "没兴趣", "不补", "不提交", "拒绝")):
            return Classification("negative", stage, 0.9, "negative marker")
        if any(x in compact for x in ("你是谁", "你叫啥", "你叫什么", "哪位", "干什么的", "什么客服")):
            return Classification("question_identity", stage, 0.9, "identity question")
        if any(x in compact for x in ("为什么", "什么意思", "没听懂", "再说一遍", "怎么", "什么")):
            return Classification("confused", stage, 0.85, "confusion marker")
        if any(x in compact for x in ("已经提交", "补过了", "提交了", "更新了")):
            return Classification("already", stage, 0.9, "already submitted")
        if compact in {"好", "好的", "行", "可以", "方便", "对", "是的", "嗯", "好呀", "是"}:
            return Classification("affirmative", stage, 0.95, "short affirmative")
        if len(compact) <= 5 and any(x in compact for x in ("好", "行", "可以", "方便", "对", "是", "嗯")):
            return Classification("affirmative", stage, 0.8, "broad affirmative")
        # Deliberately broad fallback: never return an unusable empty label
        # for non-empty merchant text; the store has a pre-generated filler.
        return Classification("other", stage, 0.5, "broad fallback")


class PresetCandidateFactory:
    """Build a small pre-generated candidate set per stage."""

    def build(self, stage: str, context: dict[str, Any]) -> list[Candidate]:
        candidates: list[Candidate] = []
        for label, exemplar in EXEMPLARS.items():
            preset = preset_for(stage, exemplar, context, verified=True)
            if preset is None:
                continue
            # Closing text is only a candidate at the closing stage; at other
            # stages the preset function returns the current stage explanation.
            if label == "closing" and stage not in {"CLOSE", "END"}:
                continue
            text = preset.reply
            if not text or label == "other":
                continue
            candidates.append(Candidate(
                candidate_id=f"{stage}:{label}",
                stage=stage,
                label=label,
                text=text,
                next_stage=NEXT_STAGE.get(stage),
                source="prepared",
            ))
        candidates.append(Candidate(
            candidate_id=f"{stage}:fallback",
            stage=stage,
            label="fallback",
            text="好的，我收到您的信息了。亲，请您稍等。",
            next_stage=None,
            source="filler",
        ))
        return candidates


class InMemoryCandidateStore:
    def __init__(self, factory: PresetCandidateFactory | None = None) -> None:
        self.factory = factory or PresetCandidateFactory()
        self.cache: dict[str, list[Candidate]] = {}

    def prepare(self, stage: str, context: dict[str, Any]) -> list[Candidate]:
        if stage not in self.cache:
            self.cache[stage] = self.factory.build(stage, context)
        return list(self.cache[stage])

    def match(self, stage: str, label: str) -> Candidate | None:
        candidates = self.cache.get(stage, [])
        for candidate in candidates:
            if candidate.label == label:
                return candidate
        for candidate in candidates:
            if candidate.source == "filler":
                return candidate
        return None
