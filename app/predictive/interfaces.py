"""Predictive dialogue interfaces: pre-generate, classify, match, fill, re-predict.

The pipeline is deliberately independent from the current LLMAgent so it can be
tested and rolled out behind a flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Classification:
    label: str
    stage: str
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    stage: str
    label: str
    text: str
    next_stage: str | None = None
    source: str = "prepared"


@dataclass
class TurnDecision:
    text: str
    hit: bool
    stage: str
    next_stage: str | None
    classification: Classification
    output_classification: Classification
    candidate: Candidate | None = None
    filler_used: bool = False
    needs_llm: bool = False
    prepared_candidates: list[Candidate] = field(default_factory=list)


class IntentClassifier(Protocol):
    def classify(self, text: str, stage: str, context: dict[str, Any]) -> Classification: ...


class CandidateStore(Protocol):
    def prepare(self, stage: str, context: dict[str, Any]) -> list[Candidate]: ...
    def match(self, stage: str, label: str) -> Candidate | None: ...


class PredictivePipeline:
    """Orchestrates the pre-generation / match / filler / next prediction loop."""

    def __init__(self, classifier: IntentClassifier, store: CandidateStore,
                 fillers: dict[str, str] | None = None) -> None:
        self.classifier = classifier
        self.store = store
        self.fillers = fillers or {}

    def prepare(self, stage: str, context: dict[str, Any]) -> list[Candidate]:
        return self.store.prepare(stage, context)

    def handle(self, text: str, stage: str, context: dict[str, Any]) -> TurnDecision:
        # Pre-generation is part of the request path: the first turn must not
        # miss merely because nobody called prepare() manually.
        self.store.prepare(stage, context)
        # Both hit and filler paths pass through the same classifier.
        classification = self.classifier.classify(text, stage, context)
        candidate = self.store.match(stage, classification.label)
        if candidate is None:
            filler = self.fillers.get(classification.label) or self.fillers.get("default") or "好的，我收到您的信息了。亲，请您稍等。"
            candidate = Candidate(
                candidate_id=f"{stage}:fallback",
                stage=stage,
                label=classification.label,
                text=filler,
                next_stage=None,
                source="filler",
            )
        filler_used = candidate.source == "filler"
        output = candidate.text
        next_stage = candidate.next_stage
        output_classification = self.classifier.classify(output, stage, context)
        # Prepare the next turn before returning the current output.
        prepared = self.store.prepare(next_stage or stage, context)
        return TurnDecision(
            text=output,
            hit=not filler_used,
            stage=stage,
            next_stage=next_stage,
            classification=classification,
            output_classification=output_classification,
            candidate=candidate,
            filler_used=filler_used,
            needs_llm=filler_used,
            prepared_candidates=prepared,
        )
