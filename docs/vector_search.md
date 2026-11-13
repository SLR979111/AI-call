# 向量搜索功能文档

## 概述

本项目集成了基于语义向量的知识检索功能，使用以下技术栈：

- **Embedding 模型**: BAAI/bge-small-zh-v1.5 (中文优化的 BGE 模型)
- **向量数据库**: ChromaDB (轻量级、可嵌入的向量数据库)
- **检索策略**: 混合检索 (向量搜索 + TF-IDF 回退)

## 特性

✅ **语义搜索**: 理解查询意图，而不仅是关键词匹配  
✅ **自动回退**: 当向量搜索不可用时，自动降级到 TF-IDF  
✅ **CPU 友好**: 使用 CPU 版本 PyTorch，无需 GPU  
✅ **持久化存储**: 向量索引持久化到磁盘，无需每次重建  

## 安装

### 1. 安装依赖

```bash
# 先安装 CPU 版本的 PyTorch（节省空间）
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 再安装向量搜索依赖
pip install sentence-transformers chromadb
```

### 2. 设置缓存目录

为避免权限问题，建议设置 HuggingFace 缓存目录：

```bash
export HF_HOME=/path/to/project/data/huggingface_cache
```

或在 Python 代码中设置：

```python
import os
os.environ['HF_HOME'] = '/path/to/project/data/huggingface_cache'
```

## 使用方法

### 基础使用

```python
from app.rag.knowledge import KnowledgeBase

# 初始化知识库（自动启用向量搜索）
kb = KnowledgeBase(
    vector_enabled=True,        # 启用向量搜索
    vector_min_score=0.65       # 最小相似度阈值
)

# 搜索相关知识
results = kb.search("配送需要多久", top_k=3)

for chunk in results:
    print(f"{chunk.title} (分数: {chunk.score:.3f})")
    print(f"  {chunk.content}")
```

### 构建向量索引

对于生产环境，建议预先构建向量索引：

```bash
# 运行构建脚本
python scripts/build_knowledge_vector.py
```

该脚本会：
1. 加载 `knowledge/` 目录下的所有 JSON 文件
2. 为每个文档生成向量嵌入
3. 将向量索引持久化到 `data/chroma_db/`

### 仅使用 Embedding

如果只需要生成嵌入向量：

```python
from app.rag.embedder import embed_query, embed_documents

# 查询嵌入（带优化前缀）
query_embedding = embed_query("配送需要多久")
print(f"维度: {len(query_embedding)}")  # 512

# 文档嵌入（批量）
doc_embeddings = embed_documents([
    "配送时间是24-48小时",
    "退款需要1-3天"
])
```

### 仅使用 VectorStore

直接操作向量数据库：

```python
from app.rag.vector_store import VectorStore
from app.rag.embedder import embed_documents, embed_query

# 初始化存储
store = VectorStore(
    persist_dir="data/chroma_db",
    collection_name="knowledge"
)

# 添加文档
texts = ["文档1", "文档2"]
embeddings = embed_documents(texts)
metadatas = [{"title": "标题1"}, {"title": "标题2"}]
ids = ["doc1", "doc2"]

store.add_documents(texts, embeddings, metadatas, ids)

# 搜索
query_emb = embed_query("搜索关键词")
results = store.search(query_emb, top_k=3, min_score=0.65)

for result in results:
    print(result['text'])
    print(result['metadata'])
    print(result['score'])
```

## 测试

### 运行单元测试

```bash
# 设置缓存目录
export HF_HOME=$(pwd)/data/huggingface_cache

# 运行测试
python -m pytest tests/test_vector_store.py -v
```

### 快速验证

```bash
# 设置缓存目录
export HF_HOME=$(pwd)/data/huggingface_cache

# 快速测试
python -c "
from app.rag.knowledge import KnowledgeBase
kb = KnowledgeBase(vector_enabled=True)
results = kb.search('配送需要多久', top_k=2)
for r in results:
    print(f'{r.title}: {r.score:.3f}')
"
```

