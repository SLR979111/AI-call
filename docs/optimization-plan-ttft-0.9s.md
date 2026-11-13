# TTFT 优化方案：降低到 0.9s 以内

## 📊 现状分析

**当前性能基准**（来自 `reports/metric-check.md`）：
- 首字 P50: **1113.82 ms**
- 首字 P95: **1120.45 ms**
- 目标门槛: ≤ 2000 ms
- **新目标**: ≤ 900 ms

**当前瓶颈识别**：

从 `app/llm/router.py` 和 `app/agent/llm_agent.py` 分析，当前架构存在以下时延瓶颈：

1. **LLM 调用是阻塞式非流式**：`LLMRouter.complete()` 等待完整响应后才返回
2. **RAG 检索串行**：先检索知识库，再构建 prompt，再调用 LLM
3. **History 拼接开销**：每次调用都重新构建完整 messages 列表
4. **工具调用串行**：Function Calling 解析 → 工具执行 → 再次 LLM 调用

---

## 🎯 优化方案：三阶段降延

### 阶段 1：LLM 流式响应（预计降低 **400-600ms**）

**核心思路**：首 token 到达后立即开始 TTS，无需等待完整 LLM 响应。

#### 1.1 LLMRouter 增加流式支持

**新增方法**：
```python
async def complete_stream(
    self,
    messages: Sequence[dict[str, str]],
    *,
    # ... 其他参数同 complete()
) -> AsyncIterator[dict[str, Any]]:
    """流式返回 LLM 响应。
    
    Yields:
        {"type": "first_token", "latency_ms": 234.5}
        {"type": "token", "text": "您"}
        {"type": "token", "text": "好"}
        {"type": "sentence", "text": "您好，"}
        {"type": "done", "usage": {...}, "cost_cny": 0.002}
    """
    # SSE 流式请求
    payload["stream"] = True
    
    async with httpx.AsyncClient(...) as client:
        first_token_sent = False
        sentence_buffer = ""
        
        async with client.stream("POST", f"{provider.base_url}/chat/completions", ...) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                
                if line == "data: [DONE]":
                    break
                
                chunk = json.loads(line[6:])
                delta = chunk["choices"][0]["delta"]
                
                # 首 token 计时
                if not first_token_sent:
                    latency = (time.perf_counter() - start) * 1000.0
                    yield {"type": "first_token", "latency_ms": latency}
                    first_token_sent = True
                
                # Token 流式返回
                if "content" in delta:
                    text = delta["content"]
                    yield {"type": "token", "text": text}
                    sentence_buffer += text
                    
                    # 句子边界检测
                    if any(p in text for p in ["。", "！", "？", "，", "\n"]):
                        yield {"type": "sentence", "text": sentence_buffer.strip()}
                        sentence_buffer = ""
```

**关键点**：
- 使用 `httpx.stream()` 建立 SSE 连接
- 首 token 到达立即 yield，**不等待完整响应**
- 检测标点符号边界，按句返回（配合 TTS 流式合成）

#### 1.2 LLMAgent 适配流式输出

**修改 `app/agent/llm_agent.py`**：
```python
async def handle(self, req: dict[str, Any], cancel: CancelToken, tools: Any) -> AsyncIterator[dict[str, Any]]:
    # ... 前置逻辑不变 ...
    
    # 使用流式 API
    first_sentence_sent = False
    sentence_id = 0
    
    async for chunk in self.router.complete_stream(msg_list, ...):
        cancel.check()
        
        if chunk["type"] == "first_token":
            # 记录首 token 时延（事件总线）
            await self._emit_first_token(chunk["latency_ms"])
        
        elif chunk["type"] == "sentence":
            sentence_id += 1
            # 立即下发给 TTS
            yield {
                "kind": "sentence",
                "reply_id": reply_id,
                "sentence_id": sentence_id,
                "text": chunk["text"],
                "final": False
            }
            first_sentence_sent = True
        
        elif chunk["type"] == "done":
            # 最终结果和 usage
            if not first_sentence_sent:
                # fallback：整句返回
                yield {"kind": "sentence", ...}
            
            yield {"kind": "end", "reply_id": reply_id, "outcome": "answered", ...}
```

**预计收益**：
- **首字时延降低 400-600ms**（不再等待完整 LLM 响应）
- P50 从 1113ms → **513-713ms**

---

### 阶段 2：本地模型部署（预计降低 **200-400ms**）

**核心思路**：消除远程 API 网络往返时延。

#### 2.1 本地模型选型

