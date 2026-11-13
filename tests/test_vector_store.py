#!/usr/bin/env python3
"""Tests for the vector store functionality."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


class TestVectorStore:
    """Test the VectorStore class."""
    
    def test_vector_store_initialization(self):
        """Test that vector store initializes correctly."""
        from app.rag.vector_store import VectorStore
        
        store = VectorStore(persist_dir="data/test_chroma_db", collection_name="test")
        assert store is not None
        assert store.available
        assert store.collection_name == "test"
    
    def test_add_and_search(self):
        """Test adding documents and searching."""
        from app.rag.embedder import embed_documents, embed_query
        from app.rag.vector_store import VectorStore
        
        # Initialize store
        store = VectorStore(persist_dir="data/test_chroma_db", collection_name="test")
        if not store.available:
            pytest.skip("Vector store not available")
        
        # Clear any existing data
        store.clear()
        
        # Test documents
        texts = [
            "配送时间一般是24到48小时",
            "退款会在1到3个工作日内到账",
            "我们的AI客服可以随时为您服务",
        ]
        
        # Generate embeddings
        embeddings = embed_documents(texts)
        assert embeddings is not None
        assert len(embeddings) == len(texts)
        
        # Add documents
        metadatas = [
            {"doc_id": "doc1", "title": "配送", "tags": "配送,订单"},
            {"doc_id": "doc2", "title": "退款", "tags": "退款,售后"},
            {"doc_id": "doc3", "title": "AI服务", "tags": "AI,客服"},
        ]
        ids = ["doc1", "doc2", "doc3"]
        
        store.add_documents(texts, embeddings, metadatas, ids)
        
        # Test search
        query = "什么时候能收到货"
        query_embedding = embed_query(query)
        assert query_embedding is not None
        
        results = store.search(query_embedding, top_k=2, min_score=0.0)
        assert len(results) > 0
        assert results[0]["score"] > 0
        assert "配送" in results[0]["text"] or "配送" in results[0]["metadata"].get("title", "")
        
        print(f"\n查询: {query}")
        for i, result in enumerate(results, 1):
            print(f"  结果 {i}: {result['metadata']['title']} (分数: {result['score']:.3f})")
            print(f"    内容: {result['text'][:50]}...")


class TestEmbedder:
    """Test the embedding functionality."""
    
    def test_get_embedder(self):
        """Test that embedder loads correctly."""
        from app.rag.embedder import get_embedder
        
        model = get_embedder()
        if model is None:
            pytest.skip("sentence-transformers not installed")
        
        assert model is not None
    
    def test_embed_query(self):
        """Test query embedding."""
        from app.rag.embedder import embed_query
        
        embedding = embed_query("测试查询")
        if embedding is None:
            pytest.skip("sentence-transformers not installed")
        
        assert embedding is not None
        assert len(embedding) == 512  # BGE-small-zh-v1.5 produces 512-dim vectors
        assert all(isinstance(x, float) for x in embedding)
    
    def test_embed_documents(self):
        """Test document embedding."""
        from app.rag.embedder import embed_documents
        
        texts = ["文档一", "文档二", "文档三"]
        embeddings = embed_documents(texts)
        
        if embeddings is None:
            pytest.skip("sentence-transformers not installed")
        
        assert embeddings is not None
        assert len(embeddings) == len(texts)
        assert all(len(emb) == 512 for emb in embeddings)


class TestKnowledgeBase:
    """Test the KnowledgeBase with vector search."""
    
    def test_knowledge_base_vector_search(self):
        """Test that KnowledgeBase uses vector search when available."""
        from app.rag.knowledge import KnowledgeBase
        
        # Create knowledge base with vector search enabled
        kb = KnowledgeBase(vector_enabled=True, vector_min_score=0.0)
        
        # Test search
        results = kb.search("配送需要多久", top_k=2)
        assert len(results) > 0
        
        print(f"\n知识库搜索测试:")
        for i, chunk in enumerate(results, 1):
            print(f"  {i}. {chunk.title} (分数: {chunk.score:.3f})")
            print(f"     {chunk.content[:50]}...")
    
    def test_knowledge_base_fallback(self):
        """Test that KnowledgeBase falls back to TF-IDF when vector search is disabled."""
        from app.rag.knowledge import KnowledgeBase
        
        # Create knowledge base with vector search disabled
        kb = KnowledgeBase(vector_enabled=False)
        
        # Test search still works via TF-IDF
        results = kb.search("配送需要多久", top_k=2)
        assert len(results) > 0
        
        print(f"\n知识库 TF-IDF 回退测试:")
        for i, chunk in enumerate(results, 1):
            print(f"  {i}. {chunk.title} (分数: {chunk.score:.3f})")


if __name__ == "__main__":
    # Run tests with verbose output
    pytest.main([__file__, "-v", "-s"])