## 性能指标

### 模型信息

- **模型大小**: ~24MB
- **嵌入维度**: 512
- **推理速度**: ~50ms/query (CPU)
- **语言优化**: 中文

### 资源占用

- **磁盘空间**:
  - 模型缓存: ~24MB
  - 向量索引: 取决于文档数量（约 2KB/文档）
- **内存**: 
  - 模型加载: ~100MB
  - 向量存储: 取决于索引大小

## 故障排除

### 问题 1: 缓存目录权限错误

**症状**: `OSError: [Errno 30] Read-only file system: '/home/user/.cache/huggingface'`

**解决方案**:
```bash
export HF_HOME=/path/to/writable/directory
```

### 问题 2: 磁盘空间不足

**症状**: `OSError: [Errno 28] No space left on device`

**解决方案**:
```bash
# 安装 CPU 版本 PyTorch（节省 ~2GB）
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 然后再安装其他依赖
pip install sentence-transformers chromadb
```

### 问题 3: ChromaDB 实例冲突

**症状**: `ValueError: An instance of Chroma already exists`

**解决方案**: 这通常发生在同一进程中创建多个 VectorStore 实例时。确保：
- 每个 collection 只创建一次实例
- 或者在不同进程中运行

### 问题 4: 向量搜索被禁用

**症状**: 日志显示 "Vector search disabled"

**检查清单**:
```bash
# 1. 确认依赖已安装
pip list | grep sentence-transformers
pip list | grep chromadb

# 2. 确认模型可以加载
python -c "from sentence_transformers import SentenceTransformer; print('OK')"

# 3. 确认 ChromaDB 可用
python -c "import chromadb; print('OK')"
```

## 架构说明

```
app/rag/
├── embedder.py         # 嵌入模型封装
├── vector_store.py     # ChromaDB 封装
└── knowledge.py        # 知识库（混合检索）

数据流:
1. 用户查询 → embed_query() → 查询向量
2. 查询向量 → vector_store.search() → 相似文档
3. 回退: 向量搜索失败 → TF-IDF 搜索
```

### 混合检索策略

1. **优先尝试向量搜索**:
   - 语义理解更强
   - 支持同义词和相关概念
   - 分数阈值可调 (`vector_min_score`)

2. **自动回退到 TF-IDF**:
   - 当向量搜索不可用时
   - 或向量搜索无结果时
   - 基于关键词匹配，更稳定

## 最佳实践

1. **生产环境**:
   - 预先构建向量索引（使用 `build_knowledge_vector.py`）
   - 设置合适的 `vector_min_score` 阈值（推荐 0.65-0.75）
   - 监控搜索质量和性能

2. **开发环境**:
   - 使用小规模测试数据
   - 可以依赖自动回退机制
   - 根据需要调整 `top_k` 参数

3. **缓存管理**:
   - 模型缓存可以共享（相同模型只下载一次）
   - 定期清理无用的向量索引
   - 考虑将缓存目录加入 `.gitignore`

## 配置建议

### requirements.txt

建议在 `requirements.txt` 中添加可选依赖注释：

```txt
# Optional: Vector search for semantic knowledge retrieval
# Install with: pip install sentence-transformers chromadb
# sentence-transformers>=2.2.0
# chromadb>=0.4.22
```

### 环境变量

在 `.env` 或系统环境中设置：

```bash
# HuggingFace 缓存目录
HF_HOME=/path/to/project/data/huggingface_cache

# HuggingFace Token (可选，用于更快的模型下载)
# HF_TOKEN=your_token_here
```

## 相关文件

- [app/rag/embedder.py](../app/rag/embedder.py) - Embedding 模型
- [app/rag/vector_store.py](../app/rag/vector_store.py) - 向量存储
- [app/rag/knowledge.py](../app/rag/knowledge.py) - 知识库接口
- [scripts/build_knowledge_vector.py](../scripts/build_knowledge_vector.py) - 构建脚本
- [tests/test_vector_store.py](../tests/test_vector_store.py) - 单元测试
