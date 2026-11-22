"""Guidance storage, validation, approval, and Markdown rendering."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .generator import llm_messages, parse_llm_document, template_document
from .models import GuidanceDocument, TicketInput


class GuidanceService:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_id(ticket_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ticket_id).strip("._")
        if not safe:
            raise ValueError("invalid ticket_id")
        return safe[:80]

    def _json_path(self, ticket_id: str) -> Path:
        return self.root / f"{self._safe_id(ticket_id)}.json"

    def _md_path(self, ticket_id: str) -> Path:
        return self.root / f"{self._safe_id(ticket_id)}.md"

    def save(self, doc: GuidanceDocument) -> None:
        self._json_path(doc.ticket_id).write_text(
            json.dumps(doc.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._md_path(doc.ticket_id).write_text(self.markdown(doc), encoding="utf-8")

    def load(self, ticket_id: str) -> GuidanceDocument | None:
        path = self._json_path(ticket_id)
        if not path.exists():
            return None
        return GuidanceDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[GuidanceDocument]:
        docs = []
        for path in sorted(self.root.glob("*.json")):
            try:
                docs.append(GuidanceDocument.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return docs

    async def generate(self, ticket: TicketInput, llm: Any = None) -> GuidanceDocument:
        ticket = TicketInput.model_validate(ticket.model_dump())
        if llm is not None and getattr(llm, "configured", True):
            try:
                result = await llm.complete(
                    llm_messages(ticket),
                    temperature=0.1,
                    max_tokens=900,
                    response_format={"type": "json_object"},
                    prompt_version="ticket_guidance@1",
                )
                if getattr(result, "ok", False):
                    doc = parse_llm_document(ticket, result.text)
                    self.save(doc)
                    return doc
            except Exception:
                # Guidance generation must not block the ticket path; fall back
                # to the deterministic template and mark the source explicitly.
                pass
        doc = template_document(ticket)
        if llm is not None:
            doc.source = "template_fallback"
        self.save(doc)
        return doc

    def approve(self, ticket_id: str, approver: str) -> GuidanceDocument | None:
        doc = self.load(ticket_id)
        if doc is None:
            return None
        doc.approved = True
        doc.version += 1
        self.save(doc)
        return doc

    @staticmethod
    def markdown(doc: GuidanceDocument) -> str:
        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) if items else "- 以本单书面驳回意见为准"

        return f"""# {doc.title}

- 工单：`{doc.ticket_id}`
- 申请单：`{doc.request_id or '—'}`
- 品牌：{doc.brand or '—'}
- 来源：`{doc.source}`
- 审批：{'已通过' if doc.approved else '待审批'}
- 版本：v{doc.version}

## 退回原因

{doc.reason}

## 需要补正

{bullets(doc.materials)}

## 操作步骤

{bullets(doc.steps)}

## 电话话术

**开场：** {doc.script.opening}

**原因：** {doc.script.reason}

**动作：** {doc.script.action}

**收尾：** {doc.script.closing}

## 禁止承诺

{bullets(doc.forbidden)}

## 检查点

{bullets(doc.checkpoints)}
"""