| 模型 | 参数量 | TTFT（本地） | 质量评估 | 部署难度 |
|------|--------|--------------|----------|----------|
| **Qwen2.5-7B-Instruct** | 7B | 80-150ms | 高（接近 DeepSeek-V3） | 中等（需 24GB VRAM） |
| **DeepSeek-Coder-6.7B** | 6.7B | 60-120ms | 中高（代码优化） | 中等（需 16GB VRAM） |
| **Qwen2.5-3B-Instruct** | 3B | 40-80ms | 中等 | 低（需 8GB VRAM） |

**推荐**：**Qwen2.5-7B-Instruct**
- 中文理解能力强，适合客服场景
- 支持 Function Calling
- vLLM 加速后 TTFT < 150ms

#### 2.2 本地部署方案

**使用 vLLM 部署**：
```bash
# 安装 vLLM
pip install vllm

# 启动推理服务（占用 1 GPU）
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-7B-Instruct \
    --served-model-name qwen2.5-7b \
    --host 127.0.0.1 \
    --port 8090 \
    --tensor-parallel-size 1 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.85 \
    --enable-chunked-prefill \
    --disable-log-requests
```

**配置 LLMRouter**：
```bash
# .env
LLM_PRIMARY_NAME=local-qwen
LLM_PRIMARY_BASE_URL=http://127.0.0.1:8090/v1
LLM_PRIMARY_API_KEY=dummy  # vLLM 不验证
LLM_PRIMARY_MODEL=qwen2.5-7b
LLM_TIMEOUT_MS=5000
```

**预计收益**：
- 网络往返消除：**-100ms**
- API Gateway 开销消除：**-50ms**
- 模型推理加速：**-50-150ms**
- **总降低 200-300ms**
- P50 从 513-713ms → **313-513ms**

#### 2.3 混合部署策略

**方案**：本地模型 + 远程 API 备份
```python
# .env
LLM_PRIMARY_NAME=local-qwen
LLM_PRIMARY_BASE_URL=http://127.0.0.1:8090/v1
LLM_PRIMARY_MODEL=qwen2.5-7b

LLM_SECONDARY_NAME=deepseek-cloud
LLM_SECONDARY_BASE_URL=https://api.deepseek.com/v1
LLM_SECONDARY_MODEL=deepseek-chat
```

**优点**：
- 本地模型故障时自动切换远程 API
- 本地模型满载时自动降级
- 成本与性能平衡

---

### 阶段 3：并行化与预热（预计降低 **100-200ms**）

#### 3.1 RAG 检索与 LLM 预热并行

**当前串行逻辑**：
```
用户输入 → RAG 检索(50ms) → 构建 prompt(10ms) → LLM 调用(600ms)
总耗时: 660ms
```

**优化为并行**：
```python
async def handle(self, req, cancel, tools):
    text = req["user_text"]
    
    # 并行启动 RAG 和 LLM prefill
    rag_task = asyncio.create_task(self.knowledge.search(text, top_k=2))
    
    # 先用基础 prompt 预热（不等 RAG）
    base_messages = [
        {"role": "system", "content": self._system(...)},
        *history[-20:],
        {"role": "user", "content": text}  # 不带 RAG context
    ]
    
    # 立即发起 LLM 请求（prefill）
    llm_task = asyncio.create_task(
        self.router.complete_stream(base_messages, ...)
    )
    
    # 异步等待 RAG
    chunks = await rag_task
    
    # RAG 结果作为补充（可选）
    # 如果 LLM 已开始生成，将 RAG 结果插入到后续 token
    ...
```

**预计收益**：
- RAG 检索与 LLM prefill 重叠：**-50ms**
- P50 从 313-513ms → **263-463ms**

#### 3.2 Context Prefilling

**原理**：提前将 system prompt 和 history 加载到 KV Cache。

**vLLM 配置**：
```python
# 启动时预热常用 prefix
--enable-prefix-caching \
--max-num-seqs 8
```

**应用层优化**：
```python
# 每个 session 的 system prompt 和 history 保持稳定
# 使得 vLLM 可以缓存 prefix KV
```

**预计收益**：
- Prefix 缓存命中时 prefill 时间减半：**-50-100ms**
- P50 从 263-463ms → **213-363ms**

#### 3.3 模型量化加速

**方案**：使用 AWQ/GPTQ 4-bit 量化
```bash
# 使用 AWQ 量化模型
python -m vllm.entrypoints.openai.api_server \
    --model TheBloke/Qwen2.5-7B-Instruct-AWQ \
    --quantization awq \
    --dtype float16 \
    --max-model-len 8192
```

**预计收益**：
- 推理速度提升 30-50%：**-50-100ms**
- 显存占用减半（7B 模型仅需 4-6GB）

---

## 📈 综合收益预估

