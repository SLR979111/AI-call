from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
    """Hybrid retrieval: semantic vector search + TF-IDF fallback.

    When sentence-transformers and chromadb are installed, uses BGE embeddings
    for semantic search. Falls back to TF-IDF token matching otherwise.
    
    The interface is unchanged: .search(query, top_k) returns Chunk objects.
    """

    def __init__(
        self,
        path: Path | None = None,
        vector_enabled: bool = True,
        vector_min_score: float = 0.65,
    ) -> None:
        """Initialize the knowledge base.
        
        Args:
            path: Path to the JSON knowledge file. If None, uses default docs.
            vector_enabled: Whether to try enabling vector search.
            vector_min_score: Minimum similarity score for vector results (0-1).
        """
        self.path = path
        self.docs: list[dict[str, Any]] = []
        self._df: dict[str, int] = {}
        self.vector_min_score = vector_min_score
        
        # Load JSON knowledge
        if path and path.exists():
            self.docs = json.loads(path.read_text(encoding="utf-8"))
        if not self.docs:
            self.docs = self._default_docs()
        
        # Build TF-IDF index
        for doc in self.docs:
            for token in set(tokenize(doc.get("title", "") + " " + doc.get("content", ""))):
                self._df[token] = self._df.get(token, 0) + 1
        
        # Try to initialize vector search
        self._vector_store = None
        self._embedder_available = False
        if vector_enabled:
            self._init_vector_search()

    def _init_vector_search(self) -> None:
        """Initialize vector search if dependencies are available."""
        try:
            from .embedder import embed_documents, embed_query, get_embedder
            from .vector_store import VectorStore
            
            # Check if embedder is available
            if get_embedder() is None:
                logger.info("Vector search disabled: sentence-transformers not installed")
                return
            
            # Initialize vector store
            store = VectorStore()
            if not store.available:
                logger.info("Vector search disabled: chromadb not installed")
                return
            
            # Check if we need to index documents
            if store._collection and store._collection.count() == 0:
                logger.info(f"Indexing {len(self.docs)} documents for vector search...")
                self._index_documents(store, embed_documents)
            
            self._vector_store = store
            self._embedder_available = True
            self._embed_query = embed_query
            logger.info("Vector search enabled (BGE + Chroma)")
        
        except ImportError as e:
            logger.info(f"Vector search disabled: {e}")
        except Exception as e:
            logger.warning(f"Failed to initialize vector search: {e}", exc_info=True)

    def _index_documents(self, store: Any, embed_fn: Any) -> None:
        """Index all documents into the vector store."""
        texts = []
        metadatas = []
        ids = []
        
        for doc in self.docs:
            doc_id = str(doc.get("id", ""))
            title = str(doc.get("title", ""))
            content = str(doc.get("content", ""))
            tags = list(doc.get("tags", []))
            
            # Combine title and content for better context
            full_text = f"{title}\n{content}" if title else content
            texts.append(full_text)
            metadatas.append({
                "doc_id": doc_id,
                "title": title,
                "tags": ",".join(tags),
            })
            ids.append(doc_id)
        
        # Generate embeddings and add to store
        embeddings = embed_fn(texts)
        if embeddings:
            store.add_documents(texts, embeddings, metadatas, ids)

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
        """Search for relevant documents using hybrid retrieval.
        
        Tries vector search first if available, falls back to TF-IDF.
        
        Args:
            query: The search query.
            top_k: Maximum number of results to return.
        
        Returns:
            A list of Chunk objects, sorted by relevance score.
        """
        # Try vector search first
        if self._embedder_available and self._vector_store:
            try:
                vector_results = self._vector_search(query, top_k)
                if vector_results:
                    return vector_results
            except Exception as e:
                logger.warning(f"Vector search failed, falling back to TF-IDF: {e}")
        
        # Fallback to TF-IDF
        return self._tfidf_search(query, top_k)

    def _vector_search(self, query: str, top_k: int) -> list[Chunk]:
        """Semantic search using embeddings."""
        query_embedding = self._embed_query(query)
        if not query_embedding:
            return []
        
        results = self._vector_store.search(
            query_embedding,
            top_k=top_k,
            min_score=self.vector_min_score,
        )
        
        chunks = []
        for result in results:
            metadata = result["metadata"]
            chunks.append(Chunk(
                doc_id=metadata.get("doc_id", ""),
                title=metadata.get("title", ""),
                content=result["text"].split("\n", 1)[-1] if "\n" in result["text"] else result["text"],
                score=result["score"],
                tags=metadata.get("tags", "").split(",") if metadata.get("tags") else [],
            ))
        
        return chunks

    def _tfidf_search(self, query: str, top_k: int) -> list[Chunk]:
        """Token-based search using TF-IDF scoring."""
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
