"""Persistent vector store using Chroma for semantic search.

Chroma is a lightweight, embeddable vector database. It requires no separate
deployment and can run in-memory or persist to disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import chromadb
    from chromadb.api import ClientAPI
    from chromadb.api.models.Collection import Collection

logger = logging.getLogger(__name__)


class VectorStore:
    """Wrapper around Chroma vector database for FAQ retrieval."""
    
    def __init__(self, persist_dir: Path | str = "data/chroma_db", collection_name: str = "knowledge"):
        """Initialize the vector store.
        
        Args:
            persist_dir: Directory where Chroma will persist data.
            collection_name: Name of the collection to use.
        """
        self.persist_dir = Path(persist_dir)
        self.collection_name = collection_name
        self._client: ClientAPI | None = None
        self._collection: Collection | None = None
        self._available = self._init_chroma()
    
    def _init_chroma(self) -> bool:
        """Initialize Chroma client and collection.
        
        Returns:
            True if initialization succeeded, False if chromadb is not installed.
        """
        try:
            import chromadb
            
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            
            # Use PersistentClient to avoid singleton conflicts in tests
            self._client = chromadb.PersistentClient(
                path=str(self.persist_dir),
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},  # Use cosine similarity
            )
            
            count = self._collection.count()
            logger.info(f"Chroma collection '{self.collection_name}' initialized with {count} documents")
            return True
        
        except ImportError:
            logger.warning(
                "chromadb not installed; vector store disabled. "
                "Install with: pip install chromadb"
            )
            return False
        except Exception as e:
            logger.warning(f"Failed to initialize ChromaDB: {e}")
            return False
    
    @property
    def available(self) -> bool:
        """Check if the vector store is available."""
        return self._available
    
    def add_documents(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        """Add documents to the vector store.
        
        Args:
            texts: Document texts.
            embeddings: Pre-computed embedding vectors.
            metadatas: Metadata for each document.
            ids: Unique IDs for each document.
        """
        if not self._available or self._collection is None:
            logger.warning("Vector store not available; skipping add_documents")
            return
        
        self._collection.add(
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
            ids=ids,
        )
        logger.info(f"Added {len(texts)} documents to vector store")
    
    def search(
        self,
        query_embedding: list[float],
        top_k: int = 3,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Search for similar documents using a query embedding.
        
        Args:
            query_embedding: The query's embedding vector.
            top_k: Number of results to return.
            min_score: Minimum similarity score (0 to 1).
        
        Returns:
            A list of result dictionaries with keys:
            - text: The document text
            - metadata: The document metadata
            - score: Similarity score (1 - cosine distance)
        """
        if not self._available or self._collection is None:
            return []
        
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )
        
        if not results["documents"] or not results["documents"][0]:
            return []
        
        output = []
        for doc, meta, distance in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # Convert cosine distance to similarity score
            score = 1.0 - distance
            if score >= min_score:
                output.append({
                    "text": doc,
                    "metadata": meta,
                    "score": score,
                })
        
        return output
    
    def clear(self) -> None:
        """Clear all documents from the collection."""
        if not self._available or self._collection is None:
            return
        
        # Chroma doesn't have a clear() method, so we delete and recreate
        if self._client is not None:
            self._client.delete_collection(name=self.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"Cleared collection '{self.collection_name}'")
