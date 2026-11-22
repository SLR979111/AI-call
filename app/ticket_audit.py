"""Evidence for deciding whether a ticket intent should be excluded.

``TICKET_FORECAST_EXCLUDE`` is a manual switch.  This module measures how often
the existing lexical decision engine actually selects each generated ticket
intent across a set of user turns, without changing the rule engine or the
candidate generator.

Only an intent with zero selections in both a fitting set and a held-out set is
a removal candidate; one fitting set is never enough evidence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .context_aware_decision import ContextAwareDecisionEngine
from .models import Candidate, TaskCard
from .ticket_forecast import generate_ticket_candidates


@dataclass
class AuditReport:
    task: str
    tickets: int
    turns: int
    hits: dict[str, int] = field(default_factory=dict)
    selected_ids: list[str] = field(default_factory=list)
    per_turn: list[dict] = field(default_factory=list)
    never_selected: list[str] = field(default_factory=list)


def audit_intents(card: TaskCard, turns: list[str], *,
                  candidates: list[Candidate] | None = None,
                  engine: ContextAwareDecisionEngine | None = None,
                  assistant_text: str = "",
                  title: str = "") -> AuditReport:
    """Count which generated ticket intents the lexical rules select.

    Model candidates are deliberately absent: the question is whether the
    deterministic ticket text is reachable on its own.  ``assistant_text`` can
    be set when the audited turns answer a fixed previous question.
    """
    items = (candidates if candidates is not None
             else generate_ticket_candidates(card))
    engine = engine or ContextAwareDecisionEngine()
    hits = {item.intent: 0 for item in items if item.intent}
    selected_ids: list[str] = []
    per_turn: list[dict] = []
    for text in turns:
        decision, selected = engine.should_reuse(text, items, assistant_text, card)
        intent = selected.intent if selected is not None else None
        if intent and intent in hits:
            hits[intent] += 1
        if selected is not None:
            selected_ids.append(selected.id)
        per_turn.append({
            "input": text,
            "decision": decision,
            "candidate_id": selected.id if selected is not None else None,
            "intent": intent,
        })
    return AuditReport(
        task=title or card.title,
        tickets=len(items),
        turns=len(turns),
        hits=hits,
        selected_ids=selected_ids,
        per_turn=per_turn,
        never_selected=sorted(intent for intent, count in hits.items() if count == 0),
    )


def suggest_exclusions(fit: AuditReport, heldout: AuditReport) -> list[str]:
    """Intents with zero selections in both the fitting and held-out sets."""
    return sorted(set(fit.never_selected) & set(heldout.never_selected))


def read_turns(path: str | Path) -> list[str]:
    """One user turn per line; blank lines and ``#`` comments are ignored."""
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _load_card(path: str | Path) -> TaskCard:
    return TaskCard.model_validate_json(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit which ticket forecast intents the lexical rules select.")
    parser.add_argument("--task", required=True, help="TaskCard JSON path")
    parser.add_argument("--fit", required=True, help="fitting turns, one per line")
    parser.add_argument("--heldout", help="held-out turns, one per line")
    parser.add_argument("--assistant-text", default="",
                        help="fixed previous assistant question, if any")
    args = parser.parse_args(argv)

    card = _load_card(args.task)
    fit = audit_intents(card, read_turns(args.fit),
                        assistant_text=args.assistant_text)
    result: dict = {"fit": asdict(fit)}
    if args.heldout:
        heldout = audit_intents(card, read_turns(args.heldout),
                                assistant_text=args.assistant_text)
        result["heldout"] = asdict(heldout)
        result["suggested_exclusions"] = suggest_exclusions(fit, heldout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
