# 31 · 缓存审计日志与回灌

> 状态：已实现 provider 边界审计 + 离线回灌工具。  
> 默认关闭；设置 `CACHE_AUDIT_PATH` 后启用。

## 1. 为什么需要它

现有会话日志不能重建磁盘缓存键：

```text
对话模型：需要完整 messages、模型、温度、max_tokens、原始回复
语音识别：需要完整 PCM
语音合成：需要音色、文本、PCM
```

会话 JSONL 只有用户原话、候选、延迟和字节数，缺少上述内容。

## 2. 配置

```text
CACHE_PATH=./runtime/cache.sqlite3
CACHE_AUDIT_PATH=./runtime/cache-audit.jsonl
```

`CACHE_AUDIT_PATH` 留空即关闭。启用后会额外写：

```text
runtime/cache-audit.jsonl
runtime/cache-audit-pcm/<sha256>.pcm
```

审计只在缓存未命中、实际调用服务并成功写入缓存时记录；缓存命中不重复写大对象。

## 3. 回灌

```powershell
cd H:\aicall\new
.\.venv\Scripts\python.exe scripts\backfill_cache.py --dry-run `
  --audit runtime/cache-audit.jsonl `
  --cache runtime/cache.sqlite3
```

去掉 `--dry-run` 才写缓存。建议在服务停止或低写入时执行；SQLite 使用 WAL。

输出：

```json
{
  "dry_run": false,
  "counts": {
    "llm": {"read": 1, "written": 1, "already": 0, "skipped": 0},
    "tts": {"read": 1, "written": 1, "already": 0, "skipped": 0},
    "asr": {"read": 0, "written": 0, "already": 0, "skipped": 0}
  }
}
```

## 4. 覆盖范围

```text
对话模型：可完整回灌（messages + model + temperature + max_tokens + response）
语音合成：可完整回灌（text + voice + PCM）
语音识别：可回灌，但只对逐字节相同的 PCM 有效
开场预测：不在本审计中；仍按任务卡 + 模型的开场预测缓存
```

## 5. 限制

```text
审计含用户对话文本和原始回复，目录不要提交到仓库
TTS PCM 会占磁盘，需定期清理 cache-audit-pcm/
TTL/上限仍由主缓存决定：LLM 500/10MB、ASR 200/20MB、TTS 48/12MB
回灌不会把“曾经没命中”变成“马上命中”；真实通话的请求键仍是变化的
```

## 6. 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_audit.py
```

测试覆盖：

```text
审计写入
LLM/TTS/ASR 回灌
dry-run 不写缓存
```
