"""Lightweight embedding wrapper for semantic search.

BGE (BAAI General Embedding) is optimized for Chinese and supports both CPU and GPU.
The model is loaded lazily only when the vector store is initialized.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_model: SentenceTransformer | None = None


def get_embedder(model_name: str = "BAAI/bge-small-zh-v1.5") -> SentenceTransformer | None:
    """Get or create the embedding model instance.
    
    Args:
        model_name: HuggingFace model ID. Default is bge-small-zh-v1.5:
            - 512 dimensions
            - ~24MB model size
            - CPU-friendly (inference ~50ms/query on modern CPUs)
            - Optimized for Chinese semantic search
    
    Returns:
        The model instance, or None if sentence-transformers is not installed.
    """
    global _model
    if _model is not None:
        return _model
    
    try:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading embedding model: {model_name}")
        _model = SentenceTransformer(model_name)
        logger.info("Embedding model loaded successfully")
        return _model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed; vector search disabled. "
            "Install with: pip install sentence-transformers"
        )
        return None


def embed_query(text: str) -> list[float] | None:
    """Generate embedding vector for a search query.
    
    Uses an instruction prefix to optimize retrieval performance.
    
    Args:
        text: The query text.
    
    Returns:
        A 512-dimensional embedding vector, or None if the model is not available.
    """
    model = get_embedder()
    if model is None:
        return None
    
    # BGE-specific instruction prefix for queries
    prefixed = f"为这个句子生成表示以用于检索相关文章：{text}"
    return model.encode(prefixed, show_progress_bar=False).tolist()


def embed_documents(texts: list[str]) -> list[list[float]] | None:
    """Generate embedding vectors for a batch of documents.
    
    Args:
        texts: A list of document texts.
    
    Returns:
        A list of 512-dimensional embedding vectors, or None if the model is not available.
    """
    model = get_embedder()
    if model is None:
        return None
    
    return model.encode(texts, show_progress_bar=False).tolist()
