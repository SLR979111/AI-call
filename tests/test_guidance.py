"""Tests for ticket-to-guidance document generation."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.guidance.generator import template_document
from app.guidance.models import TicketInput
from app.guidance.service import GuidanceService


def ticket() -> TicketInput:
    return TicketInput(
        ticket_id="T100",
        request_id="Q983001",
        brand="青禾",
        reject_l1="入驻",
        reject_l2="公司主体",
        reject_l3="主体资质到期",
        remediation="更新有效资质后重新提交",
    )


class FakeLLM:
    configured = True

    def __init__(self, text: str = "", *, ok: bool = True, raise_error: bool = False) -> None:
        self.text = text
        self.ok = ok
        self.raise_error = raise_error

    async def complete(self, *args, **kwargs):
        if self.raise_error:
            raise RuntimeError("llm down")
        return SimpleNamespace(ok=self.ok, text=self.text)


def test_template_contains_reason_and_steps():
    doc = template_document(ticket())
    assert doc.reason == "入驻 / 公司主体 / 主体资质到期"
    assert doc.script.reason
    assert doc.steps
    assert doc.approved is False
    assert doc.source == "template"


@pytest.mark.asyncio
async def test_service_generates_and_approves(tmp_path):
    service = GuidanceService(tmp_path)
    doc = await service.generate(ticket(), llm=None)
    assert service.load("T100") is not None
    assert "退回原因" in service.markdown(doc)
    assert "更新有效资质" in doc.script.action
    approved = service.approve("T100", "reviewer")
    assert approved is not None and approved.approved is True
    assert approved.version == doc.version + 1


@pytest.mark.asyncio
async def test_llm_success_is_used(tmp_path):
    payload = {
        "title": "电话指导", "reason": "资质到期",
        "materials": ["新资质"], "steps": ["补材料", "重交"],
        "script": {"opening": "您好", "reason": "原因是资质到期。",
                   "action": "请补新资质。", "closing": "清楚了吗？"},
        "forbidden": ["保证通过"], "checkpoints": ["material"],
    }
    service = GuidanceService(tmp_path)
    doc = await service.generate(ticket(), llm=FakeLLM(json.dumps(payload, ensure_ascii=False)))
    assert doc.source == "llm"
    assert doc.title == "电话指导"
    assert doc.script.action == "请补新资质。"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_template(tmp_path):
    service = GuidanceService(tmp_path)
    doc = await service.generate(ticket(), llm=FakeLLM(raise_error=True))
    assert doc.source == "template_fallback"
    assert doc.reason == "入驻 / 公司主体 / 主体资质到期"


@pytest.mark.asyncio
async def test_llm_forbidden_promise_falls_back(tmp_path):
    payload = {
        "title": "保证通过", "reason": "资质到期",
        "materials": [], "steps": [],
        "script": {"opening": "您好", "reason": "保证通过", "action": "", "closing": ""},
    }
    service = GuidanceService(tmp_path)
    doc = await service.generate(ticket(), llm=FakeLLM(json.dumps(payload, ensure_ascii=False)))
    assert doc.source == "template_fallback"
