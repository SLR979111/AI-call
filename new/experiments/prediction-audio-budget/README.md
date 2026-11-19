# 预测候选音频预算实验

比较“每条预测候选都预合成音频”与“只预合成前 N 条”的离线实现。脚本不调用真实
DeepSeek/ASR/TTS 接口，只使用 fake Cloud、脚本化 Brain 和受控 `asyncio.sleep` 延迟。

实现开关：

```text
PREDICTION_AUDIO_LIMIT=
```

- 所有档位都先发布文本候选；音频只在后台准备，不再等音频才发布文字。
- 不设置或留空：每条候选都在后台预合成音频，保持生产默认。
- `PREDICTION_AUDIO_LIMIT=N`（N 为正整数）：只给按到达顺序的前 N 条候选预合成音频。
  超出预算的候选仍以文本发布；被选中时走普通 TTS。
- `PREDICTION_AUDIO_LIMIT=0`：完全不预合成，全部候选以文本发布。

## 运行

在 `new/` 目录：

```powershell
uv run python scripts/experiment_prediction_audio_budget.py
```

或使用已经装好依赖的解释器：

```powershell
python scripts/experiment_prediction_audio_budget.py
```

可选参数：

```powershell
python scripts/experiment_prediction_audio_budget.py --limits all,0,1,2,3,5,10
python scripts/experiment_prediction_audio_budget.py --limits 3 --output runtime/validation/prediction-audio-budget/n3
```

多进程对比：每个档位单独起一个进程，使用不同的 `--output`，避免互相覆盖结果：

```powershell
python scripts/experiment_prediction_audio_budget.py --limits 1 --output experiments/prediction-audio-budget/n1
python scripts/experiment_prediction_audio_budget.py --limits 3 --output experiments/prediction-audio-budget/n3
python scripts/experiment_prediction_audio_budget.py --limits 5 --output experiments/prediction-audio-budget/n5
```

默认输出到本目录：

- `results.json`：每档、每候选、每脚本化短答的完整数据。
- `results.md`：可直接阅读的对比报告，包含总览、逐候选时间、短答可用性、TTS 回退检查、结论。

## 测试

```powershell
python -m pytest -q tests/test_prediction_audio_limit.py
```

覆盖文字先发布、默认全量音频、N=2 截断、预算内无音频仍保留文本、N=0 全文本、文本候选复用回退 TTS。

## 当前结论

默认保持 `all`（不设置 `PREDICTION_AUDIO_LIMIT`），分支数保持 5。

按真实分支归属回放，分支数从 5 减少并不能线性换成命中：

```text
分支 1：内容覆盖 6/9，语音音频 4/5
分支 2：内容覆盖 6/9，语音音频 4/5
分支 3：内容覆盖 7/9，语音音频 5/5
分支 4：内容覆盖 7/9，语音音频 5/5（比 3 多 33% 字节，无增益）
分支 5：内容覆盖 9/9，语音音频 5/5
```

所以不要直接把默认改成分支 4；分支 3 只有在明确接受丢失部分“忙/结束”复用、并且重采足够样本后才考虑。

音频预算方面，旧日志的“排名”混合了音频完成顺序和文本到达顺序，不能直接拿 `排名 ≤ N` 推覆盖率。当前可暂记：

```text
N=7 旧口径可省约 33% 字节，覆盖 9/9
N=4–5 在当前文本口径下只剩 1 次回退，但样本太薄，先不要上线
N≤3 不要
```

更优方向不是硬砍条数，而是按分支优先级准备音频：先 p0，再 p4，再 p2，6 条音频可覆盖 9/9，字节约为全量的 58%。这需要先改候选/音频优先级，再重跑真实样本。

## 读结果时注意

- 这是合成负载；延迟、并发排队和真实候选命中分布都不是生产测量。
- 生产中预测分支并发完成，`前 N 条` 指实际到达顺序。
- 文本先发布后，音频预算不再影响文字候选的可见时间；它只影响候选音频是否已就绪。
- 选中未预合成音频的候选会回退普通语音合成，接话时间会增加。
- 默认不设置环境变量时，线上音频准备策略不变；文本发布时机已经改为先发布。
