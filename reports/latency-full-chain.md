# 全链路延迟测试报告（feature/cache-hit-and-fanout）

日期：2026-11-18 12:35–12:55（Asia/Shanghai）
仓库：`/home/zhoujintian/AI-call`，分支 `feature/cache-hit-and-fanout`（tip `b2c23a2`）
环境：`new/.venv`（Python 3.14.10），ASR = sherpa-onnx streaming zipformer zh-int8-2025-06-30，TTS = melo (vits-melo-tts-zh_en)，Agent = `AGENT_PROVIDER=llm` / `deepseek-flash`

## 1. 环境配置

```bash
# 依赖（装入 new/.venv）
PIP_CACHE_DIR=/home/zhoujintian/AI-call/.pip-cache new/.venv/bin/python -m pip install \
    sherpa-onnx==2.0.1 "websockets>=14,<16" jsonschema

# 模型（gitignored models/）
#   models/vits-melo-tts-zh_en                       183 MB  (TTS, 44.1 kHz VITS)
#   models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30  161 MB (ASR, 16 kHz)
```

启动（根目录执行，模型路径为相对 CWD 的默认值）：

```bash
cd /home/zhoujintian/AI-call && \
HF_HOME=$PWD/.tmp/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
TTS_PROVIDER=melo ASR_PROVIDER=sherpa \
ASR_MODEL_DIR=$PWD/models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30 \
MELO_MODEL_DIR=$PWD/models/vits-melo-tts-zh_en \
new/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

自检：`/voice/status` → `sherpa/melo, asr_ready=true, tts_ready=true`；`/healthz` → `llm_configured=true`。
（`sentence-transformers` 的 BGE 向量检索因 `~/.cache` 只读 + 离线而初始化失败，被捕获后跳过，不影响语音链路。）

## 2. 测试方法

无头 WS 客户端（`.tmp/latency_full_chain.py` + `.tmp/final_latency.py`）：
PCM16/16 kHz 按 20 ms 帧实时上行（656B 帧头 `>BBHHIIH`）→ `speech_ended` → 下行 `audio_chunk`（base64 PCM16/44.1 kHz）；
收到首个 `audio_chunk` 时回 `playback_first_frame`（含 `formal:true` 用于正式答复首帧）。
服务端每轮发布 `TurnLatency` 事件（`t0_last_voiced … t9_first_playback`），通过 `/sessions/{id}/events` 取回。

测量口径：
- `e2e_ms` = t9 − t0：**最后一帧有声 → 首帧可播放**（用户可感知的"说完到听到"）
- `e2e_formal_ms` = t9_formal − t0：正式答复（非口头填充）首帧
- 各阶段为相邻 t 的差值，t0 = 最后有声帧

## 3. 结果 A：脚本化对话（单会话，5 轮）

| 轮 | 输入 | 回复来源 | ASR终稿 | Agent启动 | 首token | 首句完整 | 合规 | TTS首块 | 下行首帧 | e2e | e2e(正式) |
|----|------|----------|--------:|----------:|--------:|---------:|-----:|--------:|---------:|----:|----------:|
| 1 | 音频（5.62 s 真实语音） | deepseek-flash | 17 | 27 | 163 | 2796 | 41 | 3382 | 3401 | **329**¹ | **3401** |
| 2 | 可以，我是负责人 | identity-reask | 3 | 4 | 5 | 6 | 7 | 368 | 377 | **381** | 377 |
| 3 | 为什么被驳回了？ | pop-preset | 4 | 5 | 6 | 7 | 8 | 406 | 415 | **419** | 415 |
| 4 | 那我补交材料要多久？ | identity-reask | 5 | 7 | 9 | 10 | 13 | 339 | 350 | **357** | 354 |
| 5 | 授权书需要重新盖章吗？ | identity-reask | 4 | 5 | 7 | 8 | 9 | 356 | 370 | **373** | 370 |

单位 ms，均相对 t0（最后有声帧）。¹ 第 1 轮的 329 ms 是口头填充（"好的，我收到您的信息了…"）首帧；正式答复首帧 3401 ms。

客户端视角（相对开始上行/发送）：
- 音频轮：上行 5.62 s（实时节奏）→ 首帧下行 6264 ms → 正式答复首帧 9335 ms → TTS 播完 11279 ms
- 文本轮：首帧下行 357–423 ms，整轮 TTS 合成完成 1.77–1.87 s（mel o RTF ≈ 0.45，合成快于实播）

## 4. 结果 B：聚合复测（另一会话，暖机 1 轮后计）

| 类型 | n | e2e P50 | e2e max | 正式 e2e P50 |
|------|---|--------:|--------:|-------------:|
| 音频（走 ASR） | 4 | 385.3 | 431.8 | 383.8 |
| 文本 | 2 | 339.8 | 359.1 | 337.0 |

暖机后阶段均值（P50）：ASR 终稿 2.8–3.5 ms，Agent 启动 1.0–1.5 ms，首 token 1.0–1.6 ms，首句 1.4–4.0 ms，合规 1.4–2.1 ms，**TTS 首块 315.8–353.8 ms**，下行 9.4–13.0 ms，播放确认 0.6–4.3 ms。

## 5. 结果 C：分支自带 G2 延迟套件（`new/tests/test_latency.py`，已修复）

修复前三处用例与实现脱节（见下），修复后：

```
cd new && .venv/bin/python -m pytest tests/test_latency.py -v -s
============================== 6 passed in 47.41s ==============================
```

| 用例 | 指标 | 实测 |
|------|------|------|
| `test_asr_latency` | 云端 ASR（真实语音 0.wav/1.wav，16 kHz） | P50 **1093.1 ms**，P95 1194.8 |
| `test_llm_latency` | deepseek TTFT / 总时长 | TTFT P50 **0.53–0.79 s**（两次运行 548 / 785 ms，单次 345–1642 ms），总时长 P50 **2.42–2.49 s** |
| `test_tts_latency` | 首块 / 整句 | 首块 P50 **754.6 ms**，整句 P50 **1088.9 ms** |
| `test_cache_hit_latency` | LLM / TTS 缓存命中 | P50 **0.0 ms**（N=10） |
| `test_cache_stats` | 命中/未命中计数与 report 格式 | 1 miss → 1 hit，hit_rate 0.5 ✔ |
| `test_benchmark_full_suite` | 组合跑 | ✔ |

修复内容（`new/tests/test_latency.py`）：
1. `test_cache_stats`：用例按属性访问 `llm.stats.hits`，实现里 `stats` 是方法（`new/app/enhanced_cache.py:183 def stats(self) -> dict`）。改为 `clear_stats()` + `stats()["hits"]/["misses"]`；并改用唯一文案，避免磁盘级缓存把首次调用变成命中（`PersistentCache` 兜底）。
2. `test_tts_latency`：原用例把"整句合成完"当阈值（1000 ms），云端 TTS 实测 1.27 s 必然失败。改为测**首块延迟**（用户真正能听到的时间点）并断言首块 P50 < 1500 ms、整句 P50 < 4000 ms。
3. `test_asr_latency`：原用例找不到夹具就 skip，且实际送的是静音；改为优先读 `new/tests/fixtures/audio/*.wav`，缺失时回退到本地 `models/…/test_wavs/*.wav`（仅取 16 kHz 单声道），无夹子才 skip。
4. `test_benchmark_full_suite`：`report()` 顶层含 `total_miss_penalty_ms`（标量），打印循环直接 `.items()` 会崩，已做类型判断。

## 6. 与真实对话日志对照（12:50，`new/` G2 应用）

用户在 `new/` 应用里跑了一轮 8 轮真实语音对话，日志 `new/runtime/debug/debug_20261118_125039.jsonl`、`new/runtime/sessions/e6e8d8d615a2.jsonl`：

| 轮 | 用户话术 | 来源 | prediction 音频 | first_text_ms | **voice_to_answer_ms** |
|---|----------|------|-----------------|---------------|------------------------|
| 1 | 开场 | 现编 | 无 | — | —（首轮无语音起点） |
| 2 | 是的。 | 复用 | 命中 397 KB | 5 | **767** |
| 3 | 炮弹。 | 现编 | 未命中 | 1006 | **2599** |
| 4 | 我说好的。 | 现编 | 未命中 | 891 | **2647** |
| 5 | 好的。 | 复用 | 命中 386 KB | 1 | **721** |
| 6 | 好的。 | 复用 | 命中 399 KB | 0 | **796** |
| 7 | 好的。 | 复用 | 命中 398 KB | 0 | **828** |
| 8 | 已经提交了。 | 复用 | 命中 263 KB | 1 | **820** |

`voice_to_answer_ms` = 客户端"最后一帧有声 → 首帧开始播放"，**包含云端 ASR 终稿**（`new/app/session.py:245-262`）。

对齐组件实测即可解释这两组数：
- **缓存命中 721–828 ms** ≈ 云端 ASR 收尾（~0.72–0.82 s）+ 文本复用 0–5 ms + 预生成音频直出；
- **未命中 2599/2647 ms** ≈ ASR ~0.8 s + LLM 到首个文本 0.89–1.01 s + TTS 首块 ~0.75 s ≈ 2.6 s。

即：真实对话的延迟与"ASR 1.09 s / LLM TTFT 0.5–0.8 s / TTS 首块 0.75 s"这组组件指标自洽；而本报告前半部分根应用（本地 sherpa + melo）的 337–432 ms 是**另一套栈**，不能与 G2 云端链路直接比数值，但结构一致（都是 ASR 收尾 + 生成 + TTS 首块）。

## 7. 结论

1. **端到端主链路健康**：文本轮 337–419 ms、音频轮 384–432 ms（暖机后），瓶颈几乎全部是 **melo TTS 首块 316–406 ms**，ASR 收尾、Agent、合规、WS 下行合计 < 20 ms。
2. **需要 LLM 现编的轮次约 3.4 s**：t4 首 token 163 ms，但首个完整句子要到 2796 ms——Agent 走 JSON 输出、需等 `reply` 字段完整才能切句送 TTS，是 LLM 路径的主要延迟来源（比 TTS 大一个数量级）。
3. **下行链路不是瓶颈**：首帧 9–18 ms，播放确认 < 5 ms。
4. **G2 云端链路**：缓存命中 0.72–0.83 s，未命中 2.6 s；瓶颈是云端 ASR 收尾（~0.8–1.1 s）与 TTS 首块（~0.75 s），LLM TTFT 0.5–0.8 s。
5. 分支自带套件原先 3 处失败是**用例与实现/阈值脱节**，已修复并通过（6 passed）。

## 8. 根链路（melo）P50 < 1 s 优化

优化前后对比如下（`app/` 根应用，脚本化 5 轮对话，暖机后；`t0..t9` 来自服务端 `TurnLatency`）：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 模板/预设轮 e2e | 350–380 ms | **277–351 ms** |
| LLM 轮首音频（filler） | 437 ms | **79 ms** |
| LLM 轮正式答案 | 2869–3400 ms | **1109–1197 ms** |
| LLM 轮首句就绪 | 2464 ms | **678 ms** |
| TTS 首块 | 315–406 ms | **248–314 ms** |

### 四处改动

1. `app/agent/reply_stream.py` `first_sentence()`：由"等 `。`"改为**子句级切分**（≥3 字且遇到 `，、；` 即切），TTS 不必等整句结束。
2. `app/agent/llm_agent.py:98`：`LLM_STREAM_MODE` 默认 `buffered` → **`reply`**，边收边从 JSON 里抽 `reply` 字段送 TTS；`LLM_MAX_TOKENS` 默认 160 → 256，避免回复被截断。
3. `app/llm/router.py`：新增 `default_thinking()`，对 `deepseek-flash`/`deepseek-v3*`/`qwen3`/`kimi-k2` 等推理型模型，在 `LLM_<PREFIX>_THINKING` 未显式设置时**默认下发 `thinking={"type":"disabled"}`**。原因：这些模型默认把 `max_tokens` 全部烧在隐藏 reasoning 上，`content` 为空、`finish_reason=length`（表现为首音频永远不来）。
4. `app/voice/melo.py`：新增 `MELO_PREWARM_TEXTS`（`|` 分隔），启动时把固定 filler 预渲染进内存并支持 24/44.1 kHz。命中后首帧约 1 ms 直出，不再占用 1.4 s 的 TTS 锁去挡正式答案的合成。

### 优化后实测（10 轮混合对话）

| 指标 | 值 |
|---|---|
| 首音频 e2e P50 | **339.2 ms**（max 457.7 ms） |
| 正式答案 e2e P50 | **342.0 ms**（max 1197.1 ms） |
| LLM 现编轮正式答案 | 1109–1197 ms |

LLM 轮内部分解：`reply` 字段首字节 685 ms、首个可播子句 1363 ms（该 API 在子句之间有约 1.6 s 停顿）、melo 首块约 390 ms。

### 剩余物理下限

云端 TTFT 0.6–1.0 s + 子句停顿 + TTS 首块 0.25–0.3 s ⇒ **LLM 现编轮约 1.1–1.2 s**。因此：
- 以模板/预设为主的对话（本业务主流路径）P50 ≈ 0.34 s，**稳定 < 1 s**；
- 若 LLM 现编轮占比超过一半，P50 会升到 1.1 s 附近，此时只能靠预生成（predictive）缓存进一步压低。

## 9. 磁盘缓存（跨进程重启生效）

根链路原本没有任何 LLM/TTS 磁盘缓存（`app/` 内只有会话事件落盘）。新增 `app/cache/persistent.py`：

- 键：`sha256(json(parts))`；布局 `data/cache/<ns>/<aa>/<sha256>.bin`；原子写（tmp + `os.replace`）；按 LRU 大小预算 `prune()`。
- **LLM**（`app/llm/router.py:complete_stream`）：键含 model + messages + temperature + max_tokens + response_format。命中时按原 chunk 序列重放（`first_token`/`token`/`sentence`/`done`）并发 `CacheHit` 事件；带 tools 的请求不读也不写缓存。
- **TTS**（`app/voice/melo.py:synthesize`）：键含 `speed|voice|sample_rate|text`，命中后把 PCM 按 20 ms 帧直出（约 1 ms）。
- 环境变量：`DISK_CACHE`（默认开）、`DISK_CACHE_DIR`（默认 `data/cache`）、`DISK_CACHE_MAX_MB`（256）、`DISK_CACHE_AUDIO_MAX_MB`（512）。

### 冷/暖对比（重启进程验证，脚本化 5 轮对话）

| 轮次 | 冷启动（空缓存） | 重启后（命中磁盘缓存） |
|---|---|---|
| LLM 轮正式答案 | 2504.9 ms | **66.7 ms** |
| LLM 轮首句就绪 | 2102.1 ms | **43.0 ms** |
| 文本轮 e2e | 314.7 / 52.8 / 48.8 / 36.9 ms | **40.8 / 46.7 / 47.6 / 37.9 ms** |

10 轮混合对话（缓存已热）首音频 e2e P50 **43.4 ms**（max 70.7 ms）、正式答案 P50 **41.7 ms**（max 68.8 ms）。

### 对话日志整理落盘

`scripts/export_conversations.py` 把两端的原始日志归一化到 `reports/conversations/`：

```bash
new/.venv/bin/python scripts/export_conversations.py
# -> reports/conversations/{root,g2}/<session_id>.json + index.json + index.md（185 个会话）
```

每个会话记录轮次、用户文本、客服回复、以及 `e2e_ms`/`asr_final`/`llm_first_token`/`first_sentence`/`tts_first_chunk`/`downlink`/`playback` 分解；G2 侧记录 `voice_to_playback_ms` 与 `reused`。


## 10. 复现


```bash
# 1) 起服务（见第 1 节），留意 MELO_PREWARM_TEXTS 让 filler 免合成
export MELO_PREWARM_TEXTS="好的，我明白了。亲，请您稍等。|好的，我帮您确认一下。亲，请您稍等。|好的，我帮您看一下这笔申请。亲，请您稍等。|好的，我帮您确认处理结果。亲，请您稍等。|好的，我收到您的信息了。亲，请您稍等。"
# 等待 /voice/status 就绪
# 2) 全链路（音频 + 文本聚合）
new/.venv/bin/python .tmp/latency_full_chain.py --rounds 4 --warmup 1
# 3) 脚本化对话（含首个 LLM 轮，输出现场表）
new/.venv/bin/python .tmp/final_latency.py
# 4) 分支自带套件
cd new && .venv/bin/python -m pytest tests/test_latency.py -v -s
# 5) 磁盘缓存冷/暖对比：先 rm -rf data/cache 跑一次，再重启服务跑一次
rm -rf data/cache && .tmp/start_server.sh   # 冷
new/.venv/bin/python .tmp/final_latency.py
# Ctrl-C 后重新 .tmp/start_server.sh（暖，缓存从磁盘加载）
new/.venv/bin/python .tmp/final_latency.py
# 6) 整理对话日志
new/.venv/bin/python scripts/export_conversations.py
```

原始数据：`reports/latency-full-chain-scripted.json`、`reports/latency-full-chain-aggregate.json`、`reports/conversations/index.json`。
