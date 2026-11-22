from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class FlowPackError(Exception):
    pass


@dataclass
class FlowPack:
    id: str
    version: str
    entry: str
    states: dict[str, dict[str, Any]]
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "FlowPack":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        pack = cls(
            id=str(data["id"]),
            version=str(data.get("version", "1.0.0")),
            entry=str(data["entry"]),
            states=dict(data.get("states", {})),
            slots=dict(data.get("slots", {})),
        )
        pack.validate()
        return pack

    def validate(self) -> None:
        if self.entry not in self.states:
            raise FlowPackError(f"entry state not found: {self.entry}")
        for name, state in self.states.items():
            if not isinstance(state, dict):
                raise FlowPackError(f"state must be object: {name}")
            if "next" in state and state["next"] not in self.states:
                raise FlowPackError(f"next state not found: {name} -> {state['next']}")
            for transition in state.get("transitions", []) or []:
                target = transition.get("to")
                if target not in self.states:
                    raise FlowPackError(f"transition target not found: {name} -> {target}")
        for slot, spec in self.slots.items():
            if not isinstance(spec, dict):
                raise FlowPackError(f"slot must be object: {slot}")

    def state(self, name: str) -> dict[str, Any]:
        if name not in self.states:
            raise FlowPackError(f"unknown state: {name}")
        return self.states[name]


class FlowEngine:
    """Deterministic business flow; LLM only proposes intents/slots."""

    def __init__(self, pack: FlowPack) -> None:
        self.pack = pack
        self.current = pack.entry
        self.slots: dict[str, Any] = {}
        self.history: list[str] = [self.current]

    def start(self) -> dict[str, Any]:
        state = self.pack.state(self.current)
        return {"state": self.current, "say": state.get("say", ""), "slots": {}}

    def apply_intent(self, intent: str) -> dict[str, Any]:
        state = self.pack.state(self.current)
        for transition in state.get("transitions", []) or []:
            when = transition.get("when", {})
            if when.get("intent") == intent:
                self.current = transition["to"]
                self.history.append(self.current)
                new_state = self.pack.state(self.current)
                return {"state": self.current, "say": new_state.get("say", ""),
                        "slots": self.slots, "history": self.history}
        if "next" in state:
            self.current = state["next"]
            self.history.append(self.current)
            new_state = self.pack.state(self.current)
            return {"state": self.current, "say": new_state.get("say", ""),
                    "slots": self.slots, "history": self.history}
        return {"state": self.current, "say": state.get("say", ""), "slots": self.slots,
                "history": self.history}

    def set_slot(self, name: str, value: Any) -> None:
        if name not in self.pack.slots:
            raise FlowPackError(f"unknown slot: {name}")
        self.slots[name] = value

    def snapshot(self) -> dict[str, Any]:
        return {"flow_id": self.pack.id, "version": self.pack.version,
                "state": self.current, "slots": dict(self.slots)}
