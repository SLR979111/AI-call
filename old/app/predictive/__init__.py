"""Predictive dialogue pipeline exports."""
from .interfaces import Candidate, Classification, PredictivePipeline, TurnDecision
from .rules import InMemoryCandidateStore, PresetCandidateFactory, RuleClassifier

FILLERS = {
    "confused": "我正在核实，请您稍等。",
    "other": "好的，我确认一下，请您稍等。",
    "affirmative": "好的，我确认一下，请您稍等。",
    "default": "好的，请您稍等。",
}


def create_pipeline() -> PredictivePipeline:
    return PredictivePipeline(RuleClassifier(), InMemoryCandidateStore(), FILLERS)


__all__ = [
    "Candidate", "Classification", "PredictivePipeline", "TurnDecision",
    "InMemoryCandidateStore", "PresetCandidateFactory", "RuleClassifier",
    "FILLERS", "create_pipeline",
]
