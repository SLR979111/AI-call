"""Ticket-to-guidance API."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field

from ..config import settings
from .models import ApprovalRequest, GuidanceDocument, TicketInput
from .service import GuidanceService

router = APIRouter(prefix="/tickets/guidance", tags=["guidance"])


class GenerateBody(TicketInput):
    use_llm: bool = Field(default=True, description="Try configured LLM, fall back to template")


def _service() -> GuidanceService:
    return GuidanceService(Path(settings.data_dir) / "guidance")


@router.post("", response_model=None)
async def generate(request: Request, body: GenerateBody) -> dict:
    ticket = TicketInput.model_validate(body.model_dump(exclude={"use_llm"}))
    llm = getattr(request.app.state, "llm", None) if body.use_llm else None
    doc = await _service().generate(ticket, llm)
    return {"document": doc.model_dump(), "markdown": _service().markdown(doc)}


@router.get("", response_model=None)
def list_guidance() -> dict:
    docs = _service().list()
    return {"items": [{"ticket_id": d.ticket_id, "title": d.title, "approved": d.approved,
                       "version": d.version, "source": d.source} for d in docs]}


@router.get("/{ticket_id}", response_model=None)
def get_guidance(ticket_id: str) -> GuidanceDocument:
    doc = _service().load(ticket_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="guidance not found")
    return doc


@router.get("/{ticket_id}/markdown", response_class=PlainTextResponse)
def get_markdown(ticket_id: str) -> str:
    doc = _service().load(ticket_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="guidance not found")
    return _service().markdown(doc)


@router.post("/{ticket_id}/approve", response_model=None)
def approve(ticket_id: str, body: ApprovalRequest) -> GuidanceDocument:
    doc = _service().approve(ticket_id, body.approver)
    if doc is None:
        raise HTTPException(status_code=404, detail="guidance not found")
    return doc
