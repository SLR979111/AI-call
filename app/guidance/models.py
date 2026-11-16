"""Ticket-to-guidance document contract and models."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TicketInput(BaseModel):
    """Normalized ticket fields needed for a phone guidance document."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str = Field(min_length=1, max_length=64)
    request_id: str = ""
    brand: str = ""
    reject_l1: str = ""
    reject_l2: str = ""
    reject_l3: str = ""
    remediation: str = ""
    materials: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)


class GuidanceScript(BaseModel):
    opening: str
    reason: str
    action: str
    closing: str


class GuidanceDocument(BaseModel):
    ticket_id: str
    request_id: str
    brand: str
    title: str
    reason: str
    materials: list[str]
    steps: list[str]
    script: GuidanceScript
    forbidden: list[str]
    checkpoints: list[str]
    source: Literal["template", "template_fallback", "llm"]
    approved: bool = False
    version: int = 1
    created_at: str

    @staticmethod
    def new(**kwargs: object) -> "GuidanceDocument":
        return GuidanceDocument(
            created_at=datetime.now(timezone.utc).isoformat(),
            **kwargs,
        )


class ApprovalRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=64)
