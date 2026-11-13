#!/usr/bin/env python3
"""Build vector knowledge base from JSON files.

This script indexes all JSON knowledge files into the Chroma vector store
for semantic search. Run it after installing sentence-transformers and chromadb:

    pip install sentence-transformers chromadb
    python scripts/build_knowledge_vector.py

The vector store is persisted to data/chroma_db/ and will be automatically
loaded by KnowledgeBase when available.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def build_vector_store(knowledge_files: list[Path], output_dir: Path = Path("data/chroma_db")):
    """Build vector store from JSON knowledge files."""
    try:
        from app.rag.embedder import embed_documents, get_embedder
        from app.rag.vector_store import VectorStore
    except ImportError as e:
        logger.error(f"Failed to import vector search dependencies: {e}")
        logger.error("Install with: pip install sentence-transformers chromadb")
        return False
    
    # Check if embedder is available
    if get_embedder() is None:
        logger.error("Embedding model not available")
        return False
    
    # Initialize vector store
    store = VectorStore(persist_dir=output_dir)
    if not store.available:
        logger.error("Vector store not available")
        return False
    
    # Clear existing data
    logger.info("Clearing existing vector store...")
    store.clear()
    
    # Load all knowledge files
    all_docs = []
    for path in knowledge_files:
        if not path.exists():
            logger.warning(f"Knowledge file not found: {path}")
            continue
        
        logger.info(f"Loading {path}...")
        docs = json.loads(path.read_text(encoding="utf-8"))
        all_docs.extend(docs)
        logger.info(f"  Loaded {len(docs)} documents")
    
    if not all_docs:
        logger.error("No documents found")
        return False
    
    # Prepare documents for indexing
    texts = []
    metadatas = []
    ids = []
    
    for i, doc in enumerate(all_docs):
        doc_id = str(doc.get("id", f"doc_{i}"))
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
    
    # Generate embeddings and index
    logger.info(f"Generating embeddings for {len(texts)} documents...")
    embeddings = embed_documents(texts)
    
    if not embeddings:
        logger.error("Failed to generate embeddings")
        return False
    
    logger.info("Indexing documents into vector store...")
    store.add_documents(texts, embeddings, metadatas, ids)
    
    logger.info(f"✅ Successfully indexed {len(texts)} documents to {output_dir}")
    return True


def main():
    """Main entry point."""
    knowledge_dir = Path("knowledge")
    knowledge_files = [
        knowledge_dir / "faq.json",
        knowledge_dir / "faq_extended.json",
        knowledge_dir / "pop_audit.json",
    ]
    
    success = build_vector_store(knowledge_files)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