| 优化阶段 | 预计降低时延 | 累计 P50 | 技术难度 | 部署成本 |
|----------|-------------|----------|----------|----------|
| **阶段 0（基线）** | - | **1113 ms** | - | - |
| **阶段 1：流式响应** | -400~-600ms | **513-713 ms** | ⭐⭐ | 无（纯软件） |
| **阶段 2：本地模型** | -200~-300ms | **313-513 ms** | ⭐⭐⭐ | 中等（1 GPU） |
| **阶段 3：并行优化** | -100~-150ms | **213-363 ms** | ⭐⭐⭐⭐ | 低 |
| **终极目标** | **-900ms** | **< 900 ms ✅** | - | - |

**保守估计**：P50 可降至 **300-400ms**
**激进估计**：P50 可降至 **200-300ms**

---

## 🛠️ 实施建议

### 最小可行方案（MVP）

**仅实施阶段 1（流式响应）**：
- ✅ 无硬件成本
- ✅ 开发周期 2-3 天
- ✅ 风险低（可与现有系统并存）
- ✅ 收益明显（P50 降至 **500-700ms**）

**实施步骤**：
1. 修改 `app/llm/router.py`，新增 `complete_stream()` 方法
2. 修改 `app/agent/llm_agent.py`，适配流式输出
3. 添加 `.env` 配置项 `LLM_STREAM_ENABLED=true`
4. 兼容现有非流式模式（降级方案）
5. 测试并部署

### 完整方案

**实施阶段 1 + 阶段 2**：
- 硬件要求：1 张 NVIDIA GPU（24GB VRAM，如 RTX 4090 / A10）
- 开发周期：5-7 天
- **预计 P50: 300-500ms**

**实施阶段 1 + 2 + 3**：
- 开发周期：7-10 天
- **预计 P50: < 300ms**

---

## 🔧 技术细节补充

### 流式响应句子边界检测

**中文标点符号集合**：
```python
SENTENCE_TERMINATORS = {
    "。", "！", "？", "；",  # 完整句
    "，", "、",             # 分句（可选）
    "\n", "\n\n"            # 段落
}
```

**边界检测策略**：
```python
def detect_sentence_boundary(text: str) -> bool:
    # 1. 检测完整句标点
    if any(t in text for t in ["。", "！", "？"]):
        return True
    
    # 2. 检测逗号 + 足够长度（避免过短分句）
    if "，" in text and len(text) >= 15:
        return True
    
    # 3. 检测换行
    if "\n" in text:
        return True
    
    return False
```

### 本地模型硬件需求

| 模型 | 量化方式 | VRAM（推理） | VRAM（训练/微调） | 推荐显卡 |
|------|----------|-------------|------------------|----------|
| Qwen2.5-7B-Instruct | FP16 | 14GB | 28GB+ | RTX 4090, A10 |
| Qwen2.5-7B-Instruct | INT4 (AWQ) | 4-6GB | - | RTX 4070, L4 |
| Qwen2.5-3B-Instruct | FP16 | 6GB | 12GB+ | RTX 4060 Ti |

**云服务器成本**（按需计费）：
- 腾讯云 GPU GN7（T4 16GB）：¥2.5/小时
- 阿里云 GPU gn7i（T4 16GB）：¥2.8/小时
- 自建 RTX 4090：一次性成本 ¥12,000，电费 ¥0.8/小时

---

## 📝 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 流式响应中断导致句子不完整 | 中 | 超时重试 + 缓冲区保留 |
| 本地模型质量下降 | 高 | 先在测试环境对比 DeepSeek API |
| 本地模型 GPU OOM | 中 | 限制并发数 + 自动降级 |
| vLLM 服务不稳定 | 中 | 健康检查 + 自动重启 + 远程备份 |

---

## 🎯 验收标准

1. **性能指标**：
   - ✅ P50 首字时延 ≤ 900ms
   - ✅ P95 首字时延 ≤ 1500ms
   - ✅ 任务成功率保持 ≥ 80%

2. **质量指标**：
   - ✅ 流式响应句子完整率 ≥ 95%
   - ✅ 本地模型对话质量与远程 API 相当（A/B 测试）

3. **稳定性指标**：
   - ✅ 连续运行 24 小时无崩溃
   - ✅ GPU 服务故障时自动切换远程 API

---

## 📚 参考资源

- [vLLM 官方文档](https://docs.vllm.ai/)
- [Qwen2.5 模型卡](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)
- [OpenAI Stream API 规范](https://platform.openai.com/docs/api-reference/streaming)
- [FastAPI SSE 支持](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse)

---

**编写时间**: 2026-11-08
**作者**: Kiro (AI Assistant)
**版本**: v1.0
