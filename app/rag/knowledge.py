from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _TOKEN_RE.findall(text)
    # Chinese bigrams improve matching without a segmenter.
    chinese = [t for t in tokens if len(t) == 1 and "\u4e00" <= t <= "\u9fff"]
    bigrams = [chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1)]
    return tokens + bigrams


@dataclass
class Chunk:
    doc_id: str
    title: str
    content: str
    score: float = 0.0
    tags: list[str] = field(default_factory=list)

    def citation(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "title": self.title, "score": round(self.score, 4)}


class KnowledgeBase:
    """Tiny local retrieval layer: token overlap + length normalization.

    No embedding model is required for the fake skeleton; the interface is the
    same one a bge-m3/vector implementation will fill later.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.docs: list[dict[str, Any]] = []
        self._df: dict[str, int] = {}
        if path and path.exists():
            self.docs = json.loads(path.read_text(encoding="utf-8"))
        if not self.docs:
            self.docs = self._default_docs()
        for doc in self.docs:
            for token in set(tokenize(doc.get("title", "") + " " + doc.get("content", ""))):
                self._df[token] = self._df.get(token, 0) + 1

    @staticmethod
    def _default_docs() -> list[dict[str, Any]]:
        return [
            {
                "id": "faq-delivery",
                "title": "配送时间与改约",
                "content": "订单一般在下单后 24 到 48 小时送达；配送中可在送达前改约一次，改约后以短信通知为准。",
                "tags": ["配送", "订单", "改约"],
            },
            {
                "id": "faq-refund",
                "title": "退款与售后",
                "content": "签收后 7 天内可申请无理由退货；退款原路退回，一般 1 到 3 个工作日到账。",
                "tags": ["退款", "售后"],
            },
            {
                "id": "faq-ai",
                "title": "AI 身份与录音说明",
                "content": "本通话由 AI 数字员工发起，通话会录音用于质检；可随时要求转人工。",
                "tags": ["合规", "AI", "录音"],
            },
        ]

    def search(self, query: str, top_k: int = 3) -> list[Chunk]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        n = len(self.docs)
        scored: list[Chunk] = []
        for doc in self.docs:
            text = doc.get("title", "") + " " + doc.get("content", "")
            doc_tokens = tokenize(text)
            if not doc_tokens:
                continue
            counts: dict[str, int] = {}
            for token in doc_tokens:
                counts[token] = counts.get(token, 0) + 1
            score = 0.0
            for token in set(query_tokens):
                tf = counts.get(token, 0)
                if tf:
                    idf = math.log((n + 1) / (self._df.get(token, 0) + 1)) + 1
                    score += (1 + math.log(tf)) * idf
            if score > 0:
                scored.append(Chunk(
                    doc_id=str(doc.get("id")),
                    title=str(doc.get("title", "")),
                    content=str(doc.get("content", "")),
                    score=score,
                    tags=list(doc.get("tags", [])),
                ))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]
